"""决策路由：归属解析 + 三条降级不变量（DD-12.3 / NFR-23 / NFR-26 / 验收 39·41）。

本模块盯的是"决策能不能被路由"这件事里最容易失守的三处：

1. **归属解析**（配置 + 目录 + 基线 → 该不该问模型）——失守的表现是"配置写了规则，
   运行时还是去问模型"，而这两种情形在产出上**看不出差别**（模型通常也给得出答案）。
2. **降级必须可运行且不静默**（模型不可用 / 低置信 / 格式错）——回退到规则默认值、
   标记待确认、构建不失败。三者缺任何一条都还能跑通主流程，所以只能靠断言盯。
3. **留痕先于返回**（NFR-15）——漏记一次决策等于漏裁决一项，而"漏了哪一项"
   在没有记录时是查不出来的。

测试用的适配层是 `sdk_adapter.ScriptedAdapter`（离线回放器），而不是 mock：
它本身就是产品的一部分（需求 10.9 的离线验证退路），测它等于顺带验证回放链路。
"""

from __future__ import annotations

from typing import Any

import pytest

from docfidelity.agents.catalog import DECISION_POINT_IDS, DECISION_POINTS
from docfidelity.agents.recorder import MemoryRecorder
from docfidelity.agents.routing import (
    DEFAULT_MAX_RETRIES,
    AdapterOutcome,
    DecisionContext,
    DecisionOutcome,
    RoutingTable,
    build_routing,
    dispatch,
)
from docfidelity.agents.sdk_adapter import NullAdapter, ScriptedAdapter
from docfidelity.contracts.config import DecisionPointRoute, RoutingConfig
from docfidelity.contracts.errors import EngineError, ErrorCode

pytestmark = pytest.mark.unit

#: 走模型的三个决策点（需求 5.2.3 的 small_model / optional_large_model 两档）。
MODEL_POINTS = ("dedup.semantic_duplicate", "placement.section_slot", "convention.advisory")
OPTIONAL_POINTS = ("copy.generate", "review.document")


def _ctx(
    table: RoutingTable,
    adapter: Any,
    *,
    job_id: str | None = "job-1",
    max_retries: int = DEFAULT_MAX_RETRIES,
) -> DecisionContext:
    return DecisionContext(
        table=table,
        recorder=MemoryRecorder(job_id=job_id),
        adapter=adapter,
        job_id=job_id,
        max_retries=max_retries,
    )


def _outcome(**kwargs: Any) -> AdapterOutcome:
    base: dict[str, Any] = {"ok": True, "schema_ok": True, "confidence": 0.95, "model": "m"}
    base.update(kwargs)
    return AdapterOutcome(**base)


# ---------------------------------------------------------------- 归属解析
def test_build_routing_covers_every_declared_point() -> None:
    """路由表**没有缺口**：目录里的每个决策点都必须有一条解析结果。

    留白不会被发现——调用方拿不到路由时才会炸，而那时已经在跑作业了。
    """
    table = build_routing()
    assert set(table.points()) == DECISION_POINT_IDS
    assert [route.decision_point for route in (table.route(p) for p in table.points())] == list(
        table.points()
    )


@pytest.mark.parametrize("point_id", ["layout.decide", "gate.judge", "template.parse"])
def test_rule_points_stay_rule(point_id: str) -> None:
    """确定性决策点**不受模型总开关影响**：它们本就不该问模型（需求 5.2.3）。"""
    table = build_routing()
    route = table.route(point_id)
    assert route.declared == "rule"
    assert route.effective == "rule"
    assert route.level == "auto"
    assert route.strategy == "rule_only"


def test_human_point_is_never_model_effective() -> None:
    """`source.conflict` 属业务判断：**配置也不得**把它改成模型路由（需求 5.2.3 防蔓延）。"""
    config = RoutingConfig(
        model_enabled=True,
        decision_points={"source.conflict": DecisionPointRoute(route="small_model")},
    )
    with pytest.raises(EngineError) as caught:
        build_routing(config)
    assert caught.value.code == ErrorCode.CONFIG_INVALID
    assert "不得将其改为 small_model" in caught.value.message


