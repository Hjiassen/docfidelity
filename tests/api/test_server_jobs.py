"""作业队列 · 项目锁 · worker（DD-11.4 / DD-11.5 / DD-13.1）。

判据是"作业可续跑"，故这里反复问四个问题：

1. **失败之后，前面已完成的还留着吗？**（断点保留 → 续跑不重头来）
2. **失败之后，后面还会跑吗？**（失败隔离 → 下游不再执行）
3. **重试真的重跑了那段吗？**（丢断点 → 否则显示"重跑成功"而一次都没执行）
4. **一个作业失败，同批其他作业受影响吗？**（子作业之间互不牵连）

第 3 条是这一轮里最容易做错的一条：``restart`` / ``retry_stage`` 只改状态是不够的，
``run_stage`` 的复用判定看的是断点——不丢断点，每个阶段都会被复用掉。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

import pytest

from docfidelity.contracts.errors import EngineError, ErrorCode
from docfidelity.contracts.job import STAGE_NAMES, Job
from docfidelity.pipeline import (
    ARTIFACTS_DIRNAME,
    StageContext,
    StageOutcome,
)
from server.jobs import WorkerLoop, artifact_id_of, artifact_kind_of
from tests.fixtures.server_fixture import (
    FIXED_NOW,
    ArtifactStage,
    Harness,
    build_harness,
    build_registry,
)

pytestmark = pytest.mark.api


@pytest.fixture
def harness(tmp_path: Path) -> Harness:
    return build_harness(tmp_path)


# --------------------------------------------------------------------- 派生
def test_submit_derives_one_child_per_enabled_document(harness: Harness) -> None:
    project_id = harness.new_project()
    orchestration = harness.services.submit_build(project_id)
    children = harness.services.job_children(orchestration.job_id)
    assert orchestration.is_orchestration and orchestration.status == "queued"
    assert [child.doc_key for child in children] == ["overview", "detail"]
    assert all(child.parent_job_id == orchestration.job_id for child in children)
    assert all(child.status == "queued" for child in children)
    assert orchestration.child_job_ids == [child.job_id for child in children]


def test_submit_emits_a_job_event_with_the_child_matrix(harness: Harness) -> None:
    project_id = harness.new_project()
    orchestration = harness.services.submit_build(project_id)
    events = harness.runner.events.since(orchestration.job_id, 0)
    assert len(events) == 1
    assert events[0].type == "job"
    assert events[0].payload["total"] == 2
    assert len(events[0].payload["child_job_ids"]) == 2


def test_submit_can_target_a_subset_of_documents(harness: Harness) -> None:
    project_id = harness.new_project()
    orchestration = harness.services.submit_build(project_id, doc_keys=["detail"])
    assert [child.doc_key for child in harness.services.job_children(orchestration.job_id)] == [
        "detail"
    ]


def test_inputs_hash_is_not_shared_between_different_projects(harness: Harness) -> None:
    """输入摘要不同 = 断点不会跨项目误复用。"""
    first = harness.services.submit_build(harness.new_project("甲"))
    second = harness.services.submit_build(harness.new_project("乙"))
    assert first.inputs_hash != second.inputs_hash


# ------------------------------------------------------------------- 调度
def test_run_once_returns_none_when_nothing_is_pending(harness: Harness) -> None:
    assert harness.runner.run_once() is None
    assert harness.runner.run_until_idle() == []


def test_run_once_takes_one_child_and_leaves_the_rest_queued(harness: Harness) -> None:
    project_id = harness.new_project()
    orchestration = harness.services.submit_build(project_id)
    outcome = harness.runner.run_once()
    assert outcome is not None
    assert outcome.status == "succeeded"
    children = harness.services.job_children(orchestration.job_id)
    assert [child.status for child in children] == ["succeeded", "queued"]


def test_children_and_stages_run_in_order(harness: Harness) -> None:
    project_id = harness.new_project()
    orchestration = harness.services.submit_build(project_id)
    outcomes = harness.run_all()
    expected = list(orchestration.child_job_ids)
    assert [item.job_id for item in outcomes] == expected
    assert all(item.ran == STAGE_NAMES for item in outcomes)


def test_skip_reason_is_not_a_stage_failure(tmp_path: Path) -> None:
    """``skipped`` 不是失败：作业照常成功，且该阶段不落断点（见 ``pipeline.stages``）。"""
    harness = build_harness(
        tmp_path, stages=build_registry(refresh={"skip_reason": "目录无需刷新"})
    )
    project_id = harness.new_project()
    harness.build(project_id)
    child = harness.repository.list_jobs(kind="document")[0]
    assert child.status == "succeeded"
    stages = {stage.name: stage.status for stage in harness.services.job_stages(child.job_id)}
    assert stages["refresh"] == "skipped"
    assert harness.runner.has_checkpoint(child.job_id, "refresh") is False
    assert harness.services.resume_plan(child.job_id) == (), "成功作业不应因跳过阶段提示续跑"


def test_parent_aggregates_to_succeeded_when_all_children_succeed(harness: Harness) -> None:
    project_id = harness.new_project()
    orchestration_id = harness.build(project_id)
    parent = harness.repository.get_job(orchestration_id)
    assert parent.status == "succeeded"
    assert parent.progress is not None
    assert (parent.progress.done, parent.progress.total, parent.progress.failed) == (2, 2, 0)
    assert parent.child_job_ids == [
        item.job_id for item in harness.services.job_children(orchestration_id)
    ]


def test_succeeded_stages_report_their_checkpoint(harness: Harness) -> None:
    """成功的阶段必须报出断点引用。

    界面那一列（``StageTimeline``）回答的是"重跑时会不会真的重跑"：有断点且输入未变
    就会被直接复用。若服务端只在"沿用了断点"时才填，则**每个刚跑完的成功的阶段都会
    被显示成"无断点"**——明明刚写了断点，却告诉使用者重跑会重新执行一遍。
    """
    project_id = harness.new_project()
    harness.build(project_id)
    child = harness.repository.list_jobs(kind="document")[0]
    stages = harness.services.job_stages(child.job_id)
    assert stages
    for stage in stages:
        if stage.status == "succeeded":
            assert stage.checkpoint is not None, f"{stage.name} 成功后必须报出断点"
            assert stage.checkpoint.inputs_hash == child.inputs_hash
            assert stage.checkpoint.stage == stage.name
        else:
            assert stage.checkpoint is None, f"{stage.name}（{stage.status}）不落断点"


def test_orchestration_job_has_no_resume_plan(harness: Harness) -> None:
    """编排作业自身不推进阶段，故"可续跑阶段"必须为空。

    不特判就会算出"五个阶段全都要跑"，界面对一个**已经完成**的编排作业显示
    "将从 ingest → … 继续"——那句话是错的，而且错得让人以为还得再跑一遍。
    """
    project_id = harness.new_project()
    orchestration_id = harness.build(project_id)
    assert harness.repository.get_job(orchestration_id).status == "succeeded"
    assert harness.services.resume_plan(orchestration_id) == ()
    child = harness.repository.list_jobs(kind="document")[0]
    assert harness.services.resume_plan(child.job_id) == (), "全部阶段有有效断点时无可续跑"


def test_failed_job_resume_plan_starts_at_the_failed_stage(
    tmp_path: Path,
) -> None:
    """失败作业的可续跑点必须**落在失败阶段**（上游有断点，故不重跑）。"""
    harness = build_harness(tmp_path, stages=build_registry(render={"failures": 1}))
    project_id = harness.new_project()
    orchestration_id = harness.services.submit_build(project_id).job_id
    harness.run_all()
    child = harness.repository.list_jobs(kind="document")[0]
    assert child.status == "failed"
    assert harness.services.resume_plan(child.job_id) == ("render", "refresh", "qa")
    assert harness.repository.get_job(orchestration_id).status == "partial_failure"


# ------------------------------------------------------------------- 项目锁
def test_a_held_project_lock_keeps_the_job_pending(harness: Harness) -> None:
    """取不到锁 → 作业**保持原状态**（不能改成 failed：一次正常排队被显示成失败）。"""
    project_id = harness.new_project()
    orchestration_id = harness.services.submit_build(project_id).job_id
    other = harness.runner.locks.acquire(project_id, "j_someone_else", now=harness.clock())
    assert other.acquired
    try:
        assert harness.runner.run_once() is None
    finally:
        harness.runner.locks.release(project_id, "j_someone_else", now=harness.clock())
    assert harness.repository.get_job(orchestration_id).child_job_ids
    assert all(
        child.status == "queued" for child in harness.services.job_children(orchestration_id)
    )
    # 锁一释放，作业立刻能被认领。
    assert harness.runner.run_once() is not None


def test_the_lease_is_released_even_when_the_stage_explodes(harness: Harness) -> None:
    """阶段体抛裸异常也必须释放租约，否则该项目**永久**停在"排队中"。"""
    registry = harness.runner.registry
    registry.register("compose", _RaisingStage("boom"), replace=True)
    project_id = harness.new_project()
    harness.services.submit_build(project_id)
    outcome = harness.runner.run_once()
    assert outcome is not None and outcome.status == "failed"
    assert harness.runner.locks.holder(project_id) is None


# --------------------------------------------------------------- 失败隔离
def test_failure_stops_at_the_failed_stage(tmp_path: Path) -> None:
    harness = build_harness(tmp_path, stages=build_registry(render={"failures": 1}))
    project_id = harness.new_project()
    harness.services.submit_build(project_id)
    outcome = harness.runner.run_once()
    assert outcome is not None
    assert outcome.status == "failed"
    assert outcome.stopped_at == "render"
    assert outcome.ran == ("ingest", "compose", "render")
    # 下游**一次都没被调用**。
    assert harness.stages["refresh"].call_count == 0
    assert harness.stages["qa"].call_count == 0


def test_failed_job_carries_the_stage_error(tmp_path: Path) -> None:
    harness = build_harness(tmp_path, stages=build_registry(render={"failures": 1}))
    orchestration_id = harness.build(harness.new_project())
    failed = [
        child
        for child in harness.services.job_children(orchestration_id)
        if child.status == "failed"
    ]
    assert len(failed) == 1
    assert failed[0].error is not None
    assert "注入的失败" in failed[0].error.message


def test_failure_keeps_upstream_checkpoints_and_drops_the_failed_one(tmp_path: Path) -> None:
    """失败不是产出：失败阶段不落断点，上游断点原样保留。"""
    harness = build_harness(tmp_path, stages=build_registry(render={"failures": 1}))
    project_id = harness.new_project()
    orchestration_id = harness.services.submit_build(project_id).job_id
    harness.runner.run_once()
    child = harness.repository.get_job(orchestration_id).child_job_ids[0]
    assert harness.runner.has_checkpoint(child, "ingest") is True
    assert harness.runner.has_checkpoint(child, "compose") is True
    assert harness.runner.has_checkpoint(child, "render") is False
    assert harness.runner.plan_resume(child) == ("render", "refresh", "qa")


def test_one_failing_child_does_not_stop_its_siblings(tmp_path: Path) -> None:
    """失败隔离的**跨子作业**一面：同批里一个失败，另一个照常跑完。"""
    harness = build_harness(tmp_path, stages=build_registry(qa={"failures": 1}))
    project_id = harness.new_project()
    orchestration_id = harness.services.submit_build(project_id).job_id
    harness.run_all()
    children = harness.services.job_children(orchestration_id)
    assert [child.status for child in children] == ["failed", "succeeded"]
    parent = harness.repository.get_job(orchestration_id)
    assert parent.status == "partial_failure"
    assert parent.progress is not None
    assert (parent.progress.done, parent.progress.total, parent.progress.failed) == (2, 2, 1)


# ------------------------------------------------------------------- 重跑
def test_retry_only_reruns_the_failed_stage_and_reuses_upstream(tmp_path: Path) -> None:
    """``retry_stage`` 的语义：丢失败阶段断点，上游一律复用。"""
    harness = build_harness(tmp_path, stages=build_registry(render={"failures": 1}))
    project_id = harness.new_project()
    orchestration_id = harness.services.submit_build(project_id).job_id
    harness.run_all()
    child = harness.repository.get_job(orchestration_id).child_job_ids[0]
    ingest_before = harness.stages["ingest"].call_count

    harness.services.retry(child, mode="stage")
    assert (
        harness.repository.get_job(child).status == "running"
    ), "retry_stage 的结果状态是 running（契约迁移表），不是 queued"
    outcomes = harness.run_all()
    retried = [item for item in outcomes if item.job_id == child]
    assert len(retried) == 1, "被 retry 的作业必须能被 worker 重新认领（见 run_once 的说明）"
    assert retried[0].status == "succeeded"
    assert set(retried[0].reused) >= {"ingest", "compose"}
    assert set(retried[0].ran) >= {"render", "refresh", "qa"}
    # 上游**没有**被重跑：调用次数不变。
    assert harness.stages["ingest"].call_count == ingest_before


def test_full_retry_discards_every_checkpoint(tmp_path: Path) -> None:
    harness = build_harness(tmp_path, stages=build_registry(render={"failures": 1}))
    project_id = harness.new_project()
    orchestration_id = harness.services.submit_build(project_id).job_id
    harness.run_all()
    child = harness.repository.get_job(orchestration_id).child_job_ids[0]
    harness.services.retry(child, mode="full")
    assert harness.runner.has_checkpoint(child, "ingest") is False
    assert harness.runner.plan_resume(child) == STAGE_NAMES


def test_resume_reuses_the_whole_completed_prefix(tmp_path: Path) -> None:
    harness = build_harness(tmp_path, stages=build_registry(render={"failures": 1}))
    project_id = harness.new_project()
    orchestration_id = harness.services.submit_build(project_id).job_id
    harness.run_all()
    child = harness.repository.get_job(orchestration_id).child_job_ids[0]
    harness.services.resume(child)
    outcome = harness.run_all()[0]
    assert outcome.status == "succeeded"
    assert outcome.ran == ("render", "refresh", "qa")


def test_restart_from_canceled_runs_everything_again(tmp_path: Path) -> None:
    harness = build_harness(tmp_path)
    project_id = harness.new_project()
    orchestration_id = harness.services.submit_build(project_id).job_id
    child = harness.repository.get_job(orchestration_id).child_job_ids[0]
    harness.services.cancel(child)
    assert harness.repository.get_job(child).status == "canceled"
    harness.services.retry(child, mode="full")
    outcome = harness.run_all()[0]
    assert outcome.ran == STAGE_NAMES, "从头重跑必须真的每一步都执行"


# ------------------------------------------------------------------- 取消
def test_cancel_takes_effect_between_stages(tmp_path: Path) -> None:
    """取消点落在**阶段之间**：正在跑的阶段写完了，下一个不再开始。"""
    harness = build_harness(tmp_path)
    registry = harness.runner.registry
    holder: dict[str, str] = {}

    def cancel_during_compose(context: StageContext) -> StageOutcome:
        harness.services.cancel(context.job.job_id)
        holder["job_id"] = context.job.job_id
        return StageOutcome(
            outputs={"compose": context.workspace.put_text("work/compose.txt", "done")}
        )

    registry.register("compose", cancel_during_compose, replace=True)
    project_id = harness.new_project()
    harness.services.submit_build(project_id)
    outcome = harness.runner.run_once()
    assert outcome is not None
    assert outcome.status == "canceled"
    assert outcome.ran == ("ingest", "compose")
    assert harness.stages["render"].call_count == 0
    assert harness.stages["qa"].call_count == 0


def test_canceling_an_orchestration_takes_its_children_along(harness: Harness) -> None:
    project_id = harness.new_project()
    orchestration_id = harness.services.submit_build(project_id).job_id
    harness.services.cancel(orchestration_id)
    parent = harness.repository.get_job(orchestration_id)
    assert parent.status == "canceled"
    assert all(
        child.status == "canceled" for child in harness.services.job_children(orchestration_id)
    )


def test_canceling_a_succeeded_child_is_not_possible(harness: Harness) -> None:
    """``succeeded`` 没有到 ``canceled`` 的迁移——已完成的作业不该能被"取消"。"""
    project_id = harness.new_project()
    child = harness.repository.get_job(harness.build(project_id)).child_job_ids[0]
    with pytest.raises(EngineError) as caught:
        harness.services.cancel(child)
    assert caught.value.code == ErrorCode.STATE_INVALID


# ------------------------------------------------------------------ 产物
def test_artifacts_are_registered_with_kind_and_digest(tmp_path: Path) -> None:
    harness = build_harness(
        tmp_path,
        stages=build_registry(render={"artifacts": ("out.docx", "preview.png")}),
    )
    project_id = harness.new_project()
    orchestration_id = harness.services.submit_build(project_id).job_id
    harness.runner.run_once()
    child = harness.repository.get_job(orchestration_id).child_job_ids[0]
    artifacts = {item.relative_path: item for item in harness.repository.list_artifacts(child)}
    assert set(artifacts) == {
        f"{ARTIFACTS_DIRNAME}/out.docx",
        f"{ARTIFACTS_DIRNAME}/preview.png",
    }
    assert artifacts[f"{ARTIFACTS_DIRNAME}/out.docx"].kind == "docx"
    assert artifacts[f"{ARTIFACTS_DIRNAME}/preview.png"].kind == "png"
    for item in artifacts.values():
        assert item.size_bytes > 0
        assert len(item.sha256) == 64
        assert item.expires_at == FIXED_NOW + timedelta(
            days=harness.settings.retention_artifacts_days
        )
    registered = harness.repository.get_job(child).artifact_ids
    assert sorted(registered) == sorted(item.artifact_id for item in artifacts.values())


def test_artifact_ids_are_recorded_on_the_job(tmp_path: Path) -> None:
    harness = build_harness(tmp_path, stages=build_registry(render={"artifacts": ("out.docx",)}))
    project_id = harness.new_project()
    orchestration_id = harness.services.submit_build(project_id).job_id
    harness.runner.run_once()
    child = harness.repository.get_job(orchestration_id).child_job_ids[0]
    relative = f"{ARTIFACTS_DIRNAME}/out.docx"
    assert harness.repository.get_job(child).artifact_ids == [artifact_id_of(child, relative)]


def test_artifact_ids_are_url_safe(tmp_path: Path) -> None:
    """回归：产物 id 曾是 ``<job_id>:<relative_path>``，而相对路径含 ``/``。

    取件端点是 ``GET /artifacts/{artifact_id}/download``——路径参数**不跨** ``/``，
    于是 id 里带斜杠的产物永远匹配不上路由：列表里看得见，下载 404。
    """
    harness = build_harness(tmp_path, stages=build_registry(render={"artifacts": ("out.docx",)}))
    project_id = harness.new_project()
    orchestration_id = harness.services.submit_build(project_id).job_id
    harness.run_all()
    child = harness.repository.get_job(orchestration_id).child_job_ids[0]
    artifacts = harness.repository.list_artifacts(child)
    assert artifacts
    for item in artifacts:
        assert "/" not in item.artifact_id
        assert "\\" not in item.artifact_id
        assert (
            item.relative_path == f"{ARTIFACTS_DIRNAME}/out.docx"
        ), "可读性由 relative_path 承担，id 只负责唯一"


def test_artifact_ids_are_stable_and_unique(tmp_path: Path) -> None:
    """同一个 (job, 相对路径) 恒得同一个 id；不同路径必得不同 id。"""
    assert artifact_id_of("j_1", "artifacts/a/b.docx") == artifact_id_of(
        "j_1", "artifacts/a/b.docx"
    )
    assert artifact_id_of("j_1", "artifacts/a/b.docx") != artifact_id_of(
        "j_1", "artifacts/a.b.docx"
    )
    assert artifact_id_of("j_1", "artifacts/a/b.docx") != artifact_id_of(
        "j_2", "artifacts/a/b.docx"
    )


def test_artifacts_are_not_registered_twice_on_a_rerun(tmp_path: Path) -> None:
    """扫目录 + 去重：重跑（``render`` 被复用、产物**不重新产出**）不该出现两条登记。

    这条路径是真的会走到的：``render`` 已成功、``qa`` 失败 → 续跑时 ``render`` 走断点复用，
    而 ``_collect_artifacts`` 仍会再扫一遍 ``artifacts/``。没有去重，界面上的产物列表
    就会随每次重试变长。
    """
    harness = build_harness(
        tmp_path,
        stages=build_registry(render={"artifacts": ("out.docx",)}, qa={"failures": 1}),
    )
    project_id = harness.new_project()
    orchestration_id = harness.services.submit_build(project_id).job_id
    harness.run_all()
    child = harness.repository.get_job(orchestration_id).child_job_ids[0]
    assert harness.repository.get_job(child).status == "failed", "前提：qa 失败"
    before = harness.repository.list_artifacts(child)
    assert len(before) == 1, "前提：render 已登记一份产物"

    harness.services.retry(child, mode="stage")
    outcomes = harness.run_all()
    assert next(item for item in outcomes if item.job_id == child).status == "succeeded"
    after = harness.repository.list_artifacts(child)
    assert len(after) == 1
    assert {item.artifact_id for item in after} == {item.artifact_id for item in before}


@pytest.mark.parametrize(
    ("suffix", "expected"),
    [
        (".docx", "docx"),
        (".DOCX", "docx"),
        (".pdf", "pdf"),
        (".png", "png"),
        (".json", "report"),
        (".zip", "repro_package"),
        (".txt", "other"),
        ("", "other"),
    ],
)
def test_artifact_kind_lookup(suffix: str, expected: str) -> None:
    """未登记的后缀归 ``other`` 而**不报错**：产物是结果，多一种不该让作业失败。"""
    assert artifact_kind_of(suffix) == expected


def test_no_artifacts_directory_means_no_artifacts(harness: Harness) -> None:
    project_id = harness.new_project()
    child = harness.repository.get_job(harness.build(project_id)).child_job_ids[0]
    assert harness.repository.list_artifacts(child) == []
    assert harness.repository.get_job(child).artifact_ids == []


# ------------------------------------------------------------------ 报告
def test_report_is_stored_when_qa_emits_the_report_output(tmp_path: Path) -> None:
    harness = build_harness(tmp_path, stages=build_registry(qa={"emit_report": True}))
    project_id = harness.new_project()
    orchestration_id = harness.services.submit_build(project_id).job_id
    harness.runner.run_once()
    child = harness.repository.get_job(orchestration_id).child_job_ids[0]
    report = harness.repository.find_report(child)
    assert report is not None
    assert report.verdict == "pass"
    assert report.job_id == child


def test_an_unreadable_report_does_not_fail_the_job(tmp_path: Path) -> None:
    """报告是**元数据**：读不出来不该让一份已经渲染好的文档变成"构建失败"。"""
    harness = build_harness(tmp_path)
    registry = harness.runner.registry

    def bad_report(context: StageContext) -> StageOutcome:
        return StageOutcome(
            outputs={
                "qa": context.workspace.put_text("work/qa.txt", "ok"),
                "report": context.workspace.put_text("work/report.json", "{ 这不是 JSON"),
            }
        )

    registry.register("qa", bad_report, replace=True)
    project_id = harness.new_project()
    orchestration_id = harness.services.submit_build(project_id).job_id
    outcome = harness.runner.run_once()
    assert outcome is not None and outcome.status == "succeeded"
    child = harness.repository.get_job(orchestration_id).child_job_ids[0]
    assert harness.repository.find_report(child) is None
    warnings = [
        event.payload.get("warning")
        for event in harness.runner.events.since(child, 0)
        if event.type == "log"
    ]
    assert "report_unreadable" in warnings


def test_report_is_read_from_checkpoints_not_from_memory(tmp_path: Path) -> None:
    """续跑时上游阶段的输出只在断点里——只看内存会漏掉"上一轮产出的报告"。

    这里让 ``qa`` **失败**（不落断点）而 ``refresh`` 产报告：报告必须仍能落库，
    因为它来自断点而非本次运行的内存。
    """
    harness = build_harness(tmp_path)
    registry = harness.runner.registry

    def refresh_report(context: StageContext) -> StageOutcome:
        from docfidelity.contracts.qa import Report

        report = Report(
            report_id="r_from_refresh",
            job_id=context.job.job_id,
            generated_at=FIXED_NOW,
            findings=[],
            waivers=[],
            verdict="pass",
        )
        return StageOutcome(
            outputs={
                "refresh": context.workspace.put_text("work/refresh.txt", "ok"),
                "report": context.workspace.put_text("work/report.json", report.model_dump_json()),
            }
        )

    registry.register("refresh", refresh_report, replace=True)
    registry.register("qa", _RaisingStage("qa boom"), replace=True)
    project_id = harness.new_project()
    harness.services.submit_build(project_id)
    harness.runner.run_once()
    child = harness.repository.list_jobs(kind="document")[0]
    assert child.status == "failed", "前提：qa 失败"
    assert harness.repository.find_report(child.job_id) is None, "前提：失败时不落报告"
    # qa 修好后续跑：本次运行里 refresh 被复用，报告只能来自断点。
    registry.register("qa", ArtifactStage(name="qa"), replace=True)
    harness.services.retry(child.job_id, mode="stage")
    harness.run_all()
    assert harness.repository.find_report(child.job_id) is not None


# ------------------------------------------------------------------ 事件
def test_stage_events_are_emitted_with_progress(tmp_path: Path) -> None:
    harness = build_harness(tmp_path, stages=build_registry(render={"failures": 1}))
    project_id = harness.new_project()
    harness.services.submit_build(project_id)
    harness.runner.run_once()
    child = harness.repository.list_jobs(kind="document")[0]
    stage_events = [
        event for event in harness.runner.events.since(child.job_id, 0) if event.type == "stage"
    ]
    # 三个已跑阶段各两条（running + 终态）+ 失败阶段的一条终态。
    assert [event.payload["stage"] for event in stage_events].count("ingest") == 2
    assert any(
        event.payload.get("status") == "failed" for event in stage_events
    ), "失败必须有一条终态事件，否则界面永远显示「运行中」"


def test_event_ids_are_monotonic_per_job(harness: Harness) -> None:
    project_id = harness.new_project()
    orchestration_id = harness.services.submit_build(project_id).job_id
    harness.run_all()
    child = harness.repository.get_job(orchestration_id).child_job_ids[0]
    ids = [event.event_id for event in harness.runner.events.since(child, 0)]
    assert ids == sorted(ids)
    assert len(set(ids)) == len(ids)


def test_events_survive_a_process_restart(harness: Harness) -> None:
    """事件**先落库再推送**的可观测后果：重启后 ``Last-Event-ID`` 仍然有效。"""
    project_id = harness.new_project()
    orchestration_id = harness.services.submit_build(project_id).job_id
    harness.run_all()
    child = harness.repository.get_job(orchestration_id).child_job_ids[0]
    persisted = harness.runner.events.since(child, 0)
    assert persisted

    # 新进程：全新的仓储 + 全新的传内存游标，只有磁盘上的事件文件是共享的。
    restarted = build_harness(harness.root, clock=harness.clock)
    assert restarted.runner.restore_events() >= 1
    restored = restarted.runner.events.since(child, 0)
    assert [event.event_id for event in restored] == [event.event_id for event in persisted]
    # 续推语义成立：报"我收到 7 号"，服务端只补 8 号之后。
    mid = restored[len(restored) // 2].event_id
    tail = restarted.runner.events.since(child, mid)
    assert all(event.event_id > mid for event in tail)
    assert len(tail) == len([e for e in persisted if e.event_id > mid])


def test_restore_events_on_an_empty_root_is_a_noop(tmp_path: Path) -> None:
    fresh = build_harness(tmp_path / "空")
    assert fresh.runner.restore_events() == 0


def test_one_corrupt_event_file_does_not_lose_the_others(harness: Harness) -> None:
    """**逐个作业隔离**：一个坏文件只让它自己的续推失效，不该拖垮全站。

    整批一起失败会把"一个坏文件"放大成"所有作业重启后都重头补推"，
    而启动日志里只有一条笼统的警告，连是谁坏的都说不出来。
    """
    project_id = harness.new_project()
    orchestration_id = harness.build(project_id)
    child = harness.repository.get_job(orchestration_id).child_job_ids[0]
    broken = harness.root / "jobs" / "j_broken"
    broken.mkdir(parents=True, exist_ok=True)
    (broken / "events.jsonl").write_text("这不是 JSON\n", encoding="utf-8")

    # 模拟进程重启：全新的内存游标，只有磁盘上的事件文件是共享的。
    restarted = build_harness(harness.root, clock=harness.clock)
    assert restarted.runner.restore_events() >= 1, "好的那些必须仍然恢复"
    assert restarted.runner.events.since(child, 0), "被拖垮了：好作业的游标也丢了"


def test_restore_events_requires_a_fresh_log(harness: Harness) -> None:
    """重复 restore 会报"回放不连续"——那是**刻意的**，不该被"将就"掉。

    已有内存序列的日志被告知"从头再来一遍"，说明调用方把重启恢复当成了常规操作；
    将就只会让同一条事件有两个 id，而两个 id 的续推比报错难查得多。
    """
    project_id = harness.new_project()
    harness.build(project_id)
    with pytest.raises(EngineError) as caught:
        harness.runner.restore_events()
    assert caught.value.code == ErrorCode.CHECKPOINT_MISSING


def test_event_files_land_under_each_job_directory(harness: Harness) -> None:
    project_id = harness.new_project()
    orchestration_id = harness.build(project_id)
    child = harness.repository.get_job(orchestration_id).child_job_ids[0]
    path = harness.root / "jobs" / child / "events.jsonl"
    assert path.is_file()
    first = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    assert first["job_id"] == child


# ----------------------------------------------------------- 输入快照与载荷
def test_payload_carries_handles_not_contents(harness: Harness) -> None:
    """编排只把上游快照**原样递下去**（FR-M7-15）：这里放句柄，不放内容。"""
    project_id = harness.new_project()
    harness.services.submit_build(project_id)
    child = harness.repository.list_jobs(kind="document")[0]
    payload = harness.runner.build_payload(child)
    assert payload["template_ref"] == "demo-package@1"
    assert payload["profile_id"] == "demo-profile"
    assert payload["config_version"] == 1
    assert payload["inputs_hash"] == child.inputs_hash
    assert payload["retention"]["artifacts_days"] == harness.settings.retention_artifacts_days
    assert "documents" not in payload, "配置内容不该被复制进载荷"


def test_workspace_lives_under_the_job_directory(harness: Harness) -> None:
    """工作区就是 ``jobs/<job_id>/``（DD-11.7）；中间态落在 ``work/``。"""
    project_id = harness.new_project()
    orchestration_id = harness.services.submit_build(project_id).job_id
    harness.run_all()
    child = harness.repository.get_job(orchestration_id).child_job_ids[0]
    job_dir = harness.root / "jobs" / child
    assert job_dir.is_dir()
    assert (job_dir / "work" / "ingest.txt").is_file()


def test_child_job_ids_are_usable_as_path_segments(harness: Harness) -> None:
    """回归：子作业 id 曾被拼成 ``<编排id>:<doc_key>``，而 ``:`` 在 Windows 上非法。

    表现不是"id 难看"，是建目录直接 ``NotADirectoryError``——报错点在 ``mkdir``，
    离"id 怎么拼的"很远。这条用例从"这个 id 能不能当目录名"的角度钉住它。
    """
    from docfidelity.pipeline import path_segment

    project_id = harness.new_project()
    orchestration_id = harness.services.submit_build(project_id).job_id
    for child in harness.repository.get_job(orchestration_id).child_job_ids:
        assert path_segment(child, field="job_id") == child
        assert ":" not in child


def test_an_unsafe_doc_key_is_rejected_before_the_job_is_created(harness: Harness) -> None:
    """``doc_key`` 来自配置、内容不可控，故**发起构建前**就校验。

    先把演示资产登记进库（``seed_demo_assets``）：前置检查从本轮起真查"引用在不在库"，
    不登记的话这里的失败会变成 ``E_PRECHECK_FAILED``——那条拒绝是**对的**，但与本用例
    的题目（``doc_key`` 能不能当路径片段）无关，会把断言指到另一件事上。
    """
    from docfidelity.contracts.config import ProjectConfig
    from tests.fixtures.server_fixture import document_entry, seed_demo_assets

    seed_demo_assets(harness)
    project_id = harness.services.create_project(name="不安全键").project_id
    harness.services.put_config(
        project_id,
        ProjectConfig(project_id=project_id, author="t", documents=[document_entry("a/b")]),
        author="t",
    )
    with pytest.raises(EngineError) as caught:
        harness.services.submit_build(project_id)
    assert caught.value.code == ErrorCode.CONFIG_INVALID
    assert caught.value.where["doc_key"] == "a/b"


# ------------------------------------------------------------------- worker
def test_worker_tick_runs_one_job(harness: Harness) -> None:
    project_id = harness.new_project()
    harness.services.submit_build(project_id)
    loop = WorkerLoop(harness.runner)
    assert loop.tick() is True
    assert loop.tick() is True
    assert loop.tick() is False, "没有待推进的作业时 tick 必须回 False，否则空闲时忙等"


def test_worker_survives_a_broken_runner() -> None:
    """调度线程绝不能因单次异常退出——否则一个坏作业会让整个队列永久停摆。"""
    loop = WorkerLoop(_ExplodingRunner())  # type: ignore[arg-type]
    assert loop.tick() is False


def test_worker_start_stop_is_idempotent(harness: Harness) -> None:
    loop = WorkerLoop(harness.runner, idle_seconds=0.01)
    try:
        loop.start()
        loop.start()
        assert loop.running is True
    finally:
        loop.stop(timeout=2.0)
    assert loop.running is False
    loop.stop()  # 重复 stop 不得抛


# -------------------------------------------------------------------- 工具
@dataclass
class _RaisingStage:
    """抛**裸异常**的阶段体（验证编排的兜底，而不是 ``StageOutcome.error`` 那条路）。"""

    message: str
    calls: list[StageContext] = field(default_factory=list)

    def __call__(self, context: StageContext) -> StageOutcome:
        self.calls.append(context)
        raise RuntimeError(self.message)


@dataclass
class _ExplodingRunner:
    def run_once(self) -> Job:
        raise AssertionError("boom")
