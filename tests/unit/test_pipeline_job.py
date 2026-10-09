"""``pipeline.job``：状态机、编排聚合、失败隔离、项目锁。

P0-13 的验收判据是「作业可续跑」，而续跑首先要**在状态机上可行**——这正是本轮发现
契约缺陷的地方：``failed`` 曾被写成终态，于是"重试 / 从失败阶段续跑"在契约层就
不可表达。这里的 ``TestFailedIsResumable`` 就是那条判据的可执行形态。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from docfidelity.contracts.errors import ConfigurationError, EngineError, ErrorCode
from docfidelity.contracts.job import JOB_TRANSITIONS, Job
from docfidelity.pipeline import (
    ACTION_TRANSITIONS,
    RESUMABLE_ACTIONS,
    ChildSpec,
    JobStateMachine,
    LockLease,
    ProjectLockManager,
    actions_for,
    aggregate,
    child_progress,
    derive_children,
    rerun_failed,
    resolve_transition,
    submit_orchestration,
    with_aggregate,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 9, 21, 12, 0, 0, tzinfo=timezone.utc)


def _job(job_id: str = "j1", *, status: str = "queued", kind: str = "document") -> Job:
    return Job(
        job_id=job_id,
        kind=kind,  # type: ignore[arg-type]
        project_id="p1",
        created_at=NOW,
        inputs_hash="h1",
        status=status,  # type: ignore[arg-type]
    )


class TestActions:
    def test_action_table_covers_dd_11_2(self) -> None:
        assert set(ACTION_TRANSITIONS) == {
            ("queued", "lease"),
            ("running", "await_decision"),
            ("awaiting_decision", "decided"),
            ("running", "succeed"),
            ("running", "fail"),
            ("failed", "retry_stage"),
            ("failed", "resume_from"),
            ("failed", "restart"),
            ("canceled", "resume_from"),
            ("canceled", "restart"),
        }

    def test_every_action_target_is_permitted_by_the_contract_table(self) -> None:
        # 两张表必须一致：动作级越出状态级就会出现"按钮能点、状态改不了"
        for (status, action), target in ACTION_TRANSITIONS.items():
            assert target in JOB_TRANSITIONS[status], f"{status} --{action}--> {target}"

    def test_same_target_but_different_legality(self) -> None:
        # lease 与 resume_from 都回到 running，但合法来源完全不同 ——
        # 压成"动作 → 状态"就分不出这两种情形
        assert resolve_transition("queued", "lease") == "running"
        assert resolve_transition("failed", "lease") is None
        assert resolve_transition("failed", "resume_from") == "running"
        assert resolve_transition("queued", "resume_from") is None

    def test_actions_for_exposes_what_the_ui_may_offer(self) -> None:
        assert actions_for("queued") == ("cancel", "lease")
        assert actions_for("failed") == ("cancel", "restart", "resume_from", "retry_stage")
        assert actions_for("succeeded") == ()


class TestStateMachine:
    def test_legal_transition_returns_a_new_job(self) -> None:
        job = _job()
        leased = JobStateMachine.apply(job, "lease")
        assert leased.status == "running"
        assert job.status == "queued", "apply 不得就地改原对象"

    def test_illegal_transition_reports_the_allowed_set(self) -> None:
        with pytest.raises(EngineError) as caught:
            JobStateMachine.apply(_job(), "succeed")
        error = caught.value
        assert error.code == ErrorCode.JOB_CONFLICT
        assert error.where["from"] == "queued"
        assert "lease" in error.where["allowed"]

    def test_cancel_is_available_from_every_running_state(self) -> None:
        for status in ("queued", "running", "awaiting_decision", "failed", "partial_failure"):
            assert JobStateMachine.apply(_job(status=status), "cancel").status == "canceled"

    def test_succeeded_is_final(self) -> None:
        # 放弃一套**已完成**的产物不是"取消"（FR-M7-16：已完成子作业产物保持可用）
        assert JobStateMachine.transitions("succeeded") == frozenset()
        with pytest.raises(EngineError):
            JobStateMachine.apply(_job(status="succeeded"), "cancel")

    def test_can_apply_matches_apply(self) -> None:
        job = _job()
        assert JobStateMachine.can_apply(job, "lease")
        assert not JobStateMachine.can_apply(job, "succeed")


class TestFailedIsResumable:
    """契约层缺陷的回归：``failed`` 必须是可恢复状态。"""

    def test_retry_and_resume_are_legal(self) -> None:
        failed = _job(status="failed")
        assert JobStateMachine.apply(failed, "retry_stage").status == "running"
        assert JobStateMachine.apply(failed, "resume_from").status == "running"
        assert JobStateMachine.apply(failed, "restart").status == "queued"

    def test_a_failed_job_cannot_be_succeeded_directly(self) -> None:
        # 续跑不是"把失败改成成功"，必须重新执行
        with pytest.raises(EngineError):
            JobStateMachine.apply(_job(status="failed"), "succeed")

    def test_non_resumable_actions_are_still_rejected(self) -> None:
        with pytest.raises(EngineError):
            JobStateMachine.apply(_job(status="failed"), "lease")

    def test_resumable_action_set_is_the_one_used_by_rerun(self) -> None:
        assert {"retry_stage", "resume_from", "restart"} == RESUMABLE_ACTIONS


class TestAggregate:
    def test_all_succeeded(self) -> None:
        children = [_job(f"c{i}", status="succeeded") for i in range(3)]
        assert aggregate(children) == "succeeded"

    def test_pending_decision_wins(self) -> None:
        children = [_job("c1", status="succeeded"), _job("c2", status="awaiting_decision")]
        assert aggregate(children) == "awaiting_decision"
        assert aggregate([_job("c1", status="succeeded")], has_pending_decision=True) == (
            "awaiting_decision"
        )

    def test_a_running_child_keeps_the_orchestration_running(self) -> None:
        children = [_job("c1", status="succeeded"), _job("c2", status="running")]
        assert aggregate(children) == "running"

    def test_all_failed(self) -> None:
        assert aggregate([_job("c1", status="failed")]) == "failed"

    def test_all_canceled(self) -> None:
        assert aggregate([_job("c1", status="canceled")]) == "canceled"

    def test_partial_failure_does_not_discard_the_successful_children(self) -> None:
        # FR-M7-15 / FR-M7-16：单份失败不牵连其余
        children = [
            _job("c1", status="succeeded"),
            _job("c2", status="failed"),
            _job("c3", status="canceled"),
        ]
        assert aggregate(children) == "partial_failure"

    def test_empty_children_is_an_error_not_a_pass(self) -> None:
        # "没有子作业"若判成 succeeded，界面会显示"构建成功"却没有任何产物
        with pytest.raises(ConfigurationError):
            aggregate([])

    def test_child_progress_reports_done_total_failed_and_current(self) -> None:
        children = [
            _job("c1", status="succeeded"),
            _job("c2", status="failed"),
            _job("c3", status="running"),
        ]
        progress = child_progress(children)
        assert (progress.done, progress.total, progress.failed) == (2, 3, 1)
        assert progress.current_child == "c3"


class TestDerivation:
    def test_derive_children_sets_parent_and_doc_key(self) -> None:
        children = derive_children(
            parent_job_id="orch",
            project_id="p1",
            specs=[ChildSpec("c1", "detail"), ChildSpec("c2", "overview")],
            inputs_hash="h1",
            created_at=NOW,
        )
        assert [child.job_id for child in children] == ["c1", "c2"]
        assert all(child.parent_job_id == "orch" for child in children)
        assert [child.doc_key for child in children] == ["detail", "overview"]
        assert all(child.status == "queued" for child in children)

    def test_duplicate_child_id_is_rejected(self) -> None:
        # 撞车会让两份产物写进同一个工作区，续跑时互相覆盖
        with pytest.raises(ConfigurationError):
            derive_children(
                parent_job_id="orch",
                project_id="p1",
                specs=[ChildSpec("c1", "detail"), ChildSpec("c1", "overview")],
                inputs_hash="h1",
                created_at=NOW,
            )

    def test_empty_specs_is_rejected(self) -> None:
        with pytest.raises(ConfigurationError):
            derive_children(
                parent_job_id="orch", project_id="p1", specs=[], inputs_hash="h1", created_at=NOW
            )

    def test_submit_orchestration_links_children(self) -> None:
        orch, children = submit_orchestration(
            orchestration_id="orch",
            project_id="p1",
            specs=[ChildSpec("c1", "detail"), ChildSpec("c2", "overview")],
            inputs_hash="h1",
            created_at=NOW,
        )
        assert orch.kind == "orchestration"
        assert orch.child_job_ids == ["c1", "c2"]
        assert orch.progress is not None and orch.progress.total == 2
        assert [child.parent_job_id for child in children] == ["orch", "orch"]

    def test_with_aggregate_writes_derived_status_and_progress(self) -> None:
        orch, children = submit_orchestration(
            orchestration_id="orch",
            project_id="p1",
            specs=[ChildSpec("c1", "detail"), ChildSpec("c2", "overview")],
            inputs_hash="h1",
            created_at=NOW,
        )
        settled = [
            _job("c1", status="succeeded"),
            _job("c2", status="failed"),
        ]
        updated = with_aggregate(orch, settled)
        assert updated.status == "partial_failure"
        assert updated.progress is not None and updated.progress.failed == 1

    def test_rerun_failed_resets_only_the_failed_and_canceled(self) -> None:
        children = [
            _job("c1", status="succeeded"),
            _job("c2", status="failed"),
            _job("c3", status="canceled"),
        ]
        reset = rerun_failed(children)
        assert [child.job_id for child in reset] == ["c2", "c3"]
        assert all(child.status == "queued" for child in reset)
        assert children[0].status == "succeeded", "已成功的子作业不得被改动"

    def test_resume_from_keeps_the_job_running_for_checkpoint_reuse(self) -> None:
        reset = rerun_failed([_job("c1", status="failed")], action="resume_from")
        assert [child.status for child in reset] == ["running"]

    def test_rerun_failed_rejects_a_non_resuming_action(self) -> None:
        with pytest.raises(ConfigurationError):
            rerun_failed([_job("c1", status="failed")], action="lease")


class TestProjectLocks:
    def test_first_job_takes_the_lease(self) -> None:
        locks = ProjectLockManager(ttl_seconds=60)
        outcome = locks.acquire("p1", "j1", now=NOW)
        assert outcome.acquired
        assert outcome.holder_job_id == "j1"
        assert outcome.lease is not None and outcome.lease.expires_at == NOW + timedelta(seconds=60)

    def test_second_job_queues_behind_the_holder(self) -> None:
        locks = ProjectLockManager(ttl_seconds=60)
        locks.acquire("p1", "j1", now=NOW)
        outcome = locks.acquire("p1", "j2", now=NOW)
        assert not outcome.acquired
        assert outcome.holder_job_id == "j1"
        assert outcome.queue_position == 1
        assert locks.next_waiter("p1") == "j2"

    def test_same_job_renews_its_lease(self) -> None:
        locks = ProjectLockManager(ttl_seconds=60)
        first = locks.acquire("p1", "j1", now=NOW)
        renewed = locks.acquire("p1", "j1", now=NOW + timedelta(seconds=30))
        assert renewed.acquired
        assert first.lease is not None and renewed.lease is not None
        assert renewed.lease.expires_at > first.lease.expires_at

    def test_release_frees_the_project_for_the_next_job(self) -> None:
        locks = ProjectLockManager(ttl_seconds=60)
        locks.acquire("p1", "j1", now=NOW)
        locks.acquire("p1", "j2", now=NOW)
        assert locks.release("p1", "j1", now=NOW)
        assert locks.acquire("p1", "j2", now=NOW).acquired

    def test_a_non_holder_cannot_release(self) -> None:
        locks = ProjectLockManager(ttl_seconds=60)
        locks.acquire("p1", "j1", now=NOW)
        assert not locks.release("p1", "j2", now=NOW)
        assert locks.holder("p1", now=NOW) is not None

    def test_expired_lease_is_reclaimed(self) -> None:
        # worker 崩溃后锁不会被释放；没有 expires_at，项目会永久停在"排队中"
        locks = ProjectLockManager(ttl_seconds=60)
        locks.acquire("p1", "j1", now=NOW)
        assert locks.holder("p1", now=NOW + timedelta(seconds=61)) is None
        assert locks.acquire("p1", "j2", now=NOW + timedelta(seconds=61)).acquired

    def test_projects_do_not_block_each_other(self) -> None:
        locks = ProjectLockManager(ttl_seconds=60)
        assert locks.acquire("p1", "j1", now=NOW).acquired
        assert locks.acquire("p2", "j2", now=NOW).acquired

    def test_queue_position_zero_when_not_waiting(self) -> None:
        assert ProjectLockManager().queue_position("p1", "j1") == 0

    def test_non_positive_ttl_is_rejected(self) -> None:
        with pytest.raises(ConfigurationError):
            ProjectLockManager(ttl_seconds=0)

    def test_lease_expiry_is_a_pure_function_of_time(self) -> None:
        lease = LockLease(
            project_id="p1",
            holder_job_id="j1",
            leased_at=NOW,
            expires_at=NOW + timedelta(seconds=5),
        )
        assert not lease.expired(now=NOW)
        assert lease.expired(now=NOW + timedelta(seconds=5))
