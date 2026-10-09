"""追溯矩阵（FR-M6-07 / FR-M5-10）。"""

from __future__ import annotations

import pytest

from docfidelity.compose.traces import TRACE_RULE_ID, build_traces, used_object_refs
from docfidelity.contracts.domain import DomainObject, DomainObjectOrigin
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


def _obj(kind: str, key: str, *, source: str = "openapi", locator: str = "L1") -> DomainObject:
    return DomainObject(
        kind=kind,
        key=[key],
        fields={"name": key},
        origin=DomainObjectOrigin(source=source, locator=locator),
    )


def test_rule_id_matches_the_builtin_gate_table() -> None:
    assert TRACE_RULE_ID == "FR-M5-10"


def test_edges_carry_source_and_locator() -> None:
    unit = ContentUnit(unit_id="u1", type="para", trace=["dish:番茄炒蛋"])
    ir = _ir(unit)

    matrix = build_traces(ir, [_obj("dish", "番茄炒蛋", source="manual", locator="Sheet1!A2")])

    assert len(matrix.edges) == 1
    assert matrix.edges[0].unit_id == "u1"
    assert matrix.edges[0].object_ref == "dish:番茄炒蛋"
    assert matrix.edges[0].source == "manual"
    assert matrix.edges[0].locator == "Sheet1!A2"
    assert matrix.orphans == []
    assert matrix.unused == []


def test_unit_without_trace_is_an_orphan() -> None:
    ir = _ir(
        ContentUnit(unit_id="c1", type="heading", level=1, text="总览"),
        ContentUnit(unit_id="u1", type="para", text="无来源", trace=[]),
    )
    dish = _obj("dish", "番茄炒蛋")

    matrix = build_traces(ir, [dish])

    assert matrix.orphans == ["c1", "u1"]
    assert matrix.unused == ["dish:番茄炒蛋"]


def test_dangling_reference_is_reported_with_a_marker() -> None:
    """悬挂引用与"本来就没有来源"是两回事，写法上必须能区分。"""
    ir = _ir(ContentUnit(unit_id="u1", type="para", trace=["dish:不存在的菜"]))

    matrix = build_traces(ir, [_obj("dish", "番茄炒蛋")])

    assert "u1->dish:不存在的菜" in matrix.orphans
    assert "u1" not in matrix.orphans


def test_mixed_resolvable_and_dangling_trace_is_not_an_orphan_unit() -> None:
    unit = ContentUnit(unit_id="u1", type="para", trace=["dish:番茄炒蛋", "dish:幽灵"])
    ir = _ir(unit)

    matrix = build_traces(ir, [_obj("dish", "番茄炒蛋")])

    assert [edge.object_ref for edge in matrix.edges] == ["dish:番茄炒蛋"]
    assert matrix.orphans == ["u1->dish:幽灵"]


def test_duplicate_trace_refs_do_not_break_the_matrix() -> None:
    """``TraceMatrix`` 的校验器直接拒收重复边，故必须先按 (unit_id, ref) 去重。"""
    unit = ContentUnit(unit_id="u1", type="para", trace=["dish:甲", "dish:甲", "dish:乙"])
    ir = _ir(unit)

    matrix = build_traces(ir, [_obj("dish", "甲"), _obj("dish", "乙")])

    assert [edge.object_ref for edge in matrix.edges] == ["dish:甲", "dish:乙"]


def test_unused_lists_objects_that_never_reached_the_document() -> None:
    ir = _ir(ContentUnit(unit_id="u1", type="para", trace=["dish:甲"]))

    matrix = build_traces(ir, [_obj("dish", "甲"), _obj("dish", "乙")])

    assert matrix.unused == ["dish:乙"]


def test_untraceable_structural_units_are_excluded_from_orphans() -> None:
    """骨架章节标题来自档案而非事实，计入 orphans 只会制造固定噪声。"""
    ir = _ir(
        ContentUnit(unit_id="chapter#0", type="heading", level=1, text="总览"),
        ContentUnit(unit_id="u1", type="para", trace=["dish:甲"]),
    )

    matrix = build_traces(ir, [_obj("dish", "甲")], untraceable=["chapter#0"])

    assert matrix.orphans == []
    assert [edge.unit_id for edge in matrix.edges] == ["u1"]


def test_used_object_refs_is_deduped_in_walk_order() -> None:
    child = ContentUnit(unit_id="u2", type="para", trace=["dish:甲"])
    parent = ContentUnit(unit_id="u1", type="para", trace=["dish:乙", "dish:甲"], children=[child])
    ir = _ir(parent)

    assert used_object_refs(ir) == ["dish:乙", "dish:甲"]
