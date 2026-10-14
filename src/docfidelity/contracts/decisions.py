"""模型适配器与决策记录的共享协议，无具体 SDK 依赖。"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from pydantic import Field

from .base import FormatModel
from .config import DecisionRoute as RouteKind
from .job import Decision, DecisionSource, DecisionStatus, ModelCall


class DecisionRequest(FormatModel):
    """一次决策请求（喂给适配层的全部内容）。

    ``candidates`` 是**候选注入**的载体（需求 5.2.2）：不要求模型"想出"答案，
    只要求它在给定集合里选。``context`` 是按决策点裁剪过的上下文（NFR-25），
    裁剪由检索侧完成——把全量契约与事实塞进这里，等于把上下文预算作废。
    """

    decision_point: str
    target: str = ""
    candidates: list[str] = Field(default_factory=list)
    inputs: dict[str, Any] = Field(default_factory=dict)
    context: dict[str, Any] = Field(default_factory=dict)
    feedback: list[str] = Field(default_factory=list, description="重试时附上的上次失败原因")
    job_id: str | None = None
    model: str | None = Field(
        default=None, description="已解析的决策点模型；空值使用适配层默认模型"
    )


class DecisionOutcome(FormatModel):
    """一次决策的结果（含**实际走了哪条路径**，对应验收 41「决策点归属可查」）。

    ``declared`` 与 ``effective`` 必须分开：前者是配置声明的归属，后者是这一次真实走的路。
    二者不一致，正是"低置信退回默认值"这类事实唯一能被界面上报的方式。
    """

    decision_point: str
    declared: RouteKind
    effective: RouteKind
    chosen: str | None = None
    target: str = ""
    reason: str = ""
    source: DecisionSource = "rule_default"
    status: DecisionStatus = "auto"
    confidence: float | None = None
    fallback_used: bool = False
    attempts: int = 0
    model: str | None = None
    candidates: list[str] = Field(default_factory=list)
    output: dict[str, Any] | None = None
    admission_evidence: dict[str, Any] = Field(default_factory=dict)
    tokens_in: int = 0
    tokens_out: int = 0
    missing_confidence_count: int = 0

    @property
    def pending(self) -> bool:
        """是否进裁决队列（低置信 / 兜底 / 需人工）。"""
        return self.status == "pending"


class AdapterOutcome(FormatModel):
    """适配层一次调用的返回（薄适配层与内核之间的唯一协议）。

    ``schema_ok=False`` 与"置信度低于阈值"是**两件不同的事**：前者是格式问题
    （重试有意义），后者是能力问题（重试多半还是低置信）。二者必须分别可见，
    否则"模型老是格式错"与"模型老是不确定"在报告里长得一模一样。
    """

    ok: bool
    payload: dict[str, Any] | None = None
    chosen: str | None = None
    confidence: float | None = None
    schema_ok: bool = True
    model: str | None = None
    tokens_in: int | None = Field(default=None, ge=0)
    tokens_out: int | None = Field(default=None, ge=0)
    latency_ms: int | None = Field(default=None, ge=0)
    error: str | None = None
    detail: str = ""


@runtime_checkable
class ModelAdapter(Protocol):
    """薄适配层协议。**唯一**允许接触 openai-agents 的地方是它的实现。"""

    def available(self) -> bool: ...

    def describe(self) -> dict[str, Any]: ...

    def call(
        self, request: DecisionRequest, output_type: type[FormatModel] | None
    ) -> AdapterOutcome: ...


@runtime_checkable
class DecisionRecorder(Protocol):
    """决策与模型调用的落点。

    只要实现这两个方法，就能把留痕接到任何后端（内存 / 事件日志 / DB），
    而 `routing` 不需要知道后端是什么——这是"内核零 DB 依赖"在决策层的体现。
    """

    def record_decision(self, decision: Decision) -> None: ...

    def record_model_call(self, call: ModelCall) -> None: ...


class GrammarCapability(FormatModel):
    """受限解码能力探测结果（DD-12.4：探测失败则标记 ``grammar_disabled``）。"""

    supported: bool
    backend: str | None = None
    detail: str = ""


__all__ = [
    "DecisionRequest",
    "DecisionOutcome",
    "AdapterOutcome",
    "ModelAdapter",
    "DecisionRecorder",
    "GrammarCapability",
]
