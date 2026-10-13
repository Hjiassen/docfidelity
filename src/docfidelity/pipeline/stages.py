"""阶段定义与执行（DD-11.3 / 决策一「编排与渲染解耦」）。

本模块回答一个问题：**一个文档子作业怎么按阶段推进，又怎么在中断后接着跑。**

阶段与顺序（DD-11.3）
---------------------
``ingest → compose → render → refresh → qa``。顺序即语义：后一阶段的输入是前一阶段的
产出，中间态全部落在 ``jobs/<job_id>/work/``（DD-11.7），因此"从任一阶段续跑"就是
"从那个阶段往下重跑，上游一律沿用断点"。

编排与渲染解耦的**可执行含义**
------------------------------
本模块**不 import** ``ingest`` / ``compose`` / ``render`` / ``qa``。阶段体（谁真正
去解析模板、去注入 OOXML、去判定门禁）由调用方通过 :class:`StageRegistry` 注册。
理由不是洁癖：把阶段体写死在编排里之后，"换一种渲染策略""在测试里替换一个必失败的
阶段""在无 Office 的机器上验证降级"这三件事都会变成改编排代码。注册表把这种替换
变成一次函数调用，也让"失败隔离"能在**不构造真实输入**的前提下被证明。

为什么 ``skipped`` 不写断点
---------------------------
阶段体可以返回 ``skipped_reason``（例：刷新阶段判定"目录无需刷新"）。这类"不适用"
**不落断点**：断点一旦落盘就表示"该阶段产出了这些输出"，而 skipped 恰恰没产出。
下次续跑时它会被重新判定——判定本身是廉价的（不适用 != 昂贵），而把一个空输出伪装成
产出会让下游阶段拿到一个指向不存在文件的路径。

失败隔离
--------
某个阶段失败时：① 该阶段**不落断点**（失败不是产出）；② 前面阶段的断点**原样保留**，
所以续跑不必重头来；③ 后面的阶段**不再执行**。三条合起来就是 FR-M7-16 的
"从失败阶段续跑、已完成部分不重跑"。
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final

from docfidelity.contracts.errors import (
    ConfigurationError,
    EngineError,
    EngineErrorInfo,
    ErrorCode,
)
from docfidelity.contracts.job import (
    STAGE_NAMES,
    CheckpointRef,
    Decision,
    Job,
    JobStatus,
    Stage,
    StageCheckpoint,
    StageName,
    StageStatus,
)
from docfidelity.contracts.qa import Finding, Location
from docfidelity.platform.fs import atomic_write_bytes, atomic_write_text, safe_join

from .checkpoint import (
    WORK_DIRNAME,
    CheckpointStore,
    CorruptCheckpoint,
    ReuseDecision,
    decide_reuse,
    impl_version_of,
    path_segment,
)

# `DecisionOutcome` 经接缝模块转出：本模块**不直接 import** `agents`，
# 那条约束由 `.importlinter` 的 `pipeline-agents-seam` 契约守着。
from .decisions import DecisionOutcome, DecisionSession, rule_only_session
from .events import EventLog

__all__ = [
    "ARTIFACTS_DIRNAME",
    "CHECKPOINT_RULE_ID",
    "INPUTS_DIRNAME",
    "STAGE_ORDER",
    "DocumentRun",
    "StageContext",
    "StageFunc",
    "StageOutcome",
    "StageRegistry",
    "StageRun",
    "Workspace",
    "plan_resume",
    "run_document",
    "run_stage",
]

#: 阶段执行顺序（DD-11.3）。顺序即语义。
STAGE_ORDER: Final[tuple[StageName, ...]] = STAGE_NAMES

#: 工作区子目录名（DD-11.7）。
ARTIFACTS_DIRNAME: Final = "artifacts"
INPUTS_DIRNAME: Final = "inputs"

#: 本模块判定所用的需求条目：FR-M7-04（作业与阶段状态持久化、可续跑）。
CHECKPOINT_RULE_ID: Final = "FR-M7-04"


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


class Workspace:
    """作业工作区：``<root>/jobs/<job_id>/``（DD-11.7）。

    一切路径拼接都经 :meth:`resolve`，越界抛 ``E_PATH_ESCAPE``。阶段体因此可以
    放心地用逻辑名（``"fact_bundle.json"``）取放文件，而不必自己碰 ``pathlib``。
    """

    def __init__(self, root: Path | str, job_id: str) -> None:
        self._root = Path(root)
        self._job_id = path_segment(job_id, field="job_id")

    @property
    def root(self) -> Path:
        return self._root

    @property
    def job_id(self) -> str:
        return self._job_id

    @property
    def job_dir(self) -> Path:
        return safe_join(self._root, "jobs", self._job_id)

    @property
    def work_dir(self) -> Path:
        """阶段中间态（``fact_bundle.json`` / ``content_ir.json`` / ``out.docx`` …）。"""
        return safe_join(self.job_dir, WORK_DIRNAME)

    @property
    def artifacts_dir(self) -> Path:
        """交付物（docx / PDF / 报告 / 目检图）。"""
        return safe_join(self.job_dir, ARTIFACTS_DIRNAME)

    @property
    def inputs_dir(self) -> Path:
        """冻结的输入快照（模板包版本 / 事实源哈希 / 配置版本）。"""
        return safe_join(self.job_dir, INPUTS_DIRNAME)

    def resolve(self, relative: str) -> Path:
        """工作区内的相对路径 → 绝对路径。"""
        return safe_join(self.job_dir, relative)

    def put_text(self, relative: str, text: str) -> str:
        """原子写文本，返回该文件的**工作区相对路径**（断点里存的就是它）。"""
        atomic_write_text(self.resolve(relative), text)
        return Path(relative).as_posix()

    def put_bytes(self, relative: str, payload: bytes) -> str:
        """原子写字节（docx 等二进制产物）。"""
        atomic_write_bytes(self.resolve(relative), payload)
        return Path(relative).as_posix()

    def put_json(self, relative: str, payload: Any) -> str:
        """原子写 JSON（契约模型可先 ``model_dump(mode="json")``）。"""
        return self.put_text(
            relative, json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
        )

    def read_text(self, relative: str) -> str:
        """读工作区内的文本文件。"""
        return self.resolve(relative).read_text(encoding="utf-8")

    def read_json(self, relative: str) -> Any:
        """读工作区内的 JSON 文件。"""
        return json.loads(self.read_text(relative))


@dataclass(frozen=True)
class StageContext:
    """传给阶段体的全部信息。

    ``payload`` 承载"这次构建要用的输入句柄"（模板包路径、档案路径、事实源配置…）。
    它是 ``Mapping`` 而不是若干具名参数，是为了让**新增一种输入不必改编排签名**——
    编排只负责把上游冻结的快照原样递下去（FR-M7-15："编排作业只冻结输入快照"）。

    ``decisions`` 是**必填**的：阶段体要做判断时走 :meth:`decide`，而"没有会话"这种
    状态压根不该存在。做成可空会让每个阶段体都得写一次 ``if ctx.decisions is None``，
    而没有兜底的后果更重——一个"编排忘了接线"会伪装成"每次都判出保守取值"。
    """

    job: Job
    stage: StageName
    inputs_hash: str
    workspace: Workspace
    decisions: DecisionSession
    payload: Mapping[str, Any] = field(default_factory=dict)

    def option(self, key: str, default: Any = None) -> Any:
        """读一个输入句柄；缺失返回 ``default``（由阶段体决定是否报错）。"""
        return self.payload.get(key, default)

    def decide(
        self,
        decision_point_id: str,
        inputs: Mapping[str, Any] | None = None,
        *,
        target: str = "",
        candidates: Sequence[str] = (),
        context: Mapping[str, Any] | None = None,
    ) -> DecisionOutcome:
        """在本阶段内做一次决策（转发到本作业的决策会话，唯一实现 ``agents.dispatch``）。

        ``candidates`` 与 ``context`` 由**调用方按决策点裁剪**后传入（NFR-25）：
        "不投喂完整契约与全量事实"是阶段体的责任，因为只有它知道这次该看哪几段。
        """
        return self.decisions.decide(
            decision_point_id,
            inputs,
            target=target,
            candidates=candidates,
            context=context,
        )


@dataclass
class StageOutcome:
    """阶段体的返回值。

    ``error`` 非空即失败（``fatal`` 语义由阶段体决定，编排只区分成功/失败）。
    ``skipped_reason`` 表示"本阶段不适用"，与失败严格区分：不适用是**成功的一种**，
    不写断点、不影响下游。
    """

    outputs: dict[str, str] = field(default_factory=dict)
    findings: list[Finding] = field(default_factory=list)
    skipped_reason: str | None = None
    error: EngineErrorInfo | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.error is None

    @classmethod
    def from_error(cls, error: EngineError) -> StageOutcome:
        return cls(error=error.to_info())

    @classmethod
    def skipped(cls, reason: str) -> StageOutcome:
        return cls(skipped_reason=reason)


StageFunc = Callable[[StageContext], StageOutcome]
"""阶段体签名。返回结构化结果，**不得**抛裸异常（抛了会被编排兜成 internal 失败）。"""


class StageRegistry:
    """阶段体注册表：阶段名 → 实现。**编排与阶段体之间唯一的接缝**。"""

    def __init__(self, functions: Mapping[str, StageFunc] | None = None) -> None:
        self._functions: dict[str, StageFunc] = {}
        for name, function in (functions or {}).items():
            self.register(name, function)

    def register(self, stage: str, function: StageFunc, *, replace: bool = False) -> None:
        """登记阶段体。重复登记默认报错（静默覆盖会让"替换阶段"变成事故）。"""
        if stage not in STAGE_NAMES:
            raise ConfigurationError(
                f"未登记的阶段名：{stage!r}",
                where={"stage": stage, "known": list(STAGE_NAMES)},
                remediation=["阶段名取自 contracts.job.STAGE_NAMES，不得自创"],
            )
        if stage in self._functions and not replace:
            raise ConfigurationError(
                f"阶段 {stage!r} 已登记实现",
                where={"stage": stage},
                remediation=["确需替换时显式传 replace=True"],
            )
        self._functions[stage] = function

    def resolve(self, stage: str) -> StageFunc:
        """取阶段体；未登记**立刻报错**，不返回一个"空跑"的占位实现。"""
        try:
            return self._functions[stage]
        except KeyError:
            raise ConfigurationError(
                f"阶段 {stage!r} 没有登记实现，作业无法推进",
                where={"stage": stage, "registered": self.registered()},
                remediation=["在服务启动时注册该阶段体（编排层不内置阶段实现）"],
            ) from None

    def registered(self) -> tuple[str, ...]:
        return tuple(name for name in STAGE_NAMES if name in self._functions)

    def missing(self) -> tuple[str, ...]:
        return tuple(name for name in STAGE_NAMES if name not in self._functions)

    def __contains__(self, stage: object) -> bool:
        return stage in self._functions


@dataclass
class StageRun:
    """一次阶段推进的结果（含"沿用断点"这种零执行的情况）。"""

    stage: StageName
    status: StageStatus
    reuse: ReuseDecision
    outputs: dict[str, str] = field(default_factory=dict)
    findings: list[Finding] = field(default_factory=list)
    skipped_reason: str | None = None
    error: EngineErrorInfo | None = None
    detail: dict[str, Any] = field(default_factory=dict)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    #: 本阶段结束后留在 store 里的断点引用。
    #:
    #: 语义是 **"这个阶段有断点"**，不是"这次沿用了断点"——沿用与否由 :attr:`reuse`
    #: 单独表达。两者混进一个字段的代价很具体：界面上那一列（DD-14.4 的
    #: ``StageTimeline``）回答的是"重跑时会不会真的重跑"，而"本次零执行"恰好只是
    #: "有断点"的一个特例。混在一起之后，**每个刚刚成功跑完的阶段都会被显示成
    #: "无断点"**——它明明刚写了断点，界面却告诉使用者"重跑会重新执行一遍"。
    checkpoint_ref: CheckpointRef | None = None

    @property
    def reused(self) -> bool:
        """本次是否**零执行**（沿用有效断点）。"""
        return self.reuse.reusable

    @property
    def executed(self) -> bool:
        return not self.reused

    def record(self) -> Stage:
        """转成契约 :class:`~docfidelity.contracts.job.Stage`（落库 / 推进度用）。"""
        settled = self.status in {"succeeded", "skipped"}
        return Stage(
            name=self.stage,
            status=self.status,
            progress=1.0 if settled else 0.0,
            started_at=self.started_at,
            finished_at=self.finished_at,
            checkpoint=self.checkpoint_ref,
            error=self.error,
        )


@dataclass
class DocumentRun:
    """一次文档子作业的全阶段推进结果。"""

    job_id: str
    runs: list[StageRun] = field(default_factory=list)
    #: 本次运行共用的决策会话（路由视图、待裁决项、token 合计都从它读）。
    #:
    #: 留在这里而不是只在局部变量里：留痕是**产物的一部分**（NFR-15 决策可解释），
    #: 调用方跑完之后应当还能问"这份文档有没有待裁决项"，而不是只能从事件文件里捞。
    session: DecisionSession | None = None

    @property
    def status(self) -> JobStatus:
        """本作业推进后的状态。

        有阶段失败 → ``failed``；全部跑完但**存在待裁决项** → ``awaiting_decision``；
        否则 ``succeeded``。中间那条不能省：把它并进 ``succeeded`` 等于宣布"这份产物
        已可定稿"，而它其实在等人裁决（NFR-26 / 验收 39）。服务端 worker 的
        ``_settle`` 得出同一个结论——两处口径不同时，批量入口造出的作业会显示成
        "已完成"，而界面上那个待裁决徽标永远不亮。
        """
        if any(run.status == "failed" for run in self.runs):
            return "failed"
        if self.has_pending_decision:
            return "awaiting_decision"
        return "succeeded"

    @property
    def stopped_at(self) -> StageName | None:
        """停止的阶段（失败处）；全程跑完为 ``None``。"""
        for run in self.runs:
            if run.status == "failed":
                return run.stage
        return None

    def ran(self) -> tuple[StageName, ...]:
        """本次**真正执行**过的阶段。"""
        return tuple(run.stage for run in self.runs if run.executed)

    def reused(self) -> tuple[StageName, ...]:
        """本次沿用了断点、未重新执行的阶段。"""
        return tuple(run.stage for run in self.runs if run.reused)

    def findings(self) -> list[Finding]:
        return [item for run in self.runs for item in run.findings]

    def decisions(self) -> tuple[Decision, ...]:
        """本次运行的全部决策留痕（未绑定会话时为空元组）。"""
        return () if self.session is None else self.session.decisions()

    def pending_decisions(self) -> tuple[Decision, ...]:
        """进裁决队列的项：低置信 / 兜底 / 需人工（NFR-26）。"""
        return () if self.session is None else self.session.pending_decisions()

    @property
    def has_pending_decision(self) -> bool:
        return bool(self.pending_decisions())

    def outputs(self) -> dict[str, str]:
        """合并各阶段输出（逻辑名 → 相对路径）；同名以后者为准。"""
        merged: dict[str, str] = {}
        for run in self.runs:
            merged.update(run.outputs)
        return merged

    def stage_records(self) -> list[Stage]:
        return [run.record() for run in self.runs]


def _corrupt_finding(stage: str, error: EngineError) -> Finding:
    return Finding(
        rule_id=CHECKPOINT_RULE_ID,
        severity="major",
        message=f"阶段 {stage} 的断点无法读取，已重跑该阶段。",
        location=Location(part=str(error.where.get("path", "")) or None),
        evidence=str(error.where.get("detail", "")),
        suggestion="确认磁盘可读；若该断点确已损坏，可删除后重跑。",
    )


def _stage_ref(stage: StageName, inputs_hash: str) -> CheckpointRef:
    """阶段结束后应留在 store 里的断点引用（``version`` 与 ``inputs_hash`` 都取真实值）。

    ``inputs_hash`` 必须是**本阶段实际运行时的那个摘要**，不能填占位串：界面拿它来
    回答"这次重跑会不会复用"，填 ``""`` 等于把"输入没变"这件事说反。
    """
    return CheckpointRef(stage=stage, version=impl_version_of(stage), inputs_hash=inputs_hash)


def plan_resume(
    *,
    job_id: str,
    store: CheckpointStore,
    inputs_hash: str,
    stages: Sequence[StageName] = STAGE_ORDER,
) -> tuple[StageName, ...]:
    """算出"续跑要从哪些阶段开始执行"。

    界面的"将从 X 阶段续跑"就是它的第一项；测试靠它断言"上游确实被跳过"。
    坏断点按"必须重跑"计——这正是不可信的断点该有的下场。
    """
    pending: list[StageName] = []
    for stage in stages:
        try:
            checkpoint = store.load(job_id, stage)
        except CorruptCheckpoint:
            checkpoint = None
        if decide_reuse(checkpoint, stage=stage, inputs_hash=inputs_hash).must_run:
            pending.append(stage)
    return tuple(pending)


def run_stage(
    *,
    job: Job,
    stage: StageName,
    registry: StageRegistry,
    store: CheckpointStore,
    workspace: Workspace,
    inputs_hash: str,
    payload: Mapping[str, Any] | None = None,
    decisions: DecisionSession | None = None,
    now: Callable[[], datetime] | None = None,
) -> StageRun:
    """推进一个阶段（DD-11.3 的 ``run_stage``）。

    顺序是强制的：**先问断点 → 能复用就复用 → 否则执行 → 成功才落断点**。
    任何一步跳过都会破坏续跑语义（见模块文档与 ``checkpoint`` 的说明）。

    ``decisions`` 缺省建一个**确定性**会话（总开关关闭，全链路走规则默认值）。
    单独调本函数的场合（单阶段重跑、测试）因此不会因为"这个进程没配模型"而在某次
    决策上拿到不确定的结果。需要留痕可见时请显式传入会话——:func:`run_document`
    与服务端 worker 都是**一个作业共用一个会话**，故决策序号与事件顺序跨阶段连续。
    """
    clock = now or _utcnow
    started = clock()
    findings: list[Finding] = []
    session = decisions if decisions is not None else rule_only_session(job.job_id)

    checkpoint: StageCheckpoint | None = None
    try:
        checkpoint = store.load(job.job_id, stage)
    except CorruptCheckpoint as exc:
        findings.append(_corrupt_finding(stage, exc))

    decision = decide_reuse(checkpoint, stage=stage, inputs_hash=inputs_hash)
    if decision.reusable and decision.checkpoint is not None:
        return StageRun(
            stage=stage,
            status="succeeded",
            reuse=decision,
            outputs=dict(decision.checkpoint.outputs),
            findings=findings,
            checkpoint_ref=_stage_ref(stage, inputs_hash),
            started_at=started,
            finished_at=clock(),
        )

    session.begin_stage(stage)
    context = StageContext(
        job=job,
        stage=stage,
        inputs_hash=inputs_hash,
        workspace=workspace,
        decisions=session,
        payload=dict(payload or {}),
    )
    try:
        outcome = registry.resolve(stage)(context)
    except EngineError as exc:
        outcome = StageOutcome.from_error(exc)
    except Exception as exc:  # 阶段体的裸异常不得掀翻编排（失败隔离）
        outcome = StageOutcome.from_error(
            EngineError(
                ErrorCode.INTERNAL,
                f"阶段 {stage} 抛出未结构化异常：{exc!r}",
                where={"stage": stage, "job_id": job.job_id},
                remediation=["修正阶段体，使其返回结构化的 StageOutcome"],
            )
        )

    if not outcome.ok:
        return StageRun(
            stage=stage,
            status="failed",
            reuse=decision,
            findings=[*findings, *outcome.findings],
            error=outcome.error,
            detail=outcome.detail,
            started_at=started,
            finished_at=clock(),
        )

    if outcome.skipped_reason is not None:
        return StageRun(
            stage=stage,
            status="skipped",
            reuse=decision,
            findings=[*findings, *outcome.findings],
            skipped_reason=outcome.skipped_reason,
            detail=outcome.detail,
            started_at=started,
            finished_at=clock(),
        )

    store.save(
        job.job_id,
        StageCheckpoint(
            stage=stage,
            inputs_hash=inputs_hash,
            outputs=dict(outcome.outputs),
            version=impl_version_of(stage),
            created_at=clock(),
        ),
    )
    return StageRun(
        stage=stage,
        status="succeeded",
        reuse=decision,
        outputs=dict(outcome.outputs),
        findings=[*findings, *outcome.findings],
        detail=outcome.detail,
        checkpoint_ref=_stage_ref(stage, inputs_hash),
        started_at=started,
        finished_at=clock(),
    )


def run_document(
    *,
    job: Job,
    registry: StageRegistry,
    store: CheckpointStore,
    workspace: Workspace,
    inputs_hash: str,
    payload: Mapping[str, Any] | None = None,
    stages: Sequence[StageName] = STAGE_ORDER,
    decisions: DecisionSession | None = None,
    events: EventLog | None = None,
    now: Callable[[], datetime] | None = None,
) -> DocumentRun:
    """按顺序推进一个文档子作业的全部阶段，遇失败即停（失败隔离）。

    ``decisions`` 缺省时**建一个**（确定性会话）并交给全部阶段：留痕的 ``job_id``
    与决策序号各只能有一份，逐阶段各建一个会让它们从第二个阶段起就对不上。
    ``events`` 只在缺省建会话时用得上（会话自带留痕落点）；显式传入 ``decisions``
    时它就多余了，故两者最好只给一个。
    """
    session = decisions if decisions is not None else rule_only_session(job.job_id, events=events)
    runs: list[StageRun] = []
    for stage in stages:
        run = run_stage(
            job=job,
            stage=stage,
            registry=registry,
            store=store,
            workspace=workspace,
            inputs_hash=inputs_hash,
            payload=payload,
            decisions=session,
            now=now,
        )
        runs.append(run)
        if run.status == "failed":
            break
    return DocumentRun(job_id=job.job_id, runs=runs, session=session)