def test_human_point_needs_no_model_output_type() -> None:
    """规则与人工两档**没有模型输出**——不该被要求配一个输出契约。"""
    point = next(item for item in DECISION_POINTS if item.id == "source.conflict")
    assert point.kind == "human"
    assert point.output_type is None
    assert point.owner_agent is None


def test_model_points_resolve_to_declared_without_baseline() -> None:
    """未提供基线数据时不额外限制，按目录声明路由——且理由里要写明"未提供"。"""
    table = build_routing()
    for point_id in (*MODEL_POINTS, *OPTIONAL_POINTS):
        route = table.route(point_id)
        assert route.effective == route.declared
        assert "未提供基线评测数据" in route.reason
        assert route.baseline_pass_rate is None


def test_model_master_switch_off_falls_back_to_rule() -> None:
    """总开关关闭 → 全链路走确定性路径，产出仍完整（验收 39）。"""
    table = build_routing(RoutingConfig(model_enabled=False))
    for point_id in (*MODEL_POINTS, *OPTIONAL_POINTS):
        route = table.route(point_id)
        assert route.declared in ("small_model", "optional_large_model")
        assert route.effective == "rule"
        assert "总开关关闭" in route.reason


def test_baseline_below_threshold_forces_rule() -> None:
    """NFR-23：基线通过率不达标 → 该点强制由规则接管，理由里带上真实比率。"""
    table = build_routing(baseline={"dedup.semantic_duplicate": 0.4})
    route = table.route("dedup.semantic_duplicate")
    assert route.effective == "rule"
    assert route.baseline_pass_rate == 0.4
    assert "0.40" in route.reason and "0.90" in route.reason


def test_baseline_above_threshold_keeps_model() -> None:
    table = build_routing(baseline={"dedup.semantic_duplicate": 0.97})
    route = table.route("dedup.semantic_duplicate")
    assert route.effective == "small_model"
    assert "达标" in route.reason


def test_baseline_only_affects_the_point_it_reports_on() -> None:
    """基线是**逐决策点**的：一个点不达标不得波及其他点。"""
    table = build_routing(baseline={"dedup.semantic_duplicate": 0.1})
    assert table.route("dedup.semantic_duplicate").effective == "rule"
    assert table.route("placement.section_slot").effective == "small_model"


def test_a_point_disabled_by_baseline_is_forced_to_rule() -> None:
    """NFR-23 的**硬**接管：目录把某点标成"未达基线"时，不看比率也必须走规则。

    这条与上面三条不是同一件事：上面是"跑出来的比率低于阈值"，这条是**声明层面的
    结论**——评测已经判过某个点不可驱动，比率后来是多少都一样。两条分支在
    ``_resolve_one`` 里是并列的 ``elif``，只测前者会让后者永远没人走过。

    ``DECISION_POINTS`` 是导入期冻住的常量、改不了现场，故这里直接给解析函数喂一条
    合成声明（``_resolve_one`` 的入参就是一条声明，不读模块状态）——测的就是那个分支。
    """
    from docfidelity.agents.catalog import DecisionPointDef, FallbackSpec
    from docfidelity.agents.routing import _resolve_one

    point = DecisionPointDef(
        id="dedup.semantic_duplicate",
        title="合成声明（只给解析函数看）",
        kind="small_model",
        owner_agent="architect",
        fallback=FallbackSpec(source="omit", owner="docfidelity.ingest.merge", detail="合成"),
        enabled_by_baseline=False,
    )
    route = _resolve_one(point, None, enabled=True, baseline=None)

    assert route.enabled_by_baseline is False
    assert route.effective == "rule", "标了未达基线却仍交给模型"
    assert "NFR-23" in route.reason


