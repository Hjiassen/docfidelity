"""取消后的生命周期操作不能与仍在写产物的旧 worker 并行。"""

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from docfidelity.contracts.errors import EngineError, ErrorCode
from docfidelity.contracts.job import STAGE_NAMES
from docfidelity.pipeline import StageContext, StageOutcome
from tests.fixtures.server_fixture import build_harness

pytestmark = pytest.mark.api


@pytest.mark.parametrize("operation", ["retry-stage", "retry-full", "resume", "rerun-failed"])
def test_canceled_execution_rejects_mutation_until_worker_finishes(
    tmp_path: Path, operation: str
) -> None:
    harness = build_harness(tmp_path)
    parent = harness.services.submit_build(harness.new_project(), doc_keys=["overview"])
    job_id = parent.child_job_ids[0]

    def mutate() -> None:
        if operation.startswith("retry-"):
            harness.services.retry(job_id, mode=operation.removeprefix("retry-"))
        elif operation == "resume":
            harness.services.resume(job_id)
        else:
            harness.services.rerun_failed(parent.job_id)

    def cancel_during_render(context: StageContext) -> StageOutcome:
        harness.services.cancel(job_id)
        assert harness.runner.has_checkpoint(job_id, "ingest")
        with pytest.raises(EngineError) as caught:
            mutate()
        assert caught.value.code == ErrorCode.STATE_INVALID
        assert harness.repository.get_job(job_id).status == "canceled"
        assert harness.runner.has_checkpoint(job_id, "ingest")
        context.workspace.put_text("artifacts/out.docx", "已完成的阶段产物")
        return StageOutcome()

    harness.runner.registry.register("render", cancel_during_render, replace=True)
    outcome = harness.runner.run_once()
    assert outcome is not None and outcome.status == "canceled"
    assert harness.stages["qa"].call_count == 0
    assert harness.repository.get_job(job_id).artifact_ids

    harness.runner.registry.register("render", harness.stages["render"], replace=True)
    if operation == "retry-stage":
        with pytest.raises(EngineError) as caught:
            mutate()
        assert caught.value.code == ErrorCode.NOT_RESUMABLE
        harness.services.resume(job_id)
    else:
        mutate()
    harness.run_all()
    assert harness.repository.get_job(job_id).status == "succeeded"


def test_batch_retry_acquires_all_execution_locks_before_resetting_children(tmp_path: Path) -> None:
    harness = build_harness(tmp_path)
    parent = harness.services.submit_build(harness.new_project())

    def cancel_during_render(context: StageContext) -> StageOutcome:
        harness.services.cancel(parent.job_id)
        with pytest.raises(EngineError) as caught:
            harness.services.rerun_failed(parent.job_id)
        assert caught.value.code == ErrorCode.STATE_INVALID
        assert all(
            harness.repository.get_job(job_id).status == "canceled"
            for job_id in parent.child_job_ids
        )
        assert harness.runner.has_checkpoint(context.job.job_id, "ingest")
        return StageOutcome()

    harness.runner.registry.register("render", cancel_during_render, replace=True)
    harness.run_all()
    assert harness.repository.get_job(parent.job_id).status == "canceled"


@pytest.mark.parametrize("operation", ["retry-full", "resume-from"])
def test_invalid_operation_preserves_completed_job_checkpoints(
    tmp_path: Path, operation: str
) -> None:
    harness = build_harness(tmp_path)
    parent = harness.services.submit_build(harness.new_project(), doc_keys=["overview"])
    harness.run_all()
    job_id = parent.child_job_ids[0]
    assert all(harness.runner.has_checkpoint(job_id, stage) for stage in STAGE_NAMES)
    with pytest.raises(EngineError) as caught:
        if operation == "retry-full":
            harness.services.retry(job_id, mode="full")
        else:
            harness.services.resume(job_id, from_stage="ingest")
    assert caught.value.code == ErrorCode.NOT_RESUMABLE
    assert all(harness.runner.has_checkpoint(job_id, stage) for stage in STAGE_NAMES)


def test_batch_retry_rereads_child_state_after_acquiring_execution_locks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = build_harness(tmp_path)
    parent = harness.services.submit_build(harness.new_project(), doc_keys=["overview"])
    job_id = parent.child_job_ids[0]
    harness.services.cancel(job_id)
    original_guard = harness.runner.execution_guard
    interleaved = False

    @contextmanager
    def interleave_completed_retry(target: str) -> Iterator[None]:
        nonlocal interleaved
        if not interleaved:
            interleaved = True
            # 另一请求在本次列出候选后先取得锁并完成重跑。
            harness.services.retry(target, mode="full")
            harness.run_all()
        with original_guard(target):
            yield

    monkeypatch.setattr(harness.runner, "execution_guard", interleave_completed_retry)
    assert harness.services.rerun_failed(parent.job_id) == []
    assert harness.repository.get_job(job_id).status == "succeeded"
    assert all(harness.runner.has_checkpoint(job_id, stage) for stage in STAGE_NAMES)
