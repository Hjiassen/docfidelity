"""完成事件必须在最终产物与报告可查询后发布。"""

import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from docfidelity.contracts.errors import EngineError, ErrorCode
from docfidelity.contracts.job import Artifact, EventType, Job, JobEvent
from docfidelity.pipeline import EventLog, StageContext, StageOutcome
from server.api.deps import build_state
from server.main import APP_STATE_ATTRIBUTE, create_app
from server.sse import job_stream_settled
from server.storage import SqlAlchemyRepository, create_engine_for, create_schema
from tests.fixtures.server_fixture import build_harness, build_registry

pytestmark = pytest.mark.api


class AbandonedWorker(BaseException):
    """模拟进程在登记产物前退出，不能被阶段异常兜底捕获。"""


@pytest.mark.parametrize("cancel_parent", [False, True])
def test_restarted_worker_drains_canceled_artifacts_without_executing_stages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cancel_parent: bool
) -> None:
    harness = build_harness(
        tmp_path,
        stages=build_registry(render={"artifacts": ("out.docx",)}, qa={"emit_report": True}),
    )
    parent = harness.services.submit_build(harness.new_project(), doc_keys=["overview"])
    child_id = parent.child_job_ids[0]

    def crash_before_registration(job: Job) -> list[Artifact]:
        harness.services.cancel(parent.job_id if cancel_parent else job.job_id)
        raise AbandonedWorker

    monkeypatch.setattr(harness.runner, "_collect_artifacts", crash_before_registration)
    with pytest.raises(AbandonedWorker):
        harness.runner.run_once()
    assert harness.repository.list_artifacts(child_id) == []
    assert harness.repository.find_report(child_id) is None

    stages = build_registry()
    restarted = build_state(settings=harness.settings, repository=harness.repository, stages=stages)
    assert restarted.runner.restore_events() == 2
    for job_id in (parent.job_id, child_id):
        assert not job_stream_settled(restarted.runner.events, restarted.services.get_job(job_id))

    outcome = restarted.runner.run_once()

    assert outcome is not None, "取消的崩溃作业仍须认领收尾"
    assert outcome.status == "canceled"
    assert outcome.ran == ()
    assert outcome.reused == ()
    assert len(restarted.repository.list_artifacts(child_id)) == 1
    assert restarted.repository.find_report(child_id) is not None
    assert all(stages.resolve(stage).call_count == 0 for stage in stages.registered())
    for job_id in (parent.job_id, child_id):
        assert job_stream_settled(restarted.runner.events, restarted.services.get_job(job_id))
    events = restarted.runner.events.events(child_id)
    assert events[-1].payload == {"status": "canceled", "execution_active": False}
    assert events[-2].type == "artifact"
    assert restarted.runner.run_once() is None


def test_cancellation_between_claim_and_start_waits_for_execution_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = build_harness(tmp_path)
    parent = harness.services.submit_build(harness.new_project(), doc_keys=["overview"])
    state = build_state(
        settings=harness.settings, repository=harness.repository, stages=harness.runner.registry
    )
    original = state.runner._start
    observed: list[bool] = []

    def cancel_before_start(job: Job) -> Job:
        state.services.cancel(job.job_id)
        observed.append(
            job_stream_settled(state.runner.events, state.repository.get_job(job.job_id))
        )
        return original(job)

    monkeypatch.setattr(state.runner, "_start", cancel_before_start)
    outcome = state.runner.run_once()

    assert observed == [False], "执行锁已被认领时，取消流不能先于 worker 收尾"
    assert outcome is not None and outcome.status == "canceled"
    assert outcome.ran == ()
    assert all(stage.call_count == 0 for stage in harness.stages.values())
    assert job_stream_settled(
        state.runner.events, state.repository.get_job(parent.child_job_ids[0])
    )