def test_config_cannot_widen_a_rule_point_to_model() -> None:
    """配置**只能收紧，不能放宽**：把确定性点改成小模型必须被拒（需求 5.2.3）。"""
    config = RoutingConfig(
        decision_points={"geometry.compute": DecisionPointRoute(route="small_model")}
    )
    with pytest.raises(EngineError) as caught:
        build_routing(config)
    assert caught.value.code == ErrorCode.CONFIG_INVALID
    assert "geometry.compute" in caught.value.message
    assert caught.value.remediation


def test_config_can_tighten_a_model_point() -> None:
    """反方向允许：模型点可被配置收紧为规则，且 level / strategy 一并随新归属走。"""
    config = RoutingConfig(
        decision_points={"dedup.semantic_duplicate": DecisionPointRoute(route="rule")}
    )
    route = build_routing(config).route("dedup.semantic_duplicate")
    assert (route.declared, route.effective) == ("rule", "rule")
    assert route.level == "auto"
    assert route.strategy == "rule_only"


def test_tuning_one_field_does_not_reset_the_others() -> None:
    """只换模型标识**不得**顺带改掉 level——这处静默改写在配置文本里看不出来。"""
    config = RoutingConfig(decision_points={"copy.generate": DecisionPointRoute(model="m-x")})
    route = build_routing(config).route("copy.generate")
    assert route.model == "m-x"
    assert route.declared == "optional_large_model"
    assert route.level == "confirm"  # 仍是"需确认"，没被契约默认值压成 auto
    assert route.strategy == "rule_then_model"
    assert route.confidence_threshold == 0.7


def test_config_can_retune_a_model_point_explicitly() -> None:
    """显式写下的 level / strategy / 阈值原样生效——"未声明"与"显式声明"必须分得开。"""
    config = RoutingConfig(
        decision_points={
            "placement.section_slot": DecisionPointRoute(
                level="confirm", strategy="rule_only", confidence_threshold=0.85
            )
        }
    )
    route = build_routing(config).route("placement.section_slot")
    assert (route.declared, route.effective) == ("small_model", "small_model")
    assert route.level == "confirm"
    assert route.strategy == "rule_only"
    assert route.confidence_threshold == 0.85


def test_explicit_zero_threshold_is_not_eaten_by_the_default() -> None:
    """`confidence_threshold=0.0` 是合法配置（不做门槛），不得被默认值 0.7 顶掉。"""
    config = RoutingConfig(
        decision_points={"dedup.semantic_duplicate": DecisionPointRoute(confidence_threshold=0.0)}
    )
    route = build_routing(config).route("dedup.semantic_duplicate")
    assert route.confidence_threshold == 0.0


def test_config_can_move_a_point_between_model_tiers() -> None:
    """模型两档之间可互换（Lateral move）：不改变"会问模型"这个事实，故不被拦。"""
    config = RoutingConfig(
        decision_points={"placement.section_slot": DecisionPointRoute(route="optional_large_model")}
    )
    route = build_routing(config).route("placement.section_slot")
    assert (route.declared, route.effective) == ("optional_large_model", "optional_large_model")
    assert route.level == "confirm"


def test_config_override_does_not_touch_other_points() -> None:
    config = RoutingConfig(
        decision_points={"dedup.semantic_duplicate": DecisionPointRoute(route="rule")}
    )
    table = build_routing(config)
    assert table.route("layout.decide").effective == "rule"
    assert table.route("placement.section_slot").effective == "small_model"


def test_build_routing_rejects_point_outside_catalog() -> None:
    """配置里出现目录之外的 id 是**声明错误**，不是"忘了实现的点"。"""
    config = RoutingConfig(decision_points={"not.a.point": DecisionPointRoute(route="rule")})
    with pytest.raises(EngineError) as caught:
        build_routing(config)
    assert caught.value.code == ErrorCode.CONFIG_INVALID
    assert "not.a.point" in caught.value.message


def test_route_lookup_rejects_unknown_point() -> None:
    """路由表缺一条也是实现缺陷：报 INTERNAL 而不是 KeyError 裸穿。"""
    with pytest.raises(EngineError) as caught:
        build_routing().route("nope")
    assert caught.value.code == ErrorCode.INTERNAL


