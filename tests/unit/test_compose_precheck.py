"""档案 × 模板包 可满足性预检（DD-7.5 第 4 条）。"""

from __future__ import annotations

import pytest

from docfidelity.compose.precheck import PRECHECK_RULE_ID, satisfiable
from docfidelity.contracts.profile import (
    AppliesStatement,
    Bindings,
    ChapterSkeletonItem,
    DocTypeProfile,
    GenreSpec,
)
from docfidelity.contracts.template import SlotSpec
from tests.fixtures.compose_fixture import template_package

pytestmark = pytest.mark.unit


def _slot(
    slot_id: str,
    locator: str,
    *,
    unit_types: list[str] | None = None,
    required: bool = True,
) -> SlotSpec:
    return SlotSpec(
        slot_id=slot_id,
        required=required,
        unit_types=list(unit_types or []),
        locator=locator,
    )


def _profile(*chapters: ChapterSkeletonItem) -> DocTypeProfile:
    return DocTypeProfile(
        profile_id="probe",
        doc_type="probe",
        title="探针",
        applies=AppliesStatement(
            layout_source="TPL-API@1.0",
            structure_enumerable="骨架见 genre",
            content_traceable="事实源为夹具",
        ),
        genre=GenreSpec(chapter_skeleton=list(chapters)),
        bindings=Bindings(template_packages=["TPL-API@1.0"]),
    )


def _chapter(title: str, *slots: SlotSpec) -> ChapterSkeletonItem:
    return ChapterSkeletonItem(level=1, title=title, slots=list(slots))


def test_matching_slots_report_nothing() -> None:
    profile = _profile(
        _chapter("总览", _slot("overview", "chapter[1]", unit_types=["para"])),
        _chapter("清单", _slot("apis", "chapter[2]", unit_types=["heading", "table"])),
    )
    package = template_package(slots=[_slot("c1", "chapter[1]"), _slot("c2", "chapter[2]")])

    assert satisfiable(profile, package) == []


def test_missing_locator_is_major() -> None:
    profile = _profile(
        _chapter("总览", _slot("overview", "chapter[1]")),
        _chapter("清单", _slot("apis", "chapter[2]")),
    )
    package = template_package(slots=[_slot("c1", "chapter[1]")])

    findings = satisfiable(profile, package)

    assert [item.severity for item in findings] == ["major"]
    assert findings[0].rule_id == PRECHECK_RULE_ID
    assert "chapter[2]" in findings[0].message
    assert findings[0].location.chapter_path == ["清单"]


def test_extra_template_slot_is_minor() -> None:
    profile = _profile(_chapter("总览", _slot("overview", "chapter[1]")))
    package = template_package(slots=[_slot("c1", "chapter[1]"), _slot("c2", "chapter[2]")])

    findings = satisfiable(profile, package)

    assert [item.severity for item in findings] == ["minor"]
    assert "c2" in findings[0].message


def test_incompatible_unit_types_is_major() -> None:
    profile = _profile(
        _chapter("总览", _slot("overview", "chapter[1]", unit_types=["para", "figure"]))
    )
    package = template_package(slots=[_slot("c1", "chapter[1]", unit_types=["para"])])

    findings = satisfiable(profile, package)

    assert [item.severity for item in findings] == ["major"]
    assert "figure" in findings[0].message
    assert findings[0].evidence == "template_unit_types=['para']"


def test_template_open_unit_types_accept_any() -> None:
    """模板侧未声明类型 = 保持开放，不是"什么都不接受"。"""
    profile = _profile(
        _chapter("总览", _slot("overview", "chapter[1]", unit_types=["para", "figure"]))
    )
    package = template_package(slots=[_slot("c1", "chapter[1]", unit_types=[])])

    assert satisfiable(profile, package) == []


def test_empty_template_skeleton_is_major_and_short_circuits() -> None:
    profile = _profile(_chapter("总览", _slot("overview", "chapter[1]")))
    package = template_package(slots=[])

    findings = satisfiable(profile, package)

    assert [item.severity for item in findings] == ["major"]
    assert "未声明任何槽位" in findings[0].message


def test_alignment_uses_locator_not_slot_id() -> None:
    """槽位 id 双方各起各的（``overview`` vs ``c1``），对齐坐标只能是 ``locator``。"""
    profile = _profile(_chapter("总览", _slot("overview", "chapter[1]")))
    package = template_package(
        slots=[_slot("c1", "chapter[1]"), _slot("c1.1", "chapter[1].section[1]")]
    )

    findings = satisfiable(profile, package)

    assert [item.severity for item in findings] == ["minor"]