def test_partial_failure_parent_waits_for_the_canceled_child_to_finish_registration(
    tmp_path: Path,
) -> None:
    harness = build_harness(tmp_path)
    parent = harness.services.submit_build(harness.new_project())
    assert harness.runner.run_once() is not None
    observed: list[bool] = []

    def cancel_during_render(context: StageContext) -> StageOutcome:
        harness.services.cancel(context.job.job_id)
        current = harness.repository.get_job(parent.job_id)
        assert current.status == "partial_failure"
        observed.append(job_stream_settled(harness.runner.events, current))
        context.workspace.put_text("artifacts/out.docx", "阶段产物")
        return StageOutcome()

    harness.runner.registry.register("render", cancel_during_render, replace=True)
    outcome = harness.runner.run_once()

    assert observed == [False]
    assert outcome is not None and outcome.status == "canceled"
    assert outcome.artifact_ids
    assert job_stream_settled(harness.runner.events, harness.repository.get_job(parent.job_id))


@pytest.mark.parametrize("failed", [False, True])
@pytest.mark.parametrize("crash_before_parent", [False, True])
def test_terminal_state_saved_before_worker_exit_can_finish_publication_after_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failed: bool, crash_before_parent: bool
) -> None:
    harness = build_harness(
        tmp_path,
        stages=build_registry(render={"failures": int(failed), "artifacts": ("out.docx",)}),
    )
    parent = harness.services.submit_build(harness.new_project(), doc_keys=["overview"])
    child_id = parent.child_job_ids[0]
    original = harness.runner.events.emit

    def crash_at_publication(
        job_id: str, kind: EventType, payload: Mapping[str, Any] | None = None
    ) -> JobEvent:
        if kind == "job" and payload and payload.get("execution_active") is False:
            raise AbandonedWorker
        return original(job_id, kind, payload)

    monkeypatch.setattr(harness.runner.events, "emit", crash_at_publication)
    if crash_before_parent:

        def crash_at_aggregation(job: Job, *, has_pending_decision: bool = False) -> None:
            raise AbandonedWorker

        monkeypatch.setattr(harness.runner, "_aggregate_parent", crash_at_aggregation)
    with pytest.raises(AbandonedWorker):
        harness.runner.run_once()
    status = "failed" if failed else "succeeded"
    assert harness.repository.get_job(child_id).status == status
    stages = build_registry()
    restarted = build_state(settings=harness.settings, repository=harness.repository, stages=stages)
    assert not job_stream_settled(restarted.runner.events, restarted.services.get_job(child_id))

    outcome = restarted.runner.run_once()

    assert outcome is not None
    assert outcome.status == status
    assert outcome.ran == ()
    assert all(stages.resolve(stage).call_count == 0 for stage in stages.registered())
    assert job_stream_settled(restarted.runner.events, restarted.services.get_job(child_id))
    assert restarted.services.get_job(parent.job_id).status == status


def test_cancellation_during_session_setup_is_not_overwritten_by_setup_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = build_harness(tmp_path)
    parent = harness.services.submit_build(harness.new_project(), doc_keys=["overview"])
    child_id = parent.child_job_ids[0]

    def fail_after_cancellation(job: Job) -> None:
        harness.services.cancel(job.job_id)
        raise EngineError(ErrorCode.CONFIG_INVALID, "会话初始化失败")

    monkeypatch.setattr(harness.runner, "session_for", fail_after_cancellation)
    outcome = harness.runner.run_once()

    assert outcome is not None and outcome.status == "canceled"
    assert outcome.ran == ()
    for job_id in (parent.job_id, child_id):
        assert job_stream_settled(harness.runner.events, harness.repository.get_job(job_id))