def test_describe_is_serializable_and_covers_all_points() -> None:
    """验收 41「界面可查看实际路由结果」的数据面：逐点可序列化，且带实际路径与理由。"""
    records = build_routing().describe()
    assert len(records) == len(DECISION_POINTS)
    required = {
        "id",
        "title",
        "declared",
        "owner_agent",
        "output_type",
        "effective",
        "level",
        "strategy",
        "confidence_threshold",
        "reason",
        "fallback",
    }
    for record in records:
        assert required <= set(record)
        assert record["fallback"]["owner"]
    assert {record["id"] for record in records} == set(DECISION_POINT_IDS)


# ---------------------------------------------------------------- 规则路径
def test_dispatch_rule_point_records_non_pending_decision() -> None:
    ctx = _ctx(build_routing(), NullAdapter())
    outcome = dispatch("layout.decide", {"node": "h1"}, ctx, target="h1")
    assert outcome.source == "rule_default"
    assert outcome.status == "auto"
    assert outcome.pending is False
    assert outcome.chosen is None
    assert outcome.attempts == 0

    recorded = ctx.recorder.decisions
    assert len(recorded) == 1
    assert recorded[0].kind == "layout.decide"
    assert recorded[0].target == "h1"
    assert recorded[0].source == "rule_default"


def test_dispatch_human_point_is_manual_and_pending() -> None:
    ctx = _ctx(build_routing(), NullAdapter())
    outcome = dispatch("source.conflict", {}, ctx, target="platelet-count")
    assert outcome.effective == "human"
    assert outcome.source == "manual"
    assert outcome.pending is True
    assert ctx.recorder.pending_decisions()[0].kind == "source.conflict"


def test_dispatch_never_calls_adapter_for_rule_points() -> None:
    """规则点必须**一次模型都不调**——否则"不问模型"只是提示词里的客套话。"""
    adapter = ScriptedAdapter()
    ctx = _ctx(build_routing(), adapter)
    dispatch("template.parse", {}, ctx)
    assert adapter.calls == []


# ------------------------------------------------------- 降级：模型不可用
def test_model_unavailable_degrades_to_rule_and_marks_pending() -> None:
    ctx = _ctx(build_routing(), NullAdapter())
    outcome = dispatch("dedup.semantic_duplicate", {}, ctx, target="c1")
    assert outcome.effective == "rule"
    assert outcome.source == "fallback"
    assert outcome.fallback_used is True
    assert outcome.pending is True
    assert "模型不可用" in outcome.reason
    assert ctx.recorder.model_calls == []
    assert ctx.recorder.token_totals() == (0, 0)


def test_optional_point_without_fallback_pending_stays_auto() -> None:
    """`copy.generate` 的规则默认值是模板化文案（等价可用），故不标待确认。"""
    ctx = _ctx(build_routing(), NullAdapter())
    outcome = dispatch("copy.generate", {}, ctx, target="para-3")
    assert outcome.source == "fallback"
    assert outcome.fallback_used is True
    assert outcome.pending is False
    assert ctx.recorder.pending_decisions() == ()


def test_unscripted_point_degrades_rather_than_raises() -> None:
    """回放器没覆盖该点 → 仍走降级路径，**构建不失败**（需求 5.2.4 硬约束 1）。"""
    ctx = _ctx(build_routing(), ScriptedAdapter(script={}))
    outcome = dispatch("placement.section_slot", {}, ctx, target="s1", candidates=["s1"])
    assert outcome.source == "fallback"
    assert outcome.pending is True


# ------------------------------------------------------------ 模型路径
def test_model_confident_conclusion_is_adopted() -> None:
    adapter = ScriptedAdapter(
        script={"dedup.semantic_duplicate": _outcome(chosen="dup", tokens_in=12, tokens_out=3)}
    )
    ctx = _ctx(build_routing(), adapter)
    outcome = dispatch("dedup.semantic_duplicate", {}, ctx, target="c1", candidates=["dup", "new"])

    assert outcome.source == "model"
    assert outcome.effective == "small_model"
    assert outcome.chosen == "dup"
    assert outcome.confidence == 0.95
    assert outcome.attempts == 1
    assert outcome.pending is False
    assert outcome.candidates == ["dup", "new"]

    calls = ctx.recorder.model_calls
    assert len(calls) == 1
    assert calls[0].decision_point == "dedup.semantic_duplicate"
    assert calls[0].ok is True
    assert calls[0].fallback_used is False
    assert ctx.recorder.token_totals() == (12, 3)


