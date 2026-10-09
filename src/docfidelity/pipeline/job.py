"""作业状态机、编排派生与聚合、失败隔离、项目锁（DD-11.2 / 11.4 / 11.5）。

两条不变式
----------
1. **作业状态迁移只有 :meth:`JobStateMachine.apply` 一个入口**（DD-11.2）。
   ``contracts.job.Job`` 的 ``status`` 字段本身可写（它是个数据模型），但引擎里
   任何"改状态"的位置都必须经过这里——否则"合法迁移"就有了第二个判定口径，
   而两个口径迟早会给出不同结论。
2. **编排作业的状态是派生值**，由 :func:`aggregate` 从子作业算出（DD-11.2 把这件事
   明确交给 ``orchestrator.aggregate``）。它不是一次生命周期迁移，故不走 ``apply``；
   两个写入点都在本模块内，"唯一写入点"不变式依然成立。

失败隔离（FR-M7-15 / FR-M7-16）
-------------------------------
"单份失败不牵连其余文档"落在三件事上：① :func:`aggregate` **只看子作业状态**，
不会因为一份失败就把编排判成 failed；② :func:`rerun_failed` 只重置失败/取消的子作业，
已成功子作业的产物（与其断点）一律不动；③ :func:`child_progress` 如实给出
``done/total/failed``，让界面能定位到具体是哪一份卡住。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Final, Literal

from docfidelity.contracts.errors import ConfigurationError, EngineError, ErrorCode
from docfidelity.contracts.job import (
    JOB_TRANSITIONS,
    ORCHESTRATION_AGGREGATION_RULE,
    ChildProgress,
    Job,
    JobStatus,
)

__all__ = [
    "ACTION_TRANSITIONS",
    "CANCEL_ACTION",
    "FAILURE_ISOLATION_RULE_ID",
    "ORCHESTRATION_RULE_ID",
    "RESUMABLE_ACTIONS",
    "JobAction",
    "JobStateMachine",
    "LockLease",
    "LockOutcome",
    "ProjectLockManager",
    "actions_for",
    "aggregate",
    "child_progress",
    "derive_children",
    "rerun_failed",
    "resolve_transition",
    "submit_orchestration",
    "with_aggregate",
]

#: 需求条目：FR-M7-15（编排作业 + 子作业，单份失败不牵连其余）。
ORCHESTRATION_RULE_ID: Final = "FR-M7-15"
#: 需求条目：FR-M7-16（失败隔离与局部重跑）。
FAILURE_ISOLATION_RULE_ID: Final = "FR-M7-16"

JobAction = Literal[
    "cancel",
    "lease",
    "await_decision",
    "decided",
    "succeed",
    "fail",
    "retry_stage",
    "resume_from",
    "restart",
]

#: 动作级迁移表：``(当前状态, 动作) -> 目标状态``。**DD-11.2 的逐条落地**。
#:
#: 为什么不写成"动作 → 目标状态"：**同一个目标状态可能对应不同合法性**。
#: ``lease``（worker 领走一个排队中的作业）与 ``resume_from``（续跑一个失败作业）
#: 都回到 ``running``，但前者只对 ``queued`` 合法、后者只对 ``failed`` / ``canceled``
#: 合法。压成"动作 → 状态"就再也分不出这两种情形，"重试一个还在排队的作业"
#: 这类无意义调用会被放过——而它正是"重复派单"的入口。
ACTION_TRANSITIONS: Final[dict[tuple[str, str], JobStatus]] = {
    ("queued", "lease"): "running",
    ("running", "await_decision"): "awaiting_decision",
    ("awaiting_decision", "decided"): "running",
    ("running", "succeed"): "succeeded",
    ("running", "fail"): "failed",
    ("failed", "retry_stage"): "running",
    ("failed", "resume_from"): "running",
    ("failed", "restart"): "queued",
    # DD-11.2 的表里没有这两条，但 DD-11.4 的 ``rerun_failed`` 明确把
    # ``("failed", "canceled")`` 一并列为要重置的子作业——"只重跑失败子作业"
    # 必须把被取消的也算进去。同一节文档内部冲突时取**并集**，否则
    # "从取消态恢复"在动作层不可表达，而它恰恰是"局部重跑"的一半。
    ("canceled", "resume_from"): "running",
    ("canceled", "restart"): "queued",
}

#: 通配动作名：DD-11.2 的 ``("*", "cancel")``。
#:
#: 可否取消不由本表决定，而由契约的状态级表决定（``"canceled" in JOB_TRANSITIONS[status]``）——
#: 让"取消边"只有一处定义，就不会出现"动作表说能取消、状态表说不能"的分裂。
CANCEL_ACTION: Final = "cancel"

#: 可从失败/取消态恢复的动作（FR-M7-03 / FR-M7-16 的"重试 / 从失败阶段续跑"）。
RESUMABLE_ACTIONS: Final[frozenset[str]] = frozenset({"retry_stage", "resume_from", "restart"})

#: 尚未进入终态的作业状态；有它们在场，编排作业就是"还在跑"。
_IN_FLIGHT_STATUSES: Final[frozenset[str]] = frozenset({"queued", "running"})

#: 终态里表示"没成"的两种。
_UNSUCCESSFUL_STATUSES: Final[frozenset[str]] = frozenset({"failed", "canceled"})


def actions_for(status: JobStatus) -> tuple[str, ...]:
    """某状态下的可用动作（界面据此决定按钮可用性，别再自己推一遍）。"""
    names = {action for (state, action) in ACTION_TRANSITIONS if state == status}
    if "canceled" in JOB_TRANSITIONS[status]:
        names.add(CANCEL_ACTION)
    return tuple(sorted(names))


def resolve_transition(status: JobStatus, action: str) -> JobStatus | None:
    """``(状态, 动作) → 目标状态``；该组合不合法时返回 ``None``。

    两张表不一致（动作级越出状态级）属**实现缺陷**，抛 ``E_INTERNAL`` 而不是返回
    ``None``：静默当成"不合法"会让一个内部 bug 表现为"按钮点不动"，排查方向全错。
    """
    if action == CANCEL_ACTION:
        return "canceled" if "canceled" in JOB_TRANSITIONS[status] else None
    target = ACTION_TRANSITIONS.get((status, action))
    if target is None:
        return None
    if target not in JOB_TRANSITIONS[status]:
        raise EngineError(
            ErrorCode.INTERNAL,
            f"动作级迁移表越出状态级迁移表：{status} --{action}--> {target}",
            where={
                "status": status,
                "action": action,
                "target": target,
                "allowed": sorted(JOB_TRANSITIONS[status]),
            },
            remediation=["修正 pipeline.job.ACTION_TRANSITIONS 或 contracts.job.JOB_TRANSITIONS"],
        )
    return target


class JobStateMachine:
    """作业状态机（DD-11.2）。**改一个作业的状态，只能走 :meth:`apply`。**"""

    @staticmethod
    def transitions(status: JobStatus) -> frozenset[str]:
        """某状态下允许到达的状态集合（来自契约，全项目唯一一份）。"""
        return JOB_TRANSITIONS[status]

    @staticmethod
    def actions(status: JobStatus) -> tuple[str, ...]:
        return actions_for(status)

    @staticmethod
    def can_apply(job: Job, action: str) -> bool:
        """这次迁移合法吗。界面据此决定"重试"按钮是否可点。"""
        return resolve_transition(job.status, action) is not None

    @staticmethod
    def apply(job: Job, action: str) -> Job:
        """把 ``action`` 应用到作业上，返回**新**作业对象（不改原对象）。

        Raises
        ------
        EngineError
            ``E_JOB_CONFLICT``：该动作在当前状态下不合法。
        """
        target = resolve_transition(job.status, action)
        if target is None:
            raise EngineError(
                ErrorCode.JOB_CONFLICT,
                f"作业 {job.job_id} 在状态 {job.status} 下不能执行动作 {action!r}",
                where={
                    "job_id": job.job_id,
                    "from": job.status,
                    "action": action,
                    "allowed": list(actions_for(job.status)),
                },
                remediation=[
                    "按 allowed 里的动作重试；" "若确需重跑已完成的作业，请新建作业而不是回退状态"
                ],
            )
        return job.model_copy(update={"status": target})


def child_progress(children: Sequence[Job]) -> ChildProgress:
    """编排作业的进度视图（``done/total/failed`` + 当前子作业）。"""
    total = len(children)
    failed = sum(1 for child in children if child.status in _UNSUCCESSFUL_STATUSES)
    done = sum(1 for child in children if child.status not in _IN_FLIGHT_STATUSES)
    current = next(
        (child.job_id for child in children if child.status in _IN_FLIGHT_STATUSES),
        None,
    )
    return ChildProgress(done=done, total=total, failed=failed, current_child=current)


def aggregate(children: Sequence[Job], *, has_pending_decision: bool = False) -> JobStatus:
    """按 DD-11.2 的规则算出编排作业的状态。

    规则（顺序即优先级）::

        存在待裁决 / 有子作业待裁决  -> awaiting_decision
        还有子作业在跑              -> running
        全部 succeeded              -> succeeded
        全部 failed                 -> failed
        全部 canceled               -> canceled
        其余混合                    -> partial_failure

    **不因单份失败丢弃其他产物**：只要有成功的子作业，结果就是 ``partial_failure``
    而不是 ``failed``，界面据此仍然把成功那几份的产物呈现出来（FR-M7-16）。
    """
    if not children:
        raise ConfigurationError(
            "编排作业没有子作业，无法聚合状态",
            where={"rule": ORCHESTRATION_AGGREGATION_RULE},
            remediation=["发起构建时按文档族派生至少一个子作业（FR-M7-15）"],
        )

    statuses = [child.status for child in children]
    if has_pending_decision or any(status == "awaiting_decision" for status in statuses):
        return "awaiting_decision"
    if any(status in _IN_FLIGHT_STATUSES for status in statuses):
        return "running"
    if all(status == "succeeded" for status in statuses):
        return "succeeded"
    if all(status == "failed" for status in statuses):
        return "failed"
    if all(status == "canceled" for status in statuses):
        return "canceled"
    return "partial_failure"


@dataclass(frozen=True)
class ChildSpec:
    """一个待派生的文档子作业。"""

    job_id: str
    doc_key: str
    doc_type: str | None = None


def derive_children(
    *,
    parent_job_id: str,
    project_id: str,
    specs: Sequence[ChildSpec],
    inputs_hash: str,
    created_at: datetime,
    initiated_by: str | None = None,
) -> list[Job]:
    """按文档族派生等量子作业（每份文档一个，FR-M7-15）。

    子作业 id 由调用方给出（``specs``）而不是这里生成：内核禁用 ``random``，
    而且"同一份输入快照派生出同一组作业 id"是可复现构建的前提。

    ``initiated_by`` 只随留痕走，不参与派生：一次构建的发起人对**每一份**子作业
    都是同一个人，故它是复制的而不是派生的。
    """
    if not specs:
        raise ConfigurationError(
            "派生作业需要至少一个文档条目",
            where={"parent_job_id": parent_job_id},
            remediation=["在项目配置的文档条目中声明至少一份文档"],
        )
    seen: set[str] = set()
    for spec in specs:
        if spec.job_id in seen:
            raise ConfigurationError(
                f"子作业 id 重复：{spec.job_id!r}",
                where={"parent_job_id": parent_job_id, "job_id": spec.job_id},
                remediation=["子作业 id 必须唯一，否则断点与产物会互相覆盖"],
            )
        seen.add(spec.job_id)
    return [
        Job(
            job_id=spec.job_id,
            parent_job_id=parent_job_id,
            kind="document",
            project_id=project_id,
            doc_key=spec.doc_key,
            doc_type=spec.doc_type,
            created_at=created_at,
            inputs_hash=inputs_hash,
            initiated_by=initiated_by,
        )
        for spec in specs
    ]


def submit_orchestration(
    *,
    orchestration_id: str,
    project_id: str,
    specs: Sequence[ChildSpec],
    inputs_hash: str,
    created_at: datetime,
    initiated_by: str | None = None,
) -> tuple[Job, list[Job]]:
    """派生编排作业及其子作业（DD-11.4 的 ``submit_orchestration``）。

    编排作业只冻结输入快照、聚合进度与汇总报告，**不亲自产出文档**（FR-M7-15）。
    """
    children = derive_children(
        parent_job_id=orchestration_id,
        project_id=project_id,
        specs=specs,
        inputs_hash=inputs_hash,
        created_at=created_at,
        initiated_by=initiated_by,
    )
    orchestration = Job(
        job_id=orchestration_id,
        kind="orchestration",
        project_id=project_id,
        child_job_ids=[child.job_id for child in children],
        created_at=created_at,
        inputs_hash=inputs_hash,
        initiated_by=initiated_by,
        progress=child_progress(children),
    )
    return orchestration, children


def with_aggregate(
    orchestration: Job, children: Sequence[Job], *, has_pending_decision: bool = False
) -> Job:
    """把派生出的状态与进度写回编排作业（派生值的**唯一**写入点）。"""
    return orchestration.model_copy(
        update={
            "status": aggregate(children, has_pending_decision=has_pending_decision),
            "progress": child_progress(children),
        }
    )


def rerun_failed(children: Sequence[Job], *, action: JobAction = "restart") -> list[Job]:
    """只重置失败/取消的子作业（DD-11.4 的 ``rerun_failed``）。

    ``action="restart"`` 表示"只重跑失败子作业"（回到 ``queued``，从头来）；
    ``action="resume_from"`` 表示"从失败阶段续跑"（回到 ``running``，断点复用）。
    已成功的子作业**原样返回在结果之外**——它们既不重跑也不被改状态，产物保持可用
    （FR-M7-16）。
    """
    if action not in RESUMABLE_ACTIONS:
        raise ConfigurationError(
            f"rerun_failed 只接受恢复类动作，收到 {action!r}",
            where={"action": action, "allowed": sorted(RESUMABLE_ACTIONS)},
            remediation=["用 restart（只重跑失败子作业）或 resume_from（从失败阶段续跑）"],
        )
    return [
        JobStateMachine.apply(child, action)
        for child in children
        if child.status in _UNSUCCESSFUL_STATUSES
    ]


# --------------------------------------------------------------------- 项目锁
@dataclass(frozen=True)
class LockLease:
    """项目锁租约（DD-11.5）。``expires_at`` 是防死锁的关键。"""

    project_id: str
    holder_job_id: str
    leased_at: datetime
    expires_at: datetime

    def expired(self, *, now: datetime) -> bool:
        return self.expires_at <= now


@dataclass(frozen=True)
class LockOutcome:
    """尝试获取项目锁的结果。"""

    acquired: bool
    lease: LockLease | None = None
    holder_job_id: str | None = None
    queue_position: int = 0


class ProjectLockManager:
    """同项目串行的租约锁（DD-11.5）。

    为什么要租约而不是单纯"加锁"：worker 崩溃时锁不会被释放，没有 ``expires_at``
    的项目会永久停在 ``queued``，而界面上只看到"排队中"。租约超时即回收，
    把"进程没了"和"任务正忙"区分开。

    本实现是**单进程**的（内存字典）。多 worker 场景由 ``server`` 用数据库事务
    （``SELECT ... FOR UPDATE SKIP LOCKED``）落实同一条语义——口径一致，落点不同。
    """

    def __init__(self, *, ttl_seconds: float = 900.0) -> None:
        if ttl_seconds <= 0:
            raise ConfigurationError(
                f"项目锁租约时长必须为正，收到 {ttl_seconds!r}",
                where={"ttl_seconds": ttl_seconds},
                remediation=["取一个大于 0 的秒数"],
            )
        self._ttl = timedelta(seconds=ttl_seconds)
        self._leases: dict[str, LockLease] = {}
        self._waiting: dict[str, list[str]] = {}

    @staticmethod
    def _now(now: datetime | None) -> datetime:
        return now or datetime.now(tz=timezone.utc)

    def _purge(self, project_id: str, now: datetime) -> None:
        lease = self._leases.get(project_id)
        if lease is not None and lease.expired(now=now):
            del self._leases[project_id]

    def acquire(self, project_id: str, job_id: str, *, now: datetime | None = None) -> LockOutcome:
        """尝试获取（或续租）项目锁。"""
        moment = self._now(now)
        self._purge(project_id, moment)

        lease = self._leases.get(project_id)
        if lease is None:
            granted = LockLease(
                project_id=project_id,
                holder_job_id=job_id,
                leased_at=moment,
                expires_at=moment + self._ttl,
            )
            self._leases[project_id] = granted
            self._waiting.pop(project_id, None)
            return LockOutcome(acquired=True, lease=granted, holder_job_id=job_id)

        if lease.holder_job_id == job_id:
            renewed = LockLease(
                project_id=project_id,
                holder_job_id=job_id,
                leased_at=lease.leased_at,
                expires_at=moment + self._ttl,
            )
            self._leases[project_id] = renewed
            return LockOutcome(acquired=True, lease=renewed, holder_job_id=job_id)

        queue = self._waiting.setdefault(project_id, [])
        if job_id not in queue:
            queue.append(job_id)
        return LockOutcome(
            acquired=False,
            holder_job_id=lease.holder_job_id,
            queue_position=queue.index(job_id) + 1,
        )

    def release(self, project_id: str, job_id: str, *, now: datetime | None = None) -> bool:
        """释放自己持有的锁。非持有者调用返回 ``False``（不得替别人解锁）。"""
        moment = self._now(now)
        self._purge(project_id, moment)
        lease = self._leases.get(project_id)
        if lease is None or lease.holder_job_id != job_id:
            return False
        del self._leases[project_id]
        queue = self._waiting.get(project_id)
        if queue and job_id in queue:
            queue.remove(job_id)
        return True

    def holder(self, project_id: str, *, now: datetime | None = None) -> LockLease | None:
        """当前持有者（已过期的租约视为无持有者）。"""
        moment = self._now(now)
        self._purge(project_id, moment)
        return self._leases.get(project_id)

    def next_waiter(self, project_id: str) -> str | None:
        """队首等待者——调度器下一个该尝试唤醒的作业。"""
        queue = self._waiting.get(project_id)
        return queue[0] if queue else None

    def queue_position(self, project_id: str, job_id: str) -> int:
        queue = self._waiting.get(project_id)
        if not queue or job_id not in queue:
            return 0
        return queue.index(job_id) + 1
