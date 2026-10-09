"""裁决提交（P10）与裁决待办聚合（P12）—— ``Services`` 的裁决职责。

为什么在服务层而不在内核
------------------------
"谁有权豁免、理由够不够"是**治理**问题，不是引擎问题：同一份产物在不同组织里由不同的人
签。内核提供三件东西——判定的可寻址标识（``Finding.finding_id``）、结论算法
（``contracts.qa.verdict_of`` / :meth:`~docfidelity.contracts.qa.Report.recompute`）、
作业状态机。本模块把它们串成一次可留痕的人工作业，**不新造任何判定**。

三条纪律
--------
① **规范动作只有三种**（``accept`` / ``configure`` / ``waive``，FR-M7-06 / NFR-15）。
   取值直接取自 :data:`~docfidelity.contracts.qa.AdjudicationAction`（``get_args``），
   本模块不另立集合——多一种动作就多一条绕过门禁的路径。
② **留痕落在报告上**（``Report.adjudications``）。裁决的对象是"这份报告里的这一条判定"
   或"这个作业的这一个决策"，而报告正是那份不可变产物的人工批注面。另开一张表会让
   ``GET /jobs/{jid}/report`` 与 ``GET /inbox`` 各读一处，而两处迟早不一致。
③ **"待裁决"的判据只有一条：没有裁决留痕**。出现过一次裁决就是已办结——``accept`` /
   ``configure`` 不改门禁结论（只有 ``waive`` 改），但它们同样**是**一次办理；
   把"未豁免的 blocker"继续留在待办里，会让待办徽标永远清不掉。

**已办结 ≠ 可通过**：``accept`` 之后判定仍在 ``open``、``verdict`` 仍可能是 ``blocked``，
报告卡上照样显示"阻断"。待办列表回答"还有谁要看"，``can_finalize`` 回答"能不能定稿"，
两件事各有唯一出口，不合并。

待裁决项有哪两类
----------------
* **门禁判定**（``Finding.finding_id``）：只取**未豁免的 blocker**。major / minor 是
  信息而非阻塞，把它们全塞进待办会让徽标变成一个噪音计数器（判定总共几十条）。
* **决策项**（``Decision.decision_id``，``status == "pending"``）：低置信 / 兜底 /
  路由给人工的那些。**它们才是作业进入 ``awaiting_decision`` 的原因**（NFR-26 / 验收 39）。

作业状态怎么走
--------------
一次裁决之后，本作业若还在 ``awaiting_decision`` 且**再没有未办结的项**，就按状态机
``decided`` 回到 ``running``（worker 会认领并复用断点收尾）。两种情形**不**推进，
且都在响应里说明原因：

* ``action="configure"``——"改配置后重来"的那次构建需要一个新的 ``inputs_hash`` 才能让
  旧断点失效，而只有提交构建时才算得出它。此刻把作业置回 ``running``，worker 会用**旧**
  的 ``inputs_hash`` 复用全部断点，最后一个阶段都不重跑却报 ``succeeded``——产物看起来
  是"按新配置做好了"，其实还是旧的。这是最坏的一种成功，故宁可不推进。
* 报告仍然 ``blocked``——``blocked`` 的唯一出口是 :func:`~docfidelity.qa.can_finalize`
  （FR-M5-16），裁决面不得绕过它把作业推成"成功"。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Final, Literal, get_args

from docfidelity.contracts.errors import EngineError, ErrorCode
from docfidelity.contracts.job import Decision, Job
from docfidelity.contracts.qa import (
    Adjudication,
    AdjudicationAction,
    Finding,
    Report,
    Waiver,
)
from docfidelity.pipeline import (
    JobStateMachine,
    decisions_from_events,
    pending_decisions_from_events,
    with_aggregate,
)

from .ids import new_id
from .jobs import JobRunner
from .storage import Repository

__all__ = [
    "ADJUDICATION_ACTIONS",
    "AdjudicationMixin",
    "AdjudicationResult",
    "PendingSubject",
]

#: 规范动作的闭集，**取自契约**（``get_args``）而不是手抄一遍。
ADJUDICATION_ACTIONS: Final[frozenset[str]] = frozenset(get_args(AdjudicationAction))

#: 待裁决项的类别（``GET /inbox`` 的一行属于哪一类）。
AdjudicationKind = Literal["finding", "decision"]

#: 作业推进的结果（响应里要如实说明，界面不自行推断，NFR-22）。
AdvanceOutcome = Literal["advanced", "remaining", "blocked", "configure", "not_awaiting"]

#: ``advance == "blocked"`` 时给使用者的说明。
ADVANCE_BLOCKED_NOTE: Final = (
    "裁决已记录；报告仍有未豁免的 blocker，作业保持在「待裁决」。"
    "若要解除阻断，请对相应判定提交 waive（豁免）并在理由中说明责任归属。"
)


@dataclass(frozen=True)
class PendingSubject:
    """一条待裁决项（``GET /inbox`` 的一行）。"""

    subject_id: str
    kind: AdjudicationKind
    job_id: str
    project_id: str
    title: str
    severity: str | None = None
    detail: str | None = None
    created_at: datetime | None = None
    #: 该项此刻能否被提交裁决。为 ``False`` 时 :attr:`unresolvable_reason` 必非空
    #: ——界面按它渲染"为什么点不了"（硬约束 15），而不是自己猜。
    resolvable: bool = True
    unresolvable_reason: str | None = None


@dataclass(frozen=True)
class AdjudicationResult:
    """一次裁决的结果（``POST /findings/{fid}/decisions`` 的响应面）。"""

    adjudication: Adjudication
    report: Report
    job: Job
    finding: Finding | None = None
    waiver: Waiver | None = None
    advance: AdvanceOutcome = "not_awaiting"
    note: str = ""
    #: 该作业**仍未办结**的待裁决项标识（含刚提交的这一条之外的所有项）。
    remaining: tuple[str, ...] = ()


class AdjudicationMixin:
    """裁决与待办聚合。宿主 = :class:`server.services.Services`。

    与 :class:`~server.maintenance.MaintenanceMixin` 同构：这些用例都要读仓储、事件日志
    与运行器的工作区，做成混合类才不必在每个调用点重传宿主的字段。依赖的宿主字段在
    ``TYPE_CHECKING`` 里声明（不能声明成真属性，见 ``maintenance`` 模块的说明）。
    """

    if TYPE_CHECKING:
        repository: Repository
        runner: JobRunner
        now: Callable[[], datetime]

    # ============================================================== 待办聚合
    def inbox(self, *, project_id: str | None = None) -> list[PendingSubject]:
        """聚合待裁决项（DD-13.2 的 ``GET /inbox``）。

        扫描**作业**而不是扫描报告：决策项只存在于事件里，而"哪份作业有报告"是另一回事
        （报告由 ``qa`` 阶段产出，阶段未跑就没有）。以作业为轴，两类待办才在同一个循环里
        齐备。次序按 ``(created_at, subject_id)`` 固定——待办列表每次刷新换个顺序，
        使用者会以为"刚才那条不见了"。
        """
        items: list[PendingSubject] = []
        for job in self.repository.list_jobs(project_id=project_id):
            if job.is_orchestration:
                # 编排作业自己不做决策、也不产报告：它的待办由子作业承载。
                continue
            report = self.repository.find_report(job.job_id)
            decided = (
                {item.subject_id for item in report.adjudications} if report is not None else set()
            )
            items.extend(self._finding_subjects(job, report, decided))
            items.extend(self._decision_subjects(job, report, decided))
        return sorted(items, key=lambda item: (item.created_at or _EPOCH, item.subject_id))

    def _finding_subjects(
        self, job: Job, report: Report | None, decided: set[str]
    ) -> list[PendingSubject]:
        if report is None:
            return []
        found: list[PendingSubject] = []
        for finding in report.findings:
            if finding.severity != "blocker" or finding.status == "waived":
                continue
            if finding.finding_id is None or finding.finding_id in decided:
                continue
            found.append(
                PendingSubject(
                    subject_id=finding.finding_id,
                    kind="finding",
                    job_id=job.job_id,
                    project_id=job.project_id,
                    title=f"[{finding.rule_id}] {finding.message}",
                    severity=finding.severity,
                    detail=finding.suggestion,
                    created_at=report.generated_at,
                )
            )
        return found

    def _decision_subjects(
        self, job: Job, report: Report | None, decided: set[str]
    ) -> list[PendingSubject]:
        found: list[PendingSubject] = []
        for decision in self.pending_decisions(job.job_id):
            if decision.decision_id in decided:
                continue
            resolvable = report is not None
            found.append(
                PendingSubject(
                    subject_id=decision.decision_id,
                    kind="decision",
                    job_id=job.job_id,
                    project_id=job.project_id,
                    title=f"[{decision.kind}] {decision.target}",
                    severity=None,
                    detail=decision.reason,
                    created_at=decision.created_at,
                    resolvable=resolvable,
                    unresolvable_reason=None if resolvable else _NO_LEDGER,
                )
            )
        return found

    # ============================================================== 裁决提交
    def adjudicate(
        self,
        subject_id: str,
        *,
        action: str,
        operator: str,
        reason: str | None = None,
    ) -> AdjudicationResult:
        """提交一次裁决（DD-13.2 的 ``POST /findings/{fid}/decisions``）。

        ``reason`` 只有 ``waive`` 强制要求：只有它改变门禁结论，也只有它需要留下
        "谁以什么理由放行的"（FR-M7-06 / NFR-15）。``accept`` / ``configure`` 给理由
        照收（留痕更完整），不给也不算错。
        """
        if action not in ADJUDICATION_ACTIONS:
            raise EngineError(
                ErrorCode.CONFIG_INVALID,
                f"未知的裁决动作：{action!r}",
                where={"action": action, "allowed": sorted(ADJUDICATION_ACTIONS)},
                remediation=["动作取 accept / configure / waive 三者之一"],
            )
        normalized = (reason or "").strip()
        if action == "waive" and not normalized:
            raise EngineError(
                ErrorCode.WAIVER_REASON_REQUIRED,
                "豁免必须给出理由：它是「谁以什么理由让一条阻断不阻断」的唯一记录",
                where={"subject_id": subject_id, "action": action},
                remediation=[
                    "在 reason 里写明豁免依据与责任人",
                    "若只是认可这条判定、不打算改变门禁结论，请改用 accept",
                ],
            )
        job, report, finding = self._locate(subject_id)
        moment = self.now()

        waiver: Waiver | None = None
        if action == "waive" and finding is not None:
            # 只有**判定**才产生 Waiver。``Waiver`` 是规则级的（``rule_id`` + 有效期 +
            # 作用域），它作用于门禁规则；给一个决策项硬编一个 ``rule_id`` 会让
            # ``Report.waivers`` 与 ``open_findings`` 的语义失真——那份豁免永远不会命中
            # 任何判定。故对决策项的 waive 只留痕（"以责任人身份放行，不采纳该决策"）。
            waiver = Waiver(
                waiver_id=new_id("wv_"),
                rule_id=finding.rule_id,
                reason=normalized,
                operator=operator,
                created_at=moment,
                scope="instance",
                target=subject_id,
            )
        adjudication = Adjudication(
            adjudication_id=new_id("adj_"),
            subject_id=subject_id,
            action=action,  # type: ignore[arg-type]
            operator=operator,
            reason=normalized or None,
            created_at=moment,
            waiver_id=waiver.waiver_id if waiver is not None else None,
        )
        updated = self._record(report, adjudication, waiver=waiver, subject_id=subject_id)
        self.repository.put_report(job.job_id, updated)

        remaining = self._unresolved(job, updated)
        moved, advance = self._advance(job, updated, action=action, remaining=remaining)
        settled = updated.findings
        return AdjudicationResult(
            adjudication=adjudication,
            report=updated,
            job=moved,
            finding=next(
                (item for item in settled if item.finding_id == subject_id),
                None,
            ),
            waiver=waiver,
            advance=advance,
            note=_NOTE[advance],
            remaining=tuple(remaining),
        )

    # ------------------------------------------------------------ 待办与读取
    def pending_decisions(self, job_id: str) -> tuple[Decision, ...]:
        """该作业事件里 ``status == "pending"`` 的决策（唯一实现在 ``agents.recorder``）。

        读事件而不是读某次运行的内存对象：内存那份只覆盖**本进程**见过的决策，
        而"这份作业还有哪些项在等人裁决"必须跨重启成立。事件是决策的持久形态。
        """
        return pending_decisions_from_events(self.runner.events.events(job_id), job_id=job_id)

    def decisions(self, job_id: str) -> tuple[Decision, ...]:
        """该作业的全部决策留痕（含已自动采用的），供报告与详情页展示。"""
        return decisions_from_events(self.runner.events.events(job_id), job_id=job_id)

    # ------------------------------------------------------------ 内部
    def _record(
        self,
        report: Report,
        adjudication: Adjudication,
        *,
        waiver: Waiver | None,
        subject_id: str,
    ) -> Report:
        """把一次裁决写进报告并**重算**结论与摘要。

        重算是必须的：``verdict`` 是"当前"结论，``summary`` 里也写着结论那句话。
        只改判定状态而不重算，报告会自相矛盾（"结论：阻断" + 判定全是 waived）。
        """
        findings = (
            report.findings
            if waiver is None
            else [
                item.model_copy(update={"status": "waived", "waiver_id": waiver.waiver_id})
                if item.finding_id == subject_id
                else item
                for item in report.findings
            ]
        )
        waivers = list(report.waivers)
        if waiver is not None:
            waivers.append(waiver)
        return report.model_copy(
            update={
                "findings": findings,
                "waivers": waivers,
                "adjudications": [*report.adjudications, adjudication],
            }
        ).recompute()

    def _locate(self, subject_id: str) -> tuple[Job, Report, Finding | None]:
        """``subject_id`` → (作业, 报告, 判定或 ``None``)。

        两类待办共用一条路径（见模块文档）。判定标识**跨作业唯一**由 ``report_id``
        保证（它由判定内容 + 时刻派生），但两份内容完全相同、同一秒产出的报告会撞号
        ——那种情形下**报错**而不是随便挑一份：静默挑中一份会让"我豁免了这条"变成
        "另一份文档里的同名判定被豁免了"，而那件事在界面上完全看不出来。
        """
        matches = [
            (report, finding)
            for report in self.repository.list_reports()
            for finding in report.findings
            if finding.finding_id == subject_id
        ]
        if len(matches) > 1:
            raise EngineError(
                ErrorCode.STATE_INVALID,
                f"待裁决项标识在 {len(matches)} 份报告里都存在，无法确定指向：{subject_id}",
                where={
                    "subject_id": subject_id,
                    "job_ids": [report.job_id for report, _ in matches],
                },
                remediation=[
                    "这两份报告内容完全相同且产出于同一秒，报告标识因此撞号",
                    "请在该作业的报告页内提交裁决（界面已带作业上下文）",
                ],
            )
        if matches:
            matched, finding = matches[0]
            return self.repository.get_job(matched.job_id), matched, finding

        for job in self.repository.list_jobs():
            if job.is_orchestration:
                continue
            if not any(item.decision_id == subject_id for item in self.decisions(job.job_id)):
                continue
            ledger = self.repository.find_report(job.job_id)
            if ledger is None:
                raise EngineError(
                    ErrorCode.REPORT_MISSING,
                    f"作业 {job.job_id} 还没有门禁报告，决策项 {subject_id} 的裁决无处留痕",
                    where={"subject_id": subject_id, "job_id": job.job_id},
                    remediation=[
                        "裁决留痕写在报告上（Report.adjudications），故报告是前提",
                        "等该作业的 qa 阶段产出报告后重试",
                    ],
                )
            return job, ledger, None

        raise EngineError(
            ErrorCode.NOT_FOUND,
            f"找不到待裁决项：{subject_id}",
            where={"subject_id": subject_id},
            remediation=[
                "标识取自 GET /inbox 的 subject_id 或报告里判定的 finding_id",
                "已办结的项可以从报告里读到，但不再出现在待办列表",
            ],
        )

    def _unresolved(self, job: Job, report: Report) -> list[str]:
        """该作业仍未办结的待裁决项（空 = 全部办结）。

        两个 ``_*_subjects`` 已经把"有裁决留痕的"排除掉了，故这里只取标识别无二次过滤
        ——再滤一遍是同一判据的第二处实现，而这类重复的典型症状是"某一类项永远清不掉"。
        """
        decided = {item.subject_id for item in report.adjudications}
        remaining = self._finding_subjects(job, report, decided)
        remaining.extend(self._decision_subjects(job, report, decided))
        return [item.subject_id for item in remaining]

    def _advance(
        self, job: Job, report: Report, *, action: str, remaining: list[str]
    ) -> tuple[Job, AdvanceOutcome]:
        """按裁决结果决定作业下一步（规则见模块文档末节）。"""
        if job.status != "awaiting_decision":
            return job, "not_awaiting"
        if action == "configure":
            return job, "configure"
        if remaining:
            return job, "remaining"
        if report.verdict == "blocked":
            return job, "blocked"
        return self._decided(job), "advanced"

    def _decided(self, job: Job) -> Job:
        """``awaiting_decision --decided--> running``（经状态机，不直接改 status）。"""
        moved = JobStateMachine.apply(job, "decided")
        self.repository.save_job(moved)
        self.runner.events.emit(moved.job_id, "job", {"status": moved.status, "action": "decided"})
        self._aggregate_parent(moved)
        return moved

    def _aggregate_parent(self, child: Job) -> None:
        """重算子作业的编排聚合（与 ``JobRunner._aggregate_parent`` 同口径）。

        漏掉这一步时，子作业已经离开 ``awaiting_decision`` 而编排作业还停在
        "待裁决"，界面上的进度会永远差一份。
        """
        if child.parent_job_id is None:
            return
        parent = self.repository.find_job(child.parent_job_id)
        if parent is None:
            return
        children = [
            found
            for job_id in parent.child_job_ids
            if (found := self.repository.find_job(job_id)) is not None
        ]
        if not children:
            return
        self.repository.save_job(with_aggregate(parent, children))


_EPOCH: Final = datetime(1970, 1, 1, tzinfo=timezone.utc)
"""排序兜底（aware，与 ``created_at`` 同类型）。用 naive 兜底会在"某条缺时刻"时
抛 ``can't compare offset-naive and offset-aware datetimes``——一次排序崩掉整个待办页。"""

_NO_LEDGER: Final = "该作业还没有门禁报告，裁决留痕无处落笔（留痕写在 Report.adjudications 上）"

_NOTE: Final[dict[str, str]] = {
    "advanced": "已办结；作业回到运行中，worker 将复用断点收尾。",
    "remaining": "已办结；该作业还有其它待裁决项，全部办结后作业才会继续。",
    "blocked": ADVANCE_BLOCKED_NOTE,
    "configure": (
        "已办结；配置改动需要一次新的构建才会生效（新旧输入的摘要不同，断点才会失效），"
        "请修改配置后重新发起构建。"
    ),
    "not_awaiting": "已留痕；该作业当前不在「待裁决」，状态不受影响。",
}
