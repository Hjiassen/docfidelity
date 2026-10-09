"""五 Agent 与决策点目录的声明校验（DD-12.1 / DD-12.3 / 需求 5.2.3、5.4）。

这里验的是**声明**，不是行为。声明错误的可怕之处在于它不会自己暴露：
一个指向不存在工具的 Agent 只是在被调用时"刚好失败"，一条归属写错的决策点
只是安安静静走了另一条路。故本模块的主线是**正向断言 + 对立断言**成对出现——
只断言"合法声明能过"的实现，在把校验函数改成 `return None` 之后依然全绿。
"""

from __future__ import annotations

import pytest

from docfidelity.agents import (
    AGENT_NAMES,
    AGENT_OUTPUT_TYPES,
    AGENT_SPECS,
    DECISION_POINTS,
    HANDOFF_CHAIN,
    INSTRUCTIONS_VERSION,
    MAX_TOOLS_PER_AGENT,
    TOOLS,
    agent_spec,
    decision_point,
    descriptions,
    instructions_digest,
)
from docfidelity.agents.catalog import AgentSpec, validate_agent_spec
from docfidelity.agents.instructions import AGENT_INSTRUCTIONS, COMMON_RULES, agent_instructions
from docfidelity.contracts.base import FormatModel
from docfidelity.contracts.errors import EngineError, ErrorCode

pytestmark = pytest.mark.unit


# --------------------------------------------------------------- 五 Agent
def test_five_agents_in_handoff_order() -> None:
    """五 Agent 按 handoff 链序排列，且名字集合与 outputs 的名单一致。"""
    assert [spec.name for spec in AGENT_SPECS] == list(HANDOFF_CHAIN)
    assert set(AGENT_NAMES) == set(HANDOFF_CHAIN)
    assert len(AGENT_SPECS) == 5


def test_handoff_chain_is_closed() -> None:
    """链上是"下一个"，末位无后继，且唯一的回边属于 Inspector。

    断言方向要写全：只断言"每条的 handoff 都在链上"的话，五条互相指也能过。
    """
    for index, spec in enumerate(AGENT_SPECS):
        expected = HANDOFF_CHAIN[index + 1] if index + 1 < len(HANDOFF_CHAIN) else None
        assert spec.handoff == expected, spec.name
    assert AGENT_SPECS[-1].rework_target == "composer"
    assert sum(1 for spec in AGENT_SPECS if spec.rework_target) == 1


def test_each_agent_is_single_duty_with_bounded_tools() -> None:
    """单一职责 + 工具数上限（NFR-25）。"""
    for spec in AGENT_SPECS:
        assert spec.single_duty.endswith("。"), spec.name
        assert 0 < len(spec.tools) <= MAX_TOOLS_PER_AGENT, spec.name
        assert len(set(spec.tools)) == len(spec.tools), spec.name
        assert set(spec.tools) <= set(TOOLS), spec.name


def test_output_types_are_distinct_models() -> None:
    """一 Agent 一输出，且五个输出互不相同（DD-12.1 的单一职责在输出侧也成立）。"""
    types = [spec.output_type() for spec in AGENT_SPECS]
    assert all(issubclass(item, FormatModel) for item in types)
    assert len({id(item) for item in types}) == len(types)
    for spec in AGENT_SPECS:
        assert AGENT_OUTPUT_TYPES[spec.name] is spec.output_type()


def test_agent_spec_lookup_rejects_unknown() -> None:
    assert agent_spec("curator").title
    with pytest.raises(EngineError) as caught:
        agent_spec("no-such-agent")
    assert caught.value.code == ErrorCode.INTERNAL


# ------------------------------------------------------------- 决策点目录
def test_decision_points_match_requirement_5_2_3() -> None:
    """决策点归属逐条对应需求 5.2.3 的分级表。

    归属是这张表唯一的实质内容，故逐条写死——用集合比较会让"两条互换归属"通过。
    """
    expected = {
        "layout.decide": "rule",
        "gate.judge": "rule",
        "template.parse": "rule",
        "geometry.compute": "rule",
        "dedup.semantic_duplicate": "small_model",
        "placement.section_slot": "small_model",
        "convention.advisory": "small_model",
        "copy.generate": "optional_large_model",
        "review.document": "optional_large_model",
        "source.conflict": "human",
    }
    assert {point.id: point.kind for point in DECISION_POINTS} == expected


