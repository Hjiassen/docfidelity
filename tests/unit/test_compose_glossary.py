"""术语变量与连读重复（DD-8.2 / FR-M3-09 / FR-M5-08）。"""

from __future__ import annotations

import pytest

from docfidelity.compose.glossary import (
    apply_glossary_to_ir,
    apply_variables,
    fix_concat_duplication,
    glossary_apply,
    unresolved_variables,
)
from docfidelity.contracts.config import Terminology, TerminologyEntry
from docfidelity.contracts.ir import ContentIR, ContentUnit
from tests.fixtures.compose_fixture import terminology

pytestmark = pytest.mark.unit


def test_variable_is_replaced_by_canonical_form() -> None:
    assert apply_variables("{{软件名}}的使用说明", terminology()) == "XXX分析仪的使用说明"


def test_variable_tolerates_inner_whitespace() -> None:
    assert apply_variables("{{ 软件名 }}", terminology()) == "XXX分析仪"


def test_forbidden_form_is_normalised_to_canonical() -> None:
    assert apply_variables("YYY分析仪已就绪", terminology()) == "XXX分析仪已就绪"


def test_unknown_variable_is_left_intact_and_reported() -> None:
    text = "{{未登记项}}与{{软件名}}"

    assert apply_variables(text, terminology()) == "{{未登记项}}与XXX分析仪"
    assert unresolved_variables(text, terminology()) == ["未登记项"]


def test_concat_duplication_is_merged() -> None:
    """术语「XXX分析仪」+ 原文「分析仪整体软件」→ 「XXX分析仪整体软件」。"""
    assert glossary_apply("{{软件名}}分析仪整体软件", terminology()) == "XXX分析仪整体软件"


def test_duplication_created_by_forbidden_form_is_also_merged() -> None:
    assert glossary_apply("YYY分析仪分析仪整体软件", terminology()) == "XXX分析仪整体软件"


def test_single_character_overlap_is_not_merged() -> None:
    """单字重合是正常中文（「分析仪」+「仪器」），合并会粘出一个新词。"""
    terms = Terminology(entries=[TerminologyEntry(term="仪器", canonical="分析仪")])

    assert fix_concat_duplication("分析仪仪器", terms) == "分析仪仪器"


def test_non_overlapping_text_is_untouched() -> None:
    assert fix_concat_duplication("XXX分析仪整体软件", terminology()) == "XXX分析仪整体软件"


def test_glossary_apply_is_idempotent() -> None:
    once = glossary_apply("{{软件名}}分析仪整体软件", terminology())

    assert glossary_apply(once, terminology()) == once


def test_scope_none_disables_replacement_but_still_reports_placeholders() -> None:
    """术语表关闭时不替换，但 ``{{…}}`` 仍要报为未解析。

    关闭术语表不会让占位符消失——它会**原样出现在正文里**。此时若不报，一个必然
    出现在成品中的缺陷就彻底隐形了。
    """
    terms = terminology().model_copy(update={"scope": "none"})
    text = "{{软件名}}YYY分析仪"

    assert apply_variables(text, terms) == text
    assert glossary_apply(text, terms) == text
    assert unresolved_variables(text, terms) == ["软件名"]


def test_apply_glossary_to_ir_covers_every_readable_field() -> None:
    ir = ContentIR(
        project_id="demo",
        doc_key="detail",
        profile_id="p",
        template_ref="TPL@1.0",
        units=[
            ContentUnit(
                unit_id="u1",
                type="heading",
                level=1,
                text="{{软件名}}总览",
                children=[
                    ContentUnit(unit_id="u2", type="para", text="YYY分析仪已就绪"),
                    ContentUnit(
                        unit_id="u3",
                        type="bullets",
                        fields={"items": ["{{模块名}}要点", "无术语"]},
                    ),
                    ContentUnit(
                        unit_id="u4",
                        type="table",
                        fields={
                            "columns": ["{{模块名}}"],
                            "rows": [["YYY分析仪"], ["普通值"]],
                            "caption": "{{软件名}}参数",
                            "caption_slot": 0,
                        },
                    ),
                    ContentUnit(
                        unit_id="u5",
                        type="figure",
                        fields={"image_ref": "shot", "caption": "YYY分析仪截图", "dpi": 96},
                    ),
                ],
            )
        ],
    )

    apply_glossary_to_ir(ir, terminology())

    assert ir.find("u1").text == "XXX分析仪总览"  # type: ignore[union-attr]
    assert ir.find("u2").text == "XXX分析仪已就绪"  # type: ignore[union-attr]
    assert ir.find("u3").fields["items"] == ["订单模块要点", "无术语"]  # type: ignore[union-attr]
    table = ir.find("u4")
    assert table is not None
    assert table.fields["columns"] == ["订单模块"]
    assert table.fields["rows"] == [["XXX分析仪"], ["普通值"]]
    assert table.fields["caption"] == "XXX分析仪参数"
    figure = ir.find("u5")
    assert figure is not None
    assert figure.fields["caption"] == "XXX分析仪截图"


def test_apply_glossary_to_ir_leaves_trace_untouched() -> None:
    """``trace`` 是引用句柄，改写它会让追溯链指向不存在的对象。"""
    unit = ContentUnit(unit_id="u1", type="para", text="YYY分析仪", trace=["x:YYY分析仪"])
    ir = ContentIR(
        project_id="demo", doc_key="d", profile_id="p", template_ref="TPL@1.0", units=[unit]
    )

    apply_glossary_to_ir(ir, terminology())

    assert unit.trace == ["x:YYY分析仪"]