def test_optional_large_model_conclusion_is_pending_confirmation() -> None:
    """`optional_large_model` 的分级是 confirm：模型给结论也要人过一眼。"""
    adapter = ScriptedAdapter(script={"copy.generate": _outcome(chosen="text-a")})
    ctx = _ctx(build_routing(), adapter)
    outcome = dispatch("copy.generate", {}, ctx, target="p1", candidates=["text-a"])
    assert outcome.source == "model"
    assert outcome.status == "pending"
    assert outcome.pending is True


def test_model_call_and_decision_carry_job_id() -> None:
    adapter = ScriptedAdapter(script={"placement.section_slot": _outcome(chosen="s1")})
    ctx = _ctx(build_routing(), adapter, job_id="job-7")
    dispatch("placement.section_slot", {}, ctx, target="s1", candidates=["s1"])
    assert ctx.recorder.model_calls[0].job_id == "job-7"
    assert ctx.recorder.decisions[0].job_id == "job-7"


def test_dispatch_passes_candidates_and_context_to_adapter() -> None:
    """候选注入（需求 5.2.2）：请求里必须真的带上候选集，而不是让模型自己"想出"答案。"""
    adapter = ScriptedAdapter(script={"placement.section_slot": _outcome(chosen="s2")})
    ctx = _ctx(build_routing(), adapter)
    dispatch(
        "placement.section_slot",
        {"section": "overview"},
        ctx,
        target="sec-1",
        candidates=["s1", "s2"],
        context={"budget": 32},
    )
    request = adapter.calls[0]
    assert request.candidates == ["s1", "s2"]
    assert request.context == {"budget": 32}
    assert request.target == "sec-1"
    assert request.inputs == {"section": "overview"}


def test_dispatch_passes_the_configured_point_model_to_adapter() -> None:
    adapter = ScriptedAdapter(script={"placement.section_slot": _outcome(chosen="s1")})
    table = build_routing(
        RoutingConfig(
            decision_points={"placement.section_slot": DecisionPointRoute(model="specialist")}
        )
    )
    dispatch("placement.section_slot", {}, _ctx(table, adapter), candidates=["s1"])
    assert adapter.calls[0].model == "specialist"


# ------------------------------------------- 降级：低置信 / 格式错（重试）
def test_low_confidence_retries_then_falls_back_with_pending() -> None:
    """验收 39 的要害：低置信不得静默采用；重试耗尽后回规则默认值并进裁决队列。"""
    adapter = ScriptedAdapter(
        script={"dedup.semantic_duplicate": _outcome(chosen="dup", confidence=0.2)}
    )
    ctx = _ctx(build_routing(), adapter)
    outcome = dispatch("dedup.semantic_duplicate", {}, ctx, target="c1")

    assert outcome.source == "fallback"
    assert outcome.pending is True
    assert outcome.fallback_used is True
    assert outcome.attempts == DEFAULT_MAX_RETRIES + 1
    assert len(adapter.calls) == DEFAULT_MAX_RETRIES + 1
    assert "置信度" in outcome.reason
    assert ctx.recorder.silent_adoptions() == ()


def test_retry_carries_previous_failure_reason() -> None:
    """不带原因的重试只是把同一个错误问三遍（DD-12.4）。"""
    adapter = ScriptedAdapter(
        script={"dedup.semantic_duplicate": _outcome(chosen="dup", confidence=0.1)}
    )
    ctx = _ctx(build_routing(), adapter)
    dispatch("dedup.semantic_duplicate", {}, ctx, target="c1")
    first, second, third = adapter.calls
    assert first.feedback == []
    assert len(second.feedback) == 1 and "置信度" in second.feedback[0]
    assert len(third.feedback) == 2


