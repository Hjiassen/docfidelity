"""本地结构化输出层（`agents.structured`）的单测。

**不 import openai-agents**：抽取与校验是纯 pydantic + stdlib 的事，
这条边界由 lint-imports 的层次契约保证，也由本文件"没有任何 SDK 替身"体现——
一旦哪天往 `structured` 里塞进 `agents`，这里会先撑不住。
"""

from __future__ import annotations

import pytest
from pydantic import Field

from docfidelity.agents.outputs import DuplicateJudgement, PlacementChoice
from docfidelity.agents.structured import (
    SCHEMA_RULES,
    extract_json_object,
    output_instructions,
    parse_structured,
    schema_hint,
)
from docfidelity.contracts.base import FormatModel

pytestmark = pytest.mark.unit


# --------------------------------------------------------------- 专用模型
class _Deep(FormatModel):
    c: str = ""


class _Mid(FormatModel):
    b: _Deep = Field(default_factory=_Deep)


class _Top(FormatModel):
    a: _Mid = Field(default_factory=_Mid)


# ------------------------------------------------------------------ 形状
def test_schema_hint_lists_fields_with_required_flags() -> None:
    hint = schema_hint(DuplicateJudgement)
    assert "is_duplicate" in hint
    assert "布尔" in hint
    assert hint.count("必填") == 2  # is_duplicate / reason 都无默认值
    assert "共 2 个" in hint


def test_schema_hint_marks_optional_fields_and_literal_values() -> None:
    hint = schema_hint(PlacementChoice)
    assert "- chapter_path：数组<字符串>，可选" in hint
    assert "- reason：字符串，可选" in hint
    assert "- unit_ref：字符串，必填" in hint


def test_schema_hint_never_exposes_format_version() -> None:
    """格式版本由本地确定（模块文档"两条边界"），不能出现在交给模型的字段清单里。

    否则模型会照着填，一旦填出一个自编的主版本号，`FormatModel` 的版本校验会抛出
    一个与"模型没按 schema 作答"完全无关的失败。
    """
    for model in (DuplicateJudgement, PlacementChoice):
        assert "format_version" not in schema_hint(model)


def test_schema_hint_limits_nesting_depth() -> None:
    """嵌套只展开一层：再深只报"对象"，避免提示词被一份大契约撑爆。"""
    hint = schema_hint(_Top)
    assert "- a：对象（b: 对象），可选" in hint
    assert "c" not in hint  # `_Deep` 不再展开


def test_output_instructions_carries_both_rules_and_shape() -> None:
    text = output_instructions(DuplicateJudgement)
    assert text.startswith("输出格式：")
    for rule in SCHEMA_RULES:
        assert rule in text
    assert "is_duplicate" in text


# ------------------------------------------------------------------ 抽取
def test_extract_accepts_a_bare_object() -> None:
    assert extract_json_object('{"a": 1}') == '{"a": 1}'


@pytest.mark.parametrize(
    "text",
    [
        '```json\n{"a": 1}\n```',
        '```\n{"a": 1}\n```',
        '```JSON\n{"a": 1}\n```',
    ],
)
def test_extract_unwraps_markdown_fences(text: str) -> None:
    """围栏是模型最常见的"礼貌包装"，判成格式错等于白白浪费一次重试预算。"""
    assert extract_json_object(text) == '{"a": 1}'


def test_extract_ignores_prose_around_the_object() -> None:
    text = '好的，结论是 {"a": 1} 请查收。'
    assert extract_json_object(text) == '{"a": 1}'


def test_extract_does_not_stop_at_a_brace_inside_a_string() -> None:
    """用正则非贪婪匹配会在字符串里的 ``}`` 处提前截断，截出一个"合法"的错误载荷。"""
    text = '{"text": "公式是 {x}", "n": 2}'
    assert extract_json_object(text) == text


def test_extract_handles_escaped_quotes() -> None:
    text = '{"text": "他说 \\"好\\" 然后走了", "n": 1}'
    assert extract_json_object(text) == text


def test_extract_keeps_nested_objects_whole() -> None:
    text = '前方 {"a": {"b": {"c": 1}}} 后方'
    assert extract_json_object(text) == '{"a": {"b": {"c": 1}}}'


def test_extract_takes_the_first_of_several_objects() -> None:
    """取第一个而不是最后一个：重试提示里会回显上一次的载荷，取错会"继承"错误结论。"""
    assert extract_json_object('{"a": 1} 与 {"a": 2}') == '{"a": 1}'


def test_extract_falls_back_to_the_whole_text_when_the_fence_has_no_object() -> None:
    text = '```\n说明：见下\n```\n{"a": 1}'
    assert extract_json_object(text) == '{"a": 1}'


@pytest.mark.parametrize("text", ["", "   ", "抱歉，我做不到", "[1, 2, 3]", "```\n说明\n```"])
def test_extract_returns_none_when_there_is_no_object(text: str) -> None:
    assert extract_json_object(text) is None


# ------------------------------------------------------------------ 解析
def test_parse_returns_the_validated_payload() -> None:
    parsed = parse_structured(DuplicateJudgement, '{"is_duplicate": true, "reason": "同功能"}')
    assert parsed.ok is True
    assert parsed.reason == ""
    assert parsed.payload is not None
    assert parsed.payload["is_duplicate"] is True


def test_parse_reports_a_missing_required_field_actionably() -> None:
    """原因要**能照着改**：pydantic 原文 "Field required" 说不出该补哪个字段。"""
    parsed = parse_structured(DuplicateJudgement, '{"reason": "重复"}')
    assert parsed.payload is None
    assert "缺少必填字段" in parsed.reason
    assert "is_duplicate" in parsed.reason


def test_parse_reports_an_unexpected_field_actionably() -> None:
    parsed = parse_structured(
        DuplicateJudgement, '{"is_duplicate": true, "reason": "x", "bonus": 1}'
    )
    assert parsed.payload is None
    assert "bonus" in parsed.reason


def test_parse_reports_a_type_error_with_its_location() -> None:
    parsed = parse_structured(DuplicateJudgement, '{"is_duplicate": "也许", "reason": "x"}')
    assert parsed.payload is None
    assert "is_duplicate" in parsed.reason


def test_parse_reports_json_syntax_error_with_position() -> None:
    parsed = parse_structured(DuplicateJudgement, '{"is_duplicate": true,}')
    assert parsed.payload is None
    assert "JSON 语法错误" in parsed.reason


def test_parse_reports_when_the_reply_holds_no_object() -> None:
    """数组也不是我们要的形态——本层只认对象，别的都算"没找到"。"""
    parsed = parse_structured(DuplicateJudgement, "[1, 2]")
    assert parsed.payload is None
    assert "找不到 JSON 对象" in parsed.reason


def test_parse_reports_when_no_object_is_found_and_shows_the_head() -> None:
    """附一小段原文开头：人看一眼就知道模型到底答了什么，而不是只知道"失败了"。"""
    parsed = parse_structured(DuplicateJudgement, "我觉得这一段是重复的，但没有 JSON")
    assert parsed.payload is None
    assert "找不到 JSON 对象" in parsed.reason
    assert "我觉得这一段是重复的" in parsed.reason


def test_parse_ignores_a_model_supplied_format_version() -> None:
    parsed = parse_structured(
        DuplicateJudgement,
        '{"format_version": "9.9.9", "is_duplicate": true, "reason": "x"}',
    )
    assert parsed.ok is True
    assert parsed.payload is not None
    assert parsed.payload["format_version"] == "1.0.0"
