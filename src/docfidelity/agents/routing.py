"""决策点路由（DD-12.3）——「决策可路由」的落点。

本模块只做**路由与执行**：目录与归属声明在 `agents.catalog`，模型实现在
`agents.sdk_adapter`，留痕落点在 `agents.recorder`。分成四块的理由是它们的变化
频率完全不同——加一个决策点改目录、换推理服务改适配层、换存储改记录器，
而路由逻辑（谁该被问、问不出来怎么办）应当稳定。

三条判定纪律
------------
1. **模型不可用必须可运行**（需求 5.2.4 硬约束 1）：适配层不可用、输出不合法、
   置信度不达标，一律回退到目录声明的规则默认值并**标记待确认**，构建不因此失败。
2. **低置信不得静默采用**（验收 39）：默认准入要求真实置信度；缺失值保持 None。
   章节放置可显式采用独立安全约束校验，但仍需要同策略的生产基线达标。
3. **重试带失败原因**（DD-12.4）：第 2、3 次调用会把上一次的失败原因放进
   ``DecisionRequest.feedback``。不带原因的重试只是把同一个错误问三遍。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Final, Protocol, runtime_checkable

from pydantic import Field

from docfidelity.contracts.base import FormatModel
from docfidelity.contracts.config import (
    AdmissionPolicy,
    DecisionLevel,
    DecisionPointRoute,
    DecisionStrategy,
    RoutingConfig,
)
from docfidelity.contracts.errors import EngineError, ErrorCode
from docfidelity.contracts.job import (
    Decision,
    DecisionSource,
    DecisionStatus,
    ModelCall,
)

from .admission import AdmissionResult, admit_model
from .catalog import (
    DECISION_POINT_IDS,
    DECISION_POINTS,
    DEFAULT_CONFIDENCE_THRESHOLD,
    MODEL_ROUTED,
    ROUTE_LEVEL,
    ROUTE_STRATEGY,
    DecisionPointDef,
    RouteKind,
    decision_point,
)
from .recorder import DecisionRecorder, decision_id, model_call_id, now_utc, options_of

__all__ = [
    "DEFAULT_MAX_RETRIES",
    "AdapterOutcome",
    "DecisionContext",
    "DecisionOutcome",
    "DecisionRequest",
    "ModelAdapter",
    "ResolvedRoute",
    "RoutingTable",
    "build_routing",
    "dispatch",
]

#: 模型调用重试次数上限（DD-12.4：校验 → 重试 ≤ 2 次 → 规则默认值 + 待确认）。
DEFAULT_MAX_RETRIES: Final = 2


# --------------------------------------------------------------------- 载荷
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


class ResolvedRoute(FormatModel):
    """一个决策点的**静态**解析结果（配置 + 目录 + 基线 → 该不该交给模型）。"""

    decision_point: str
    title: str
    declared: RouteKind
    effective: RouteKind
    level: DecisionLevel
    strategy: DecisionStrategy
    confidence_threshold: float = Field(ge=0.0, le=1.0)
    admission_policy: AdmissionPolicy = "require_confidence"
    model: str | None = None
    enabled_by_baseline: bool = True
    baseline_pass_rate: float | None = None
    baseline_pending: bool = False
    reason: str = ""


@dataclass
class DecisionContext:
    """一次决策所需的全部外部物：路由表、适配层、留痕落点、重试预算。

    ``job_id`` 属于**这一次运行**而不属于每一次决策，故放在上下文里、由 :func:`dispatch`
    自动填进请求。把它做成 ``dispatch`` 的逐次参数会留下一个"忘了传"的空档：漏传不报错，
    只是留痕上少一个归属——而归属缺失的留痕在报告里看起来和"这次运行没有作业"一样。

    ``sequence`` 是**作业内**的决策序号，用来让"同一作业里对同一对象的重复同结论决策"
    得到不同标识。刻意不用随机数（内核禁 random 参与逻辑）也不用时间戳（重跑不可比对）。
    """

    table: RoutingTable
    recorder: DecisionRecorder
    adapter: ModelAdapter
    job_id: str | None = None
    max_retries: int = DEFAULT_MAX_RETRIES
    sequence: int = 0

    def next_sequence(self) -> int:
        self.sequence += 1
        return self.sequence


# ------------------------------------------------------------------ 路由表
class RoutingTable:
    """已解析的路由表。构造只走 :func:`build_routing`。"""

    def __init__(self, routes: Mapping[str, ResolvedRoute]) -> None:
        self._routes: dict[str, ResolvedRoute] = dict(routes)

    # -------------------------------------------------------------- 查询
    def route(self, point_id: str) -> ResolvedRoute:
        route = self._routes.get(point_id)
        if route is None:
            raise EngineError(
                ErrorCode.INTERNAL,
                f"路由表缺少决策点 {point_id} 的解析结果",
                where={"decision_point": point_id},
                remediation=["这是实现缺陷：build_routing 必须覆盖 DECISION_POINTS 全部条目"],
            )
        return route

    def points(self) -> tuple[str, ...]:
        return tuple(self._routes)

    def describe(self) -> list[dict[str, Any]]:
        """逐决策点的归属与实际路径（验收 41「界面可查看实际路由结果」的数据面）。"""
        records: list[dict[str, Any]] = []
        for point_id, route in self._routes.items():
            record = decision_point(point_id).as_record()
            record.update(
                {
                    "effective": route.effective,
                    "level": route.level,
                    "strategy": route.strategy,
                    "confidence_threshold": route.confidence_threshold,
                    "admission_policy": route.admission_policy,
                    "model": route.model,
                    "enabled_by_baseline": route.enabled_by_baseline,
                    "baseline_pass_rate": route.baseline_pass_rate,
                    "reason": route.reason,
                }
            )
            records.append(record)
        return records

    # -------------------------------------------------------------- 决策
    def resolve(self, request: DecisionRequest, ctx: DecisionContext) -> DecisionOutcome:
        """执行一次决策，并**必定**落一条留痕。"""
        route = self.route(request.decision_point)
        if route.effective in ("rule", "human"):
            return self._settle(self._rule_outcome(route, request, route.reason), ctx, None)
        outcome, call = self._try_model(route, request, ctx)
        return self._settle(outcome, ctx, call)

    # -------------------------------------------------------------- 内部
    def _rule_outcome(
        self,
        route: ResolvedRoute,
        request: DecisionRequest,
        reason: str,
        *,
        fallback_used: bool = False,
        attempts: int = 0,
    ) -> DecisionOutcome:
        point = decision_point(route.decision_point)
        human = route.effective == "human"
        pending = human or route.baseline_pending or point.fallback.mark_pending
        # 走兜底时 ``effective`` 必须落成 ``rule``：它记的是**这一次真实走的路**——
        # 若继续写着 ``small_model``，`declared != effective` 这个"低置信退回默认值"
        # 唯一的界面信号就永远不成立，界面上只能看到一个归属，看不出实际降级。
        effective: RouteKind = "rule" if fallback_used else route.effective
        return DecisionOutcome(
            decision_point=route.decision_point,
            declared=route.declared,
            effective=effective,
            chosen=None,
            target=request.target,
            reason=reason,
            source="manual" if human else ("fallback" if fallback_used else "rule_default"),
            status="pending" if pending else "auto",
            fallback_used=fallback_used,
            attempts=attempts,
            candidates=list(request.candidates),
        )

    def _try_model(
        self, route: ResolvedRoute, request: DecisionRequest, ctx: DecisionContext
    ) -> tuple[DecisionOutcome, ModelCall | None]:
        point = decision_point(route.decision_point)
        request = request.model_copy(update={"model": route.model})
        if route.decision_point == "placement.section_slot" and not request.candidates:
            return (
                self._rule_outcome(
                    route,
                    request,
                    "没有可用的兼容章节候选，走规则默认值并标记待确认",
                    fallback_used=True,
                ).model_copy(
                    update={
                        "admission_evidence": {
                            "policy": route.admission_policy,
                            "accepted": False,
                            "validator": "placement_constraints_v1",
                        }
                    }
                ),
                None,
            )
        if not ctx.adapter.available():
            return (
                self._rule_outcome(
                    route,
                    request,
                    f"模型不可用，走规则默认值（来源 {point.fallback.source}）；构建不因此失败",
                    fallback_used=True,
                ),
                None,
            )

        last: AdapterOutcome | None = None
        admission: AdmissionResult | None = None
        tokens_in = 0
        tokens_out = 0
        missing_confidence_count = 0
        attempts = 0
        for attempt in range(1, ctx.max_retries + 2):
            attempts = attempt
            outcome = ctx.adapter.call(request, point.output_type)
            last = outcome
            tokens_in += outcome.tokens_in or 0
            tokens_out += outcome.tokens_out or 0
            missing_confidence_count += int(outcome.confidence is None)
            admission = admit_model(route, request, outcome)
            if admission.accepted:
                result = self._model_outcome(route, request, outcome, attempts, admission)
                return (
                    result.model_copy(
                        update={
                            "tokens_in": tokens_in,
                            "tokens_out": tokens_out,
                            "missing_confidence_count": missing_confidence_count,
                        }
                    ),
                    self._call_record(route, request, outcome, attempts).model_copy(
                        update={
                            "tokens_in": tokens_in,
                            "tokens_out": tokens_out,
                        }
                    ),
                )
            request = request.model_copy(update={"feedback": [*request.feedback, admission.reason]})

        assert last is not None  # 循环至少执行一次；此断言只为类型收窄
        assert admission is not None
        call = self._call_record(route, request, last, attempts).model_copy(
            update={"tokens_in": tokens_in, "tokens_out": tokens_out}
        )
        return (
            self._rule_outcome(
                route,
                request,
                f"{admission.reason}；重试 {attempts - 1} 次后走规则默认值"
                f"（来源 {point.fallback.source}）并标记待确认",
                fallback_used=True,
                attempts=attempts,
            ).model_copy(
                update={
                    "confidence": last.confidence,
                    "admission_evidence": admission.evidence,
                    "status": "pending"
                    if last.confidence is None
                    else ("pending" if point.fallback.mark_pending else "auto"),
                    "tokens_in": tokens_in,
                    "tokens_out": tokens_out,
                    "missing_confidence_count": missing_confidence_count,
                }
            ),
            call,
        )

    @staticmethod
    def _model_outcome(
        route: ResolvedRoute,
        request: DecisionRequest,
        outcome: AdapterOutcome,
        attempts: int,
        admission: AdmissionResult,
    ) -> DecisionOutcome:
        return DecisionOutcome(
            decision_point=route.decision_point,
            declared=route.declared,
            effective=route.effective,
            chosen=admission.chosen,
            target=request.target,
            reason=admission.reason,
            source="model",
            status="pending" if route.level == "confirm" else "auto",
            confidence=outcome.confidence,
            fallback_used=False,
            attempts=attempts,
            model=outcome.model or route.model,
            candidates=list(request.candidates),
            output=outcome.payload,
            admission_evidence=admission.evidence,
        )

    @staticmethod
    def _call_record(
        route: ResolvedRoute,
        request: DecisionRequest,
        outcome: AdapterOutcome,
        attempts: int,
    ) -> ModelCall:
        # ``ok`` / ``fallback_used`` 都取 ``outcome.ok``（= **调用层面**），**不要**并进
        # ``schema_ok``。格式不合格时 ``AdapterOutcome`` 是 ``ok=True, schema_ok=False``
        # （格式问题，重试有意义），而这条记录要说的是"这次调用本身成功了"——
        # 两个层面分开正是 `contracts.job.ModelCall` 与 `DecisionOutcome` 的分工：
        # 调用层面看这里，决策层面看 `DecisionOutcome.fallback_used` / `effective`。
        # 并进来的后果是"模型答了但格式不对"在报告里变成"模型调用失败"——
        # 而这两件事的补救动作完全不同（前者改提示词/换端点，后者查网络）。
        # `tests/unit/test_pipeline_decisions.py` 有一条用例专门守着这个边界。
        return ModelCall(
            call_id=model_call_id(
                {
                    "job": request.job_id,
                    "point": route.decision_point,
                    "target": request.target,
                    "attempt": attempts,
                }
            ),
            job_id=request.job_id,
            decision_point=route.decision_point,
            route=route.effective,
            model=outcome.model or route.model,
            attempts=attempts,
            latency_ms=outcome.latency_ms,
            tokens_in=outcome.tokens_in,
            tokens_out=outcome.tokens_out,
            confidence=outcome.confidence,
            fallback_used=not outcome.ok,
            ok=outcome.ok,
            created_at=now_utc(),
        )

    def _settle(
        self, outcome: DecisionOutcome, ctx: DecisionContext, call: ModelCall | None
    ) -> DecisionOutcome:
        """落留痕（NFR-15 / NFR-18），返回结论。

        留痕在**返回之前**完成：一次决策没有被记下来就等于它没发生过——
        而"模型不确定"这件事的唯一下游是裁决队列，漏记即漏裁决。
        """
        ctx.recorder.record_decision(
            Decision(
                decision_id=decision_id(
                    {
                        "job": call.job_id if call is not None else None,
                        "point": outcome.decision_point,
                        "target": outcome.target,
                        "chosen": outcome.chosen,
                        "reason": outcome.reason,
                        "sequence": ctx.next_sequence(),
                    }
                ),
                job_id=call.job_id if call is not None else None,
                kind=outcome.decision_point,
                target=outcome.target or outcome.decision_point,
                options=options_of(outcome.candidates),
                chosen=outcome.chosen,
                reason=outcome.reason,
                confidence=outcome.confidence,
                admission_evidence=outcome.admission_evidence,
                source=outcome.source,
                status=outcome.status,
                created_at=now_utc(),
            )
        )
        if call is not None:
            ctx.recorder.record_model_call(call)
        return outcome


# ------------------------------------------------------------------ 构造
def build_routing(
    config: RoutingConfig | None = None,
    *,
    model_enabled: bool | None = None,
    baseline: Mapping[str, float] | None = None,
) -> RoutingTable:
    """由配置 + 目录 + 基线评测解析出路由表。

    ``baseline`` 是逐决策点的小模型通过率（TASK-P0-17 的产物）。**未提供时不额外限制**，
    由目录声明的 ``enabled_by_baseline`` 决定；提供了就按 NFR-23 强制接管——
    两种情形都写进 ``reason``，因为"为什么这个点走了规则"必须能回答，
    否则界面上只能看到一个归属，看不出它是被能力门槛挡下来的。
    """
    routing_config = config or RoutingConfig()
    enabled = routing_config.model_enabled if model_enabled is None else model_enabled

    unknown = sorted(set(routing_config.decision_points) - DECISION_POINT_IDS)
    if unknown:
        raise EngineError(
            ErrorCode.CONFIG_INVALID,
            f"配置声明了目录之外的决策点：{unknown}",
            where={"decision_points": unknown},
            remediation=["删除这些条目，或先把它们登记到 agents/catalog.py 的 DECISION_POINTS"],
        )

    routes = {
        point.id: _resolve_one(
            point, routing_config.decision_points.get(point.id), enabled=enabled, baseline=baseline
        )
        for point in DECISION_POINTS
    }
    return RoutingTable(routes)


def _check_override_direction(point: DecisionPointDef, override: DecisionPointRoute | None) -> None:
    """配置**只能收紧，不能放宽**（需求 5.2.3 防蔓延）。

    规则与人工两档的归属是设计阶段的结论（"本就不该问模型"），不是可调参数。
    若允许配置把它们改成模型路由，那张分级表就退化成一份默认值——"顺手让模型判断一下"
    重新变得只需改一行配置，而它恰恰是需求 5.2.3 点名要消灭的形态。

    反方向是允许的：模型两档之间可以互换，也可以收紧为 ``rule`` / ``human``
    （NFR-23 的能力门槛与总开关走的是同一条方向）。
    """
    if override is None or override.route is None or override.route == point.kind:
        return
    if point.kind not in MODEL_ROUTED:
        raise EngineError(
            ErrorCode.CONFIG_INVALID,
            f"决策点 {point.id} 在设计阶段归类为 {point.kind}，配置不得将其改为 {override.route}",
            where={
                "decision_point": point.id,
                "declared": point.kind,
                "configured": override.route,
            },
            remediation=[
                "删除该配置项（要收紧可用 rule / human，或关闭模型总开关）",
                f"确需改归属，请先改 agents/catalog.py 中 {point.id} 的声明并走评审",
            ],
        )


def _resolve_one(
    point: DecisionPointDef,
    override: DecisionPointRoute | None,
    *,
    enabled: bool,
    baseline: Mapping[str, float] | None,
) -> ResolvedRoute:
    """把一条声明 + 一条配置覆盖 + 一条基线数据解析成"该不该问模型"。"""
    _check_override_direction(point, override)
    policy = override.admission_policy if override else "require_confidence"
    if policy == "independent_validation" and point.id != "placement.section_slot":
        raise EngineError(
            ErrorCode.CONFIG_INVALID,
            f"决策点 {point.id} 尚不支持独立校验准入",
            where={"decision_point": point.id, "admission_policy": policy},
            remediation=["使用 require_confidence；独立校验仅适用于章节放置"],
        )
    declared: RouteKind = (override.route if override else None) or point.kind
    # 三个字段一律"配置没写就随归属走"。阈值必须用 `is not None` 判定而不能用 `or`：
    # `confidence_threshold=0.0` 是合法配置（仍要求提供真实置信度），而 `0.0 or 0.7`
    # 会把它换成 0.7——一个"显式写下的值被默认值吃掉"的静默改写。
    level = override.level if override and override.level else ROUTE_LEVEL[declared]
    strategy = override.strategy if override and override.strategy else ROUTE_STRATEGY[declared]
    configured_threshold = override.confidence_threshold if override else None
    threshold = (
        DEFAULT_CONFIDENCE_THRESHOLD if configured_threshold is None else configured_threshold
    )
    rate = baseline.get(point.id) if baseline else None

    effective: RouteKind = declared
    if declared in ("rule", "human"):
        reason = f"归属 {declared}：由确定性路径（{point.fallback.source}）给出，不调用模型"
    elif not enabled:
        effective = "rule"
        reason = "模型总开关关闭：全链路走确定性路径，产出仍完整（验收 39）"
    elif not point.enabled_by_baseline:
        effective = "rule"
        reason = "该决策点未达基线通过率，按 NFR-23 强制由规则接管"
    elif rate is not None and rate < point.baseline_threshold:
        effective = "rule"
        reason = (
            f"基线通过率 {rate:.2f} 低于阈值 {point.baseline_threshold:.2f}，"
            "按 NFR-23 强制由规则接管"
        )
    elif rate is None:
        reason = "未提供基线评测数据，按目录声明路由"
    else:
        reason = f"基线通过率 {rate:.2f} 达标，按声明交给模型"

    return ResolvedRoute(
        decision_point=point.id,
        title=point.title,
        declared=declared,
        effective=effective,
        level=level,
        strategy=strategy,
        confidence_threshold=threshold,
        admission_policy=policy,
        model=override.model if override else None,
        enabled_by_baseline=point.enabled_by_baseline,
        baseline_pass_rate=rate,
        reason=reason,
    )


def dispatch(
    decision_point_id: str,
    inputs: Mapping[str, Any],
    ctx: DecisionContext,
    *,
    target: str = "",
    candidates: Iterable[str] = (),
    context: Mapping[str, Any] | None = None,
) -> DecisionOutcome:
    """DD-12.3 的 ``dispatch``：单次决策的唯一入口。

    只做"组装请求 → 交给路由表"两件事，判定逻辑全在 :meth:`RoutingTable.resolve`——
    两处各写一遍判定的后果是"直接调 dispatch 与经编排调得到不同结论"。
    """
    request = DecisionRequest(
        decision_point=decision_point_id,
        target=target,
        candidates=list(candidates),
        inputs=dict(inputs),
        context=dict(context or {}),
        job_id=ctx.job_id,
    )
    return ctx.table.resolve(request, ctx)