def test_missing_confidence_is_treated_as_not_met() -> None:
    """模型没给置信度一律按不达标处理——这是保守方向的默认（验收 39）。"""
    adapter = ScriptedAdapter(
        script={"dedup.semantic_duplicate": _outcome(chosen="dup", confidence=None)}
    )
    ctx = _ctx(build_routing(), adapter)
    outcome = dispatch("dedup.semantic_duplicate", {}, ctx, target="c1")
    assert outcome.source == "fallback"
    assert outcome.pending is True
    assert ctx.recorder.silent_adoptions() == ()


def test_missing_confidence_on_optional_point_falls_back_with_pending() -> None:
    """缺失置信度必须待确认，不能因可选文案的回退声明而静默略过。"""
    adapter = ScriptedAdapter(script={"copy.generate": _outcome(chosen="rewrite", confidence=None)})
    ctx = _ctx(build_routing(), adapter)
    outcome = dispatch("copy.generate", {}, ctx, target="p1")
    assert outcome.source == "fallback"
    assert outcome.effective == "rule"
    assert outcome.pending is True
    assert len(adapter.calls) == DEFAULT_MAX_RETRIES + 1
    assert len(ctx.recorder.pending_decisions()) == 1


def test_schema_failure_retries_with_schema_hint() -> None:
    """`schema_ok=False` 与"低置信"是两件事：前者是格式问题，重试有意义。"""
    adapter = ScriptedAdapter(
        script={
            "dedup.semantic_duplicate": AdapterOutcome(
                ok=True, schema_ok=False, detail="缺少 judgement 字段"
            )
        }
    )
    ctx = _ctx(build_routing(), adapter)
    outcome = dispatch("dedup.semantic_duplicate", {}, ctx, target="c1")
    assert adapter.calls[1].feedback[0].startswith("输出不符合 Schema")
    assert "缺少 judgement 字段" in adapter.calls[1].feedback[0]
    assert outcome.source == "fallback"


def test_failed_call_is_recorded_with_fallback_flag() -> None:
    adapter = ScriptedAdapter(
        script={
            "placement.section_slot": AdapterOutcome(
                ok=False, schema_ok=False, error=ErrorCode.MODEL_UNAVAILABLE, detail="端点不可达"
            )
        }
    )
    ctx = _ctx(build_routing(), adapter, job_id="job-9")
    outcome = dispatch("placement.section_slot", {}, ctx, target="s1", candidates=["s1"])
    calls = ctx.recorder.model_calls
    assert len(calls) == 1
    assert calls[0].ok is False
    assert calls[0].fallback_used is True
    assert calls[0].decision_point == "placement.section_slot"
    assert outcome.pending is True


def test_zero_retry_budget_stops_after_one_call() -> None:
    adapter = ScriptedAdapter(
        script={"dedup.semantic_duplicate": _outcome(chosen="dup", confidence=0.0)}
    )
    ctx = _ctx(build_routing(), adapter, max_retries=0)
    outcome = dispatch("dedup.semantic_duplicate", {}, ctx, target="c1")
    assert len(adapter.calls) == 1
    assert outcome.attempts == 1
    assert outcome.source == "fallback"


def test_confidence_exactly_at_threshold_is_accepted() -> None:
    """阈值是闭区间下界：等于阈值即达标（写死"大于"会让边界永远进裁决队列）。"""
    config = RoutingConfig(
        decision_points={"dedup.semantic_duplicate": DecisionPointRoute(confidence_threshold=0.8)}
    )
    adapter = ScriptedAdapter(
        script={"dedup.semantic_duplicate": _outcome(chosen="dup", confidence=0.8)}
    )
    ctx = _ctx(build_routing(config), adapter)
    outcome = dispatch("dedup.semantic_duplicate", {}, ctx, target="c1")
    assert outcome.source == "model"
    assert len(adapter.calls) == 1


