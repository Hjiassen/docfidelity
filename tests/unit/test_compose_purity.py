"""三条 IR 硬约束自检（NFR-28 / 验收 34 / 附录 D「IR 纯净」）。"""

from __future__ import annotations

import json

import pytest

from docfidelity.compose.purity import (
    PURITY_RULE_ID,
    assert_ir_clean,
    check_ir_purity,
    ir_payload,
    ir_surface,
)
from docfidelity.contracts.errors import EngineError, ErrorCode
from docfidelity.contracts.ir import ContentIR, ContentUnit

pytestmark = pytest.mark.unit


def _ir(*units: ContentUnit) -> ContentIR:
    return ContentIR(
        project_id="demo",
        doc_key="detail",
        profile_id="p",
        template_ref="TPL@1.0",
        units=list(units),
    )


def test_rule_id_matches_the_builtin_gate_table() -> None:
    assert PURITY_RULE_ID == "FR-M5-17"


def test_clean_ir_reports_no_hits() -> None:
    ir = _ir(
        ContentUnit(unit_id="c1", type="heading", level=1, text="总览"),
        ContentUnit(
            unit_id="u1",
            type="table",
            fields={"columns": ["名称", "类型"], "rows": [["甲", "str"]], "caption_slot": 0},
        ),
        ContentUnit(unit_id="f1", type="figure", fields={"image_ref": "shot", "dpi": 96}),
    )

    assert check_ir_purity(ir) == []


def test_style_token_in_a_value_is_a_hit() -> None:
    """``twips`` 这类 token 只会来自样式层，出现在任何位置都说明有东西漏进来了。"""
    ir = _ir(ContentUnit(unit_id="u1", type="para", text="宽度 9000 twips", fields={}))

    assert check_ir_purity(ir) == ["twips"]


def test_style_token_in_a_field_key_is_a_hit() -> None:
    ir = _ir(ContentUnit(unit_id="u1", type="para", fields={"styleId": "Heading1"}))

    assert check_ir_purity(ir) == ["styleId"]


def test_trace_and_origin_are_whitelisted() -> None:
    """``trace`` 存 ``kind:key`` 引用句柄，是 DD-3.2（2）已知边界，剔除后再检索。"""
    ir = _ir(ContentUnit(unit_id="u1", type="para", text="正文", trace=["x:pStyle"]))

    assert check_ir_purity(ir) == []
    assert "pStyle" not in json.dumps(ir_payload(ir), ensure_ascii=False)
    assert "pStyle" in json.dumps(ir.model_dump(mode="json"), ensure_ascii=False)


def test_extra_word_hits_only_the_engine_surface() -> None:
    """外部词表只查引擎自有的那层表面；事实**取值**里出现类型名不算违规。"""
    as_value = _ir(ContentUnit(unit_id="u1", type="para", text="详细设计说明书", fields={}))
    as_key = _ir(ContentUnit(unit_id="u1", type="para", fields={"详细设计": "x"}))

    assert check_ir_purity(as_value, extra_words=["详细设计"]) == []
    assert check_ir_purity(as_key, extra_words=["详细设计"]) == ["详细设计"]


def test_assert_raises_internal_error_on_style_trace() -> None:
    ir = _ir(ContentUnit(unit_id="u1", type="para", fields={"caption": "w:pStyle"}))

    with pytest.raises(EngineError) as error:
        assert_ir_clean(ir)

    assert error.value.code == ErrorCode.INTERNAL
    assert error.value.where["tokens"] == ["w:", "pStyle"]
    assert error.value.remediation


def test_assert_passes_on_clean_ir() -> None:
    assert_ir_clean(_ir(ContentUnit(unit_id="u1", type="para", text="普通正文", fields={})))


def test_ir_surface_lists_structure_and_field_keys_but_not_values() -> None:
    ir = _ir(
        ContentUnit(
            unit_id="u1",
            type="table",
            fields={"columns": ["甲"], "rows": [["敏感取值"]]},
        )
    )

    surface = ir_surface(ir)

    assert surface == ["unit_id=u1", "type=table", "field=columns", "field=rows"]
    assert "敏感取值" not in surface


def test_the_ir_wordlist_excludes_type_branch_literals() -> None:
    """``ir_words()`` **不含** ``branch_literal`` 组。

    回归：``compose`` 的阶段体曾把**整份**词表喂进来。``branch_literal``
    （``endpoint`` / ``table`` / ``field`` / ``parameter`` …）管的是"形如
    ``x.kind == "..."`` 的类型分支"，只在扫源码 AST 时有意义；IR 检查只能做**子串**
    匹配，并进来会让每一份合法 IR 都被判 blocker——``unit_id`` 由档案的 ``for_each``
    派生、``type`` 就是 ``UnitType`` 的取值。表现是所有构建都挂着 4 条 blocker，
    而产物本身是好的。
    """
    from docfidelity.qa.type_agnostic import ir_words

    words = ir_words()

    assert "endpoint" not in words
    assert "table" not in words
    assert "field" not in words
    assert "parameter" not in words
    assert "详细设计" in words  # doc_type_name 仍在
    assert "字段" in words  # domain_concept 仍在


def test_a_legitimate_ir_survives_the_wordlist_check() -> None:
    """一份**合法** IR 在完整词表下一条命中都不该有。

    ``unit_id`` 由档案的 ``for_each`` 派生（``endpoint`` 是档案里的领域类型名），
    这不是引擎的词汇——引擎只是把它当字符串用。
    """
    from docfidelity.qa.type_agnostic import ir_words, scan_ir

    ir = _ir(
        ContentUnit(
            unit_id="endpoint:order:/orders",
            type="table",
            fields={"columns": ["path", "method"], "rows": [["/orders", "POST"]]},
        )
    )

    assert check_ir_purity(ir, extra_words=sorted(ir_words())) == []
    assert scan_ir(ir) == []
