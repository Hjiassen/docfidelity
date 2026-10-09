"""槽位校验（DD-8.3 / FR-M3-15）。"""

from __future__ import annotations

import pytest

from docfidelity.compose.slots import (
    DEFAULT_SLOT,
    SlotPolicy,
    check_slots,
    orphan_slots,
    referenced_slot_ids,
)
from docfidelity.contracts.ir import ContentUnit, UnitType
from docfidelity.contracts.profile import ChapterSkeletonItem
from docfidelity.contracts.qa import Finding
from docfidelity.contracts.template import SlotSpec

pytestmark = pytest.mark.unit


def _slot(
    slot_id: str,
    *,
    required: bool = True,
    unit_types: list[str] | None = None,
    locator: str = "chapter[1]",
) -> SlotSpec:
    return SlotSpec(
        slot_id=slot_id,
        required=required,
        unit_types=list(unit_types or []),
        locator=locator,
    )


def _chapter(title: str, *slots: SlotSpec) -> ChapterSkeletonItem:
    return ChapterSkeletonItem(level=1, title=title, slots=list(slots))


def _unit(unit_id: str, unit_type: UnitType = "table") -> ContentUnit:
    return ContentUnit(unit_id=unit_id, type=unit_type)


def _rules(findings: list[Finding]) -> list[str]:
    return [item.rule_id for item in findings]


def test_required_empty_slot_is_major() -> None:
    chapters = [_chapter("总览", _slot("overview", unit_types=["para"]))]

    findings = check_slots(chapters, {})

    assert len(findings) == 1
    assert findings[0].severity == "major"
    assert findings[0].rule_id == "FR-M3-15"
    assert findings[0].location.chapter_path == ["总览"]
    assert "overview" in findings[0].message


def test_escalation_promotes_required_empty_slot_to_blocker() -> None:
    chapters = [_chapter("总览", _slot("overview"))]

    findings = check_slots(chapters, {}, policy=SlotPolicy(escalate_required_to_blocker=True))

    assert [item.severity for item in findings] == ["blocker"]


def test_optional_empty_slot_is_silent() -> None:
    chapters = [_chapter("附录", _slot("appendix", required=False))]

    assert check_slots(chapters, {}) == []


def test_unit_type_outside_declaration_is_major() -> None:
    chapters = [_chapter("总览", _slot("overview", unit_types=["para"]))]
    placed = {"overview": [_unit("u1", "table")]}

    findings = check_slots(chapters, placed)

    assert len(findings) == 1
    assert findings[0].severity == "major"
    assert "table" in findings[0].message
    assert findings[0].evidence == "unit_types=['para']"


def test_open_unit_types_accept_anything() -> None:
    chapters = [_chapter("总览", _slot("overview", unit_types=[]))]
    placed = {"overview": [_unit("u1", "figure")]}

    assert check_slots(chapters, placed) == []


def test_duplicate_slot_id_is_major_and_reported_once() -> None:
    chapters = [
        _chapter("甲", _slot("dup")),
        _chapter("乙", _slot("dup")),
        _chapter("丙", _slot("dup")),
    ]

    findings = [item for item in check_slots(chapters, {}) if "声明了" in item.message]

    assert len(findings) == 1
    assert findings[0].severity == "major"
    assert "3 次" in findings[0].message


def test_referenced_slot_ids_are_deduped_in_declaration_order() -> None:
    chapters = [_chapter("甲", _slot("b"), _slot("a")), _chapter("乙", _slot("b"))]

    assert referenced_slot_ids(chapters) == ["b", "a"]


def test_orphan_slots_lists_only_unreferenced_non_empty_keys() -> None:
    chapters = [_chapter("甲", _slot("apis"))]
    placed = {"apis": [_unit("u1")], DEFAULT_SLOT: [_unit("u2")], "empty": []}

    assert orphan_slots(placed, chapters) == [DEFAULT_SLOT]


def test_rule_ids_are_reported_from_one_place() -> None:
    chapters = [_chapter("甲", _slot("apis", unit_types=["table"]), _slot("overview"))]
    placed = {"apis": [_unit("u1", "para")]}

    assert _rules(check_slots(chapters, placed)) == ["FR-M3-15", "FR-M3-15"]