def test_threshold_from_config_is_the_one_enforced() -> None:
    """逐决策点阈值必须**真的生效**：配置调高后，同样的置信度变成不达标。"""
    config = RoutingConfig(
        decision_points={"dedup.semantic_duplicate": DecisionPointRoute(confidence_threshold=0.99)}
    )
    adapter = ScriptedAdapter(
        script={"dedup.semantic_duplicate": _outcome(chosen="dup", confidence=0.95)}
    )
    ctx = _ctx(build_routing(config), adapter)
    outcome = dispatch("dedup.semantic_duplicate", {}, ctx, target="c1")
    assert outcome.source == "fallback"


# ---------------------------------------------------------------- 留痕
def test_recording_happens_before_returning() -> None:
    """留痕在返回之前完成：一次没被记下来的决策等于没发生过（NFR-15）。"""
    ctx = _ctx(build_routing(), NullAdapter())
    outcome = dispatch("dedup.semantic_duplicate", {}, ctx, target="c1")
    assert len(ctx.recorder.decisions) == 1
    assert ctx.recorder.decisions[0].chosen == outcome.chosen


def test_decision_ids_are_deterministic_across_identical_runs() -> None:
    """标识由内容推导，不由随机数或计数器：同一次作业重跑得到同一批 id，可比对。"""
    adapter_a = ScriptedAdapter(script={"dedup.semantic_duplicate": _outcome(chosen="dup")})
    adapter_b = ScriptedAdapter(script={"dedup.semantic_duplicate": _outcome(chosen="dup")})
    first = _ctx(build_routing(), adapter_a)
    second = _ctx(build_routing(), adapter_b)
    dispatch("dedup.semantic_duplicate", {}, first, target="c1")
    dispatch("dedup.semantic_duplicate", {}, second, target="c1")
    assert first.recorder.decisions[0].decision_id == second.recorder.decisions[0].decision_id
    assert first.recorder.model_calls[0].call_id == second.recorder.model_calls[0].call_id


def test_repeated_decisions_in_one_job_get_distinct_ids() -> None:
    """同一作业内对同一对象的重复同结论决策必须有不同标识（序号参与推导）。"""
    adapter = ScriptedAdapter(script={"dedup.semantic_duplicate": _outcome(chosen="dup")})
    ctx = _ctx(build_routing(), adapter)
    dispatch("dedup.semantic_duplicate", {}, ctx, target="c1")
    dispatch("dedup.semantic_duplicate", {}, ctx, target="c1")
    ids = [item.decision_id for item in ctx.recorder.decisions]
    assert len(set(ids)) == 2


def test_decisions_carry_candidate_options() -> None:
    ctx = _ctx(build_routing(), NullAdapter())
    dispatch("placement.section_slot", {}, ctx, target="s1", candidates=["s1", "s2"])
    options = ctx.recorder.decisions[0].options
    assert [option.value for option in options] == ["s1", "s2"]


def test_recorder_describe_shape() -> None:
    adapter = ScriptedAdapter(script={"dedup.semantic_duplicate": _outcome(chosen="dup")})
    ctx = _ctx(build_routing(), adapter, job_id="job-3")
    dispatch("layout.decide", {}, ctx, target="h1")
    dispatch("dedup.semantic_duplicate", {}, ctx, target="c1")
    summary = ctx.recorder.describe()
    assert summary["job_id"] == "job-3"
    assert summary["decisions"] == 2
    assert summary["by_decision_point"] == {"layout.decide": 1, "dedup.semantic_duplicate": 1}
    assert summary["model_route"] == 1
    assert summary["model_calls"] == 1


def test_decision_outcome_is_serializable() -> None:
    """结论要能进报告与界面：`FormatModel` 序列化不报错，且带上声明与实际两条路径。"""
    ctx = _ctx(build_routing(), NullAdapter())
    outcome = dispatch("review.document", {}, ctx, target="doc")
    payload = outcome.model_dump()
    assert payload["declared"] == "optional_large_model"
    assert payload["effective"] == "rule"
    assert isinstance(DecisionOutcome.model_validate(payload), DecisionOutcome)