def test_model_routed_points_declare_output_and_others_do_not() -> None:
    """调模型的点必须有输出契约；规则/人工档**不该有**。

    后半句是防"顺手给规则点也配一个 schema"——那会让它看起来像可以问模型，
    下一个人就会真的去问（需求 5.2.3 点名的蔓延方式）。
    """
    for point in DECISION_POINTS:
        if point.kind in ("small_model", "optional_large_model"):
            assert point.output_type is not None, point.id
            assert issubclass(point.output_type, FormatModel), point.id
        else:
            assert point.output_type is None, point.id


def test_every_point_has_owner_or_is_human_routed() -> None:
    for point in DECISION_POINTS:
        if point.kind == "human":
            assert point.owner_agent is None, point.id
        else:
            assert point.owner_agent in AGENT_NAMES, point.id


def test_every_point_declares_fallback_owner_and_budget() -> None:
    """每个点都要有规则默认值的实现位置与上下文预算（NFR-25 / NFR-26）。"""
    for point in DECISION_POINTS:
        assert point.fallback.owner, point.id
        assert point.fallback.detail.endswith("。") or point.fallback.detail, point.id
        assert point.context_budget > 0, point.id
    assert len({point.id for point in DECISION_POINTS}) == len(DECISION_POINTS)


def test_decision_point_lookup_rejects_unknown() -> None:
    assert decision_point("dedup.semantic_duplicate").kind == "small_model"
    with pytest.raises(EngineError) as caught:
        decision_point("not.registered")
    assert caught.value.code == ErrorCode.CONFIG_INVALID
    assert caught.value.remediation


def test_point_records_are_serializable() -> None:
    """`as_record()` 是验收 41 的数据面，字段缺失会让界面显示"归属未知"。"""
    record = decision_point("copy.generate").as_record()
    assert record["declared"] == "optional_large_model"
    assert record["output_type"] == "CopyRewrite"
    assert record["owner_agent"] == "composer"
    assert record["fallback"]["source"] == "template_text"


# --------------------------------------------------- 负向：声明校验真的会拦
def _spec(**overrides: object) -> AgentSpec:
    base = AGENT_SPECS[0]
    data = {name: getattr(base, name) for name in AgentSpec.__dataclass_fields__}
    data.update(overrides)
    return AgentSpec(**data)  # type: ignore[arg-type]


def test_validate_rejects_too_many_tools() -> None:
    spec = _spec(tools=tuple(TOOLS)[: MAX_TOOLS_PER_AGENT + 1])
    with pytest.raises(EngineError) as caught:
        validate_agent_spec(spec)
    assert caught.value.code == ErrorCode.CONFIG_INVALID
    assert "上限" in caught.value.message


def test_validate_rejects_unregistered_tool() -> None:
    with pytest.raises(EngineError) as caught:
        validate_agent_spec(_spec(tools=("no_such_tool",)))
    assert "未登记的工具" in caught.value.message


def test_validate_rejects_unregistered_decision_point() -> None:
    with pytest.raises(EngineError) as caught:
        validate_agent_spec(_spec(decision_points=("not.registered",)))
    assert "未登记的决策点" in caught.value.message


def test_validate_rejects_wrong_handoff_direction() -> None:
    """跳错方向的 handoff 必须被拦——否则"下一步该谁"就没有唯一答案了。"""
    with pytest.raises(EngineError) as caught:
        validate_agent_spec(_spec(handoff="inspector"))
    assert "handoff" in caught.value.message


def test_validate_accepts_real_specs() -> None:
    """正向对照：真声明必须全过（否则上面几条负向用例可能只是"函数总在抛"）。"""
    for spec in AGENT_SPECS:
        validate_agent_spec(spec)


# ------------------------------------------------------------- instructions
def test_instructions_are_short_and_flat() -> None:
    """风格约束（需求 5.2.5）：短句、扁平、单一目标。"""
    assert 2 <= len(COMMON_RULES) <= 6
    for name in AGENT_NAMES:
        text = agent_instructions(name)
        assert AGENT_INSTRUCTIONS[name] in text
        assert all(rule in text for rule in COMMON_RULES)
        assert len(text) < 1200, name


def test_instructions_digest_is_stable_and_tool_sensitive() -> None:
    """摘要必须**对工具描述敏感**（NFR-17：描述与提示词同属被版本化的输入）。"""
    base = instructions_digest(descriptions())
    assert base == instructions_digest(descriptions())
    assert base != instructions_digest({})
    assert base != instructions_digest({**descriptions(), "extra": "改了描述"})
    assert INSTRUCTIONS_VERSION >= 1