def test_awaiting_decision_worker_exit_can_finish_publication_without_reasking_decisions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = build_harness(
        tmp_path,
        stages=build_registry(
            qa={"decide_point": "placement.section_slot", "artifacts": ("out.docx",)}
        ),
    )
    parent = harness.services.submit_build(harness.new_project(), doc_keys=["overview"])
    child_id = parent.child_job_ids[0]
    original = harness.runner.events.emit

    def crash_at_publication(
        job_id: str, kind: EventType, payload: Mapping[str, Any] | None = None
    ) -> JobEvent:
        if kind == "job" and payload and payload.get("execution_active") is False:
            raise AbandonedWorker
        return original(job_id, kind, payload)

    monkeypatch.setattr(harness.runner.events, "emit", crash_at_publication)
    with pytest.raises(AbandonedWorker):
        harness.runner.run_once()
    assert harness.repository.get_job(child_id).status == "awaiting_decision"
    stages = build_registry()
    restarted = build_state(settings=harness.settings, repository=harness.repository, stages=stages)

    outcome = restarted.runner.run_once()

    assert outcome is not None and outcome.status == "awaiting_decision"
    assert outcome.ran == ()
    assert all(stages.resolve(stage).call_count == 0 for stage in stages.registered())
    events = restarted.runner.events.events(child_id)
    assert (
        len(
            [
                event
                for event in events
                if event.type == "decision" and event.payload.get("record") == "decision"
            ]
        )
        == 1
    )
    assert events[-1].payload == {"status": "awaiting_decision", "execution_active": False}
    assert not job_stream_settled(restarted.runner.events, restarted.services.get_job(child_id))
    assert restarted.runner.run_once() is None


_CANCELING_WORKER_SCRIPT = """
import sys
from pathlib import Path
from docfidelity.pipeline import StageOutcome
from server.api.deps import build_state
from server.settings import ServerSettings
from tests.fixtures.server_fixture import build_registry

registry = build_registry()
state = build_state(settings=ServerSettings(_env_file=None, storage_root=Path(sys.argv[1]),
                    database_url=sys.argv[2]), stages=registry)

def render(context):
    state.services.cancel(context.job.job_id)
    context.workspace.put_text('artifacts/out.docx', 'completed artifact')
    print('READY', flush=True)
    assert sys.stdin.readline().strip() == 'GO'
    return StageOutcome()

registry.register('render', render, replace=True)
outcome = state.runner.run_once()
assert outcome is not None and outcome.status == 'canceled'
print('DONE', flush=True)
"""


