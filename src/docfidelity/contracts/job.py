"""作业、阶段与决策留痕契约（DD-3.2（5）/ DD-11）。

三类持久化对象
--------------
``Job``              作业（编排作业 / 文档子作业）
``Stage``            子作业内的阶段
``StageCheckpoint``  阶段断点，承载"续跑正确性"

两条硬约束
----------
1. **作业状态迁移唯一入口**是 ``pipeline`` 的 ``JobStateMachine.apply``；
   任何代码**不得**直接 ``UPDATE jobs.status``。合法迁移见 :data:`JOB_TRANSITIONS`。
2. ``StageCheckpoint.version`` 取 :data:`STAGE_IMPL_VERSION`。阶段实现一经变更，
   旧 checkpoint **必须失效重跑**，否则"续跑"的语义不可信（风险 #6）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, ClassVar, Final, Literal

from pydantic import Field

from .base import FormatModel
from .errors import EngineErrorInfo

JobStatus = Literal[
    "queued",
    "running",
    "awaiting_decision",
    "succeeded",
    "failed",
    "partial_failure",
    "canceled",
]
"""作业状态。``partial_failure`` **仅编排作业**可取（表示部分子作业成功）。"""

StageName = Literal["ingest", "compose", "render", "refresh", "qa"]

StageStatus = Literal["pending", "running", "succeeded", "failed", "skipped"]

JobKind = Literal["orchestration", "document"]

DecisionSource = Literal["rule_default", "model", "manual", "fallback"]
"""决策来源。``fallback`` 表示模型不可用后走的规则兜底（NFR-26）。"""

DecisionStatus = Literal["auto", "pending", "confirmed", "rejected"]

EventType = Literal["job", "stage", "decision", "artifact", "log"]

STAGE_NAMES: Final[tuple[StageName, ...]] = ("ingest", "compose", "render", "refresh", "qa")

#: 阶段实现版本。**变更即强制失效历史 checkpoint**，并记录到 CHANGELOG。
STAGE_IMPL_VERSION: Final[dict[str, int]] = {
    "ingest": 1,
    "compose": 2,
    "render": 4,
    "refresh": 1,
    "qa": 2,
}

#: 子作业的合法状态迁移表。空集合表示终态。
#:
#: ``failed`` 与 ``canceled`` **都不是终态**：DD-11.2 明确列了 ``failed → running``
#: （``retry_stage`` / ``resume_from``）与 ``failed → queued``（``restart``）；
#: DD-11.4 的 ``rerun_failed`` 更把 ``("failed", "canceled")`` 一并列为要重置的子作业。
#: 把这两者写成终态会让 FR-M7-03 / FR-M7-16 的"重试 / 从失败阶段续跑 / 只重跑失败子作业"
#: 在契约层就不可表达（P0-13 发现并修正）。
#: ``succeeded`` 保持终态：放弃一套**已完成**的产物不是"取消"，与 FR-M7-16
#: "已完成子作业的产物保持可用"冲突，故它不接受任何迁移。
JOB_TRANSITIONS: Final[dict[str, frozenset[str]]] = {
    "queued": frozenset({"running", "canceled", "failed"}),
    "running": frozenset(
        {"awaiting_decision", "succeeded", "failed", "canceled", "partial_failure"}
    ),
    "awaiting_decision": frozenset({"running", "succeeded", "failed", "canceled"}),
    "succeeded": frozenset(),
    "failed": frozenset({"running", "queued", "canceled"}),
    "partial_failure": frozenset({"canceled"}),
    "canceled": frozenset({"running", "queued"}),
}

#: 阶段的合法状态迁移表。
STAGE_TRANSITIONS: Final[dict[str, frozenset[str]]] = {
    "pending": frozenset({"running", "skipped"}),
    "running": frozenset({"succeeded", "failed", "skipped"}),
    "succeeded": frozenset(),
    "failed": frozenset(),
    "skipped": frozenset(),
}

#: 编排作业的聚合规则（唯一实现于服务端，前端不得推断）。
ORCHESTRATION_AGGREGATION_RULE: Final[str] = (
    "全部 succeeded 且无待裁决 -> succeeded；"
    "存在待裁决 -> awaiting_decision；"
    "部分 succeeded 部分 failed/canceled -> partial_failure；"
    "全部 failed -> failed；全部 canceled -> canceled"
)


class ChildProgress(FormatModel):
    """编排作业的进度视图（仅编排作业使用）。"""

    done: int = Field(default=0, ge=0)
    total: int = Field(default=0, ge=0)
    failed: int = Field(default=0, ge=0)
    current_child: str | None = None


class CheckpointRef(FormatModel):
    """阶段上的 checkpoint 引用。"""

    stage: str
    version: int
    inputs_hash: str


class StageCheckpoint(FormatModel):
    """阶段断点。

    ``outputs`` 记录"逻辑名 -> 工作区相对路径"，使后续阶段可以只按逻辑名取件，
    不关心物理布局。
    """

    format_family: ClassVar[str] = "job"

    stage: str
    inputs_hash: str = Field(description="本阶段输入的摘要；变化即不得复用")
    outputs: dict[str, str] = Field(default_factory=dict)
    version: int = Field(description="阶段实现版本，取值见 STAGE_IMPL_VERSION")
    created_at: datetime | None = None


class Stage(FormatModel):
    """子作业内的一个阶段。"""

    format_family: ClassVar[str] = "job"

    name: StageName
    status: StageStatus = "pending"
    progress: float = Field(default=0.0, ge=0.0, le=1.0)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    checkpoint: CheckpointRef | None = None
    error: EngineErrorInfo | None = None


class Job(FormatModel):
    """作业。

    ``kind="orchestration"`` 表示一次"构建整套文档"的编排作业，其 ``child_job_ids``
    指向各文档子作业；``kind="document"`` 表示单份文档的构建。
    """

    format_family: ClassVar[str] = "job"

    job_id: str
    parent_job_id: str | None = None
    kind: JobKind
    project_id: str
    doc_key: str | None = Field(default=None, description="仅文档子作业")
    doc_type: str | None = Field(default=None, description="仅文档子作业；引擎不假设取值")
    status: JobStatus = "queued"
    child_job_ids: list[str] = Field(default_factory=list, description="仅编排作业")
    created_at: datetime
    inputs_hash: str
    initiated_by: str | None = Field(
        default=None,
        description="发起人（界面作业列表的「发起人」列）。审计用，**不参与任何判定**",
    )
    artifact_ids: list[str] = Field(default_factory=list)
    progress: ChildProgress | None = Field(default=None, description="仅编排作业")
    error: EngineErrorInfo | None = None

    @property
    def is_orchestration(self) -> bool:
        return self.kind == "orchestration"

    def can_transition_to(self, target: JobStatus) -> bool:
        """按 :data:`JOB_TRANSITIONS` 判断迁移是否合法（实际迁移由服务端执行）。"""
        return target in JOB_TRANSITIONS[self.status]


class JobEvent(FormatModel):
    """作业事件，SSE 推送与断线回放的载体（DD-11.6）。

    纪律：**先落库再推送**——``event_id`` 单调自增，客户端用 ``Last-Event-ID``
    续推；连接断开不影响作业。
    """

    format_family: ClassVar[str] = "job"

    event_id: int = Field(ge=1, description="作业内单调自增序号")
    job_id: str
    type: EventType
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class DecisionOption(FormatModel):
    """一个候选选项。低置信选项必须给出 ``reason``，供人在裁决队列中判断。"""

    value: str
    label: str | None = None
    reason: str | None = None
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)


class Decision(FormatModel):
    """一次决策留痕。

    低置信（``confidence`` 低于档案 / 规则集阈值）或 ``status="pending"`` 的决策，
    必须在界面上进入裁决队列；**不得静默采用**（验收 39）。
    """

    format_family: ClassVar[str] = "job"

    decision_id: str
    job_id: str | None = None
    kind: str = Field(description="决策点标识")
    target: str = Field(description="作用对象：单元 id / 槽位 id / 模板引用等")
    options: list[DecisionOption] = Field(default_factory=list)
    chosen: str | None = None
    reason: str | None = None
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    source: DecisionSource = "rule_default"
    status: DecisionStatus = "auto"
    created_at: datetime


class ModelCall(FormatModel):
    """一次模型调用留痕（Tracing）。

    失败时 ``ok=False`` 且 ``fallback_used=True``——这**不是**构建失败，
    只是决策降级（NFR-26 / 验收 39）。
    """

    format_family: ClassVar[str] = "job"

    call_id: str
    job_id: str | None = None
    decision_point: str
    route: str = Field(description="路由目标：agent 名 / 规划器标识")
    model: str | None = None
    latency_ms: int | None = Field(default=None, ge=0)
    tokens_in: int | None = Field(default=None, ge=0)
    tokens_out: int | None = Field(default=None, ge=0)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    fallback_used: bool = False
    ok: bool = True
    error: EngineErrorInfo | None = None
    created_at: datetime


class Artifact(FormatModel):
    """构建产物登记条目。"""

    format_family: ClassVar[str] = "job"

    artifact_id: str
    job_id: str
    kind: Literal["docx", "pdf", "png", "report", "repro_package", "other"]
    relative_path: str
    size_bytes: int = Field(ge=0)
    sha256: str
    created_at: datetime
    expires_at: datetime | None = Field(default=None, description="过期后取件报 E_ARTIFACT_EXPIRED")


Job.model_rebuild()
Stage.model_rebuild()


__all__ = [
    "JOB_TRANSITIONS",
    "ORCHESTRATION_AGGREGATION_RULE",
    "STAGE_IMPL_VERSION",
    "STAGE_NAMES",
    "STAGE_TRANSITIONS",
    "Artifact",
    "CheckpointRef",
    "ChildProgress",
    "Decision",
    "DecisionOption",
    "DecisionSource",
    "DecisionStatus",
    "EventType",
    "Job",
    "JobEvent",
    "JobKind",
    "JobStatus",
    "ModelCall",
    "Stage",
    "StageCheckpoint",
    "StageName",
    "StageStatus",
]