@pytest.mark.parametrize("crash_worker", [False, True])
def test_api_restart_preserves_live_execution_and_worker_crash_releases_its_lock(
    tmp_path: Path, crash_worker: bool
) -> None:
    url = f"sqlite:///{tmp_path / 'state.db'}"
    engine = create_engine_for(url)
    create_schema(engine)
    harness = build_harness(tmp_path, repository=SqlAlchemyRepository(engine), database_url=url)
    parent = harness.services.submit_build(harness.new_project(), doc_keys=["overview"])
    child_id = parent.child_job_ids[0]
    process = subprocess.Popen(
        [sys.executable, "-c", _CANCELING_WORKER_SCRIPT, str(tmp_path), url],
        cwd=Path(__file__).resolve().parents[2],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stdout is not None and process.stdout.readline().strip() == "READY"
        app = create_app(
            settings=harness.settings,
            repository=SqlAlchemyRepository(engine),
            stages=build_registry(),
            start_worker=False,
        )
        with TestClient(app):
            api = getattr(app.state, APP_STATE_ATTRIBUTE)
            assert api.runner.events.execution_active(child_id)
            for job_id in (parent.job_id, child_id):
                assert not job_stream_settled(api.runner.events, api.services.get_job(job_id))
            assert api.runner.run_once() is None, "API 不能抢走仍在执行的取消收尾"
            assert api.repository.list_artifacts(child_id) == []
            if crash_worker:
                process.terminate()
                process.communicate(timeout=10)
                assert not api.runner.events.execution_active(child_id)
                assert not job_stream_settled(api.runner.events, api.services.get_job(child_id))
                outcome = api.runner.run_once()
                assert outcome is not None and outcome.ran == ()
                assert outcome.status == "canceled"
            else:
                assert process.stdin is not None
                process.stdin.write("GO\n")
                process.stdin.flush()
                output, error = process.communicate(timeout=10)
                assert process.returncode == 0, error
                assert "DONE" in output
            assert len(api.repository.list_artifacts(child_id)) == 1
            for job_id in (parent.job_id, child_id):
                assert job_stream_settled(api.runner.events, api.services.get_job(job_id))
            events = api.runner.events.events(child_id)
            assert events[-1].payload["execution_active"] is False
            assert any(event.type == "artifact" for event in events[:-1])
            assert api.runner.run_once() is None
    finally:
        if process.poll() is None:
            process.terminate()
        process.wait(timeout=5)
        engine.dispose()


@pytest.mark.parametrize("matching_progress", [True, False])
def test_legacy_parent_events_compare_progress_without_format_metadata(
    tmp_path: Path, matching_progress: bool
) -> None:
    harness = build_harness(tmp_path)
    parent = harness.services.submit_build(harness.new_project(), doc_keys=["overview"])
    harness.services.cancel(parent.child_job_ids[0])
    canceled = harness.repository.get_job(parent.job_id)
    assert canceled.progress is not None
    payload = canceled.progress.model_dump(exclude={"format_version"})
    if not matching_progress:
        payload["done"] += 1
    legacy_log = EventLog()
    legacy_log.emit(parent.job_id, "job", payload)
    assert job_stream_settled(legacy_log, canceled) is matching_progress


def test_canceling_last_queued_child_publishes_parent_terminal_event(tmp_path: Path) -> None:
    harness = build_harness(tmp_path)
    parent = harness.services.submit_build(harness.new_project(), doc_keys=["overview"])
    harness.services.cancel(parent.child_job_ids[0])
    canceled = harness.repository.get_job(parent.job_id)
    assert canceled.status == "canceled"
    assert harness.runner.run_once() is None
    assert job_stream_settled(harness.runner.events, canceled)


@pytest.mark.parametrize("cancel_parent", [False, True])
def test_active_cancellation_keeps_streams_open_until_artifacts_are_registered(
    tmp_path: Path, cancel_parent: bool
) -> None:
    harness = build_harness(tmp_path)
    during: list[bool] = []

    def cancel_during_render(context: StageContext) -> StageOutcome:
        child = harness.repository.get_job(context.job.job_id)
        assert child.parent_job_id is not None
        harness.services.cancel(child.parent_job_id if cancel_parent else child.job_id)
        for job_id in (child.job_id, child.parent_job_id):
            during.append(
                job_stream_settled(harness.runner.events, harness.repository.get_job(job_id))
            )
        context.workspace.put_text("artifacts/out.docx", "已完成的阶段产物")
        return StageOutcome()

    harness.runner.registry.register("render", cancel_during_render, replace=True)
    parent = harness.services.submit_build(harness.new_project(), doc_keys=["overview"])
    harness.run_all()
    assert during == [False, False]
    for job_id in (parent.job_id, parent.child_job_ids[0]):
        assert job_stream_settled(harness.runner.events, harness.repository.get_job(job_id))
    assert harness.repository.get_job(parent.child_job_ids[0]).artifact_ids


def test_terminal_event_is_published_after_artifact_and_report_registration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = build_harness(
        tmp_path,
        stages=build_registry(render={"artifacts": ("out.docx",)}, qa={"emit_report": True}),
    )
    original = harness.runner.events.emit
    observed: list[str] = []

    def observe(job_id: str, kind: EventType, payload: Mapping[str, Any] | None = None) -> JobEvent:
        job = harness.repository.find_job(job_id)
        if (
            kind == "job"
            and payload
            and payload.get("status") == "succeeded"
            and job is not None
            and job.kind == "document"
        ):
            assert job.artifact_ids, "完成事件发布前必须登记可下载产物"
            assert harness.repository.find_report(job_id) is not None
            observed.append(job_id)
        return original(job_id, kind, payload)

    monkeypatch.setattr(harness.runner.events, "emit", observe)
    harness.build(harness.new_project())
    assert len(observed) == 2
