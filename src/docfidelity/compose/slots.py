"""槽位校验（DD-8.3 / FR-M3-15）。

四类问题，全部返回 ``Finding`` 而不抛错——它们都是"文档仍能生成，但有洞"的情形
（NFR-26：可复核的问题进裁决队列，不阻断构建）：

===================  ==================================================================
必填槽位为空          ``required=True`` 却没有任何单元落位；严重度可按 ``SlotPolicy``
                     从 major 升级为 blocker（DD-8.3 明确要求"可配置升级"）
槽位单元类型越界      落位单元的类型不在槽位声明的 ``unit_types`` 内
槽位 id 重复          同一 ``slot_id`` 出现在多个章节上，落位归属不再唯一
落位槽未被引用        投影产出了内容，但没有任何章节声明接收它的槽位
===================  ==================================================================

最后一行是最要紧的一条：DD-8.1 的 ``units_by_slot.setdefault(proj.slot or "default", [])``
在投影漏填 ``slot`` 时**不会报错**，那些单元只是安静地消失在 ``units_by_slot`` 里。
"少了一整章而报告全绿"正是本项目反复踩的那类坑（1.2 各条），所以这里必须喊出来。

``ProjectionSpec.unit`` → ``ContentUnit.type`` 是多对一（``row`` 与 ``table`` 都落到
``table``，见 ``model.projection``），因此 ``unit_types`` 声明的是**内容单元类型**而非投影单元。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from docfidelity.contracts.ir import ContentUnit
from docfidelity.contracts.profile import ChapterSkeletonItem
from docfidelity.contracts.qa import Finding, Location, RuleSeverity
from docfidelity.contracts.template import SlotSpec

__all__ = [
    "DEFAULT_SLOT",
    "SLOT_RULE_ID",
    "SlotPolicy",
    "check_slots",
    "orphan_slots",
    "referenced_slot_ids",
]

#: 槽位校验的规则编号（FR-M3-15：体例声明与校验）。
SLOT_RULE_ID: Final = "FR-M3-15"

#: 投影未声明 ``slot`` 时落入的保留槽位（DD-8.1）。
DEFAULT_SLOT: Final = "default"


@dataclass(frozen=True)
class SlotPolicy:
    """槽位校验策略。

    ``escalate_required_to_blocker`` 对应 DD-8.3 的"``required`` 且为空 → 可配置升级为
    blocker"。默认 ``False``：把"该章节没内容"默认判成阻断，会让事实源尚不完整时的
    每一次构建都失败——这与 NFR-26 的取向相反。
    """

    escalate_required_to_blocker: bool = False


def _severity(policy: SlotPolicy) -> RuleSeverity:
    return "blocker" if policy.escalate_required_to_blocker else "major"


def check_slots(
    chapters: Sequence[ChapterSkeletonItem],
    placed: Mapping[str, Sequence[ContentUnit]],
    *,
    policy: SlotPolicy | None = None,
) -> list[Finding]:
    """校验骨架槽位与落位内容。返回顺序与骨架声明顺序一致（报告可 diff）。"""
    active = policy or SlotPolicy()
    findings: list[Finding] = list(_duplicate_slot_findings(chapters))
    for chapter in chapters:
        for slot in chapter.slots:
            units = list(placed.get(slot.slot_id, ()))
            if not units:
                if slot.required:
                    findings.append(_empty_finding(chapter, slot, active))
                continue
            findings.extend(_type_findings(chapter, slot, units))
    return findings


def _duplicate_slot_findings(chapters: Sequence[ChapterSkeletonItem]) -> list[Finding]:
    seen: dict[str, int] = {}
    for chapter in chapters:
        for slot in chapter.slots:
            seen[slot.slot_id] = seen.get(slot.slot_id, 0) + 1
    findings: list[Finding] = []
    for chapter in chapters:
        for slot in chapter.slots:
            if seen[slot.slot_id] > 1:
                findings.append(
                    Finding(
                        rule_id=SLOT_RULE_ID,
                        severity="major",
                        message=(
                            f"槽位 {slot.slot_id!r} 在章节骨架中被声明了 {seen[slot.slot_id]} 次，"
                            "落位归属不再唯一"
                        ),
                        location=Location(chapter_path=[chapter.title]),
                        evidence=f"slot_id={slot.slot_id}",
                        suggestion="给每个章节的槽位取一个唯一的 slot_id",
                    )
                )
                # 同一个 slot_id 只报一次，避免重复声明的章节数越多、报告越长
                seen[slot.slot_id] = 1
    return findings


def _empty_finding(chapter: ChapterSkeletonItem, slot: SlotSpec, policy: SlotPolicy) -> Finding:
    return Finding(
        rule_id=SLOT_RULE_ID,
        severity=_severity(policy),
        message=f"章节「{chapter.title}」的必填槽位 {slot.slot_id!r} 未填充",
        location=Location(chapter_path=[chapter.title]),
        evidence=f"locator={slot.locator}",
        suggestion=(
            "检查事实源是否有对应内容，或核对投影规则的 slot 声明"
            + (f"（该槽位说明：{slot.description}）" if slot.description else "")
        ),
    )


def _type_findings(
    chapter: ChapterSkeletonItem, slot: SlotSpec, units: list[ContentUnit]
) -> list[Finding]:
    if not slot.unit_types:
        return []
    outside = sorted({unit.type for unit in units if unit.type not in slot.unit_types})
    if not outside:
        return []
    return [
        Finding(
            rule_id=SLOT_RULE_ID,
            severity="major",
            message=(
                f"章节「{chapter.title}」的槽位 {slot.slot_id!r} 落入了"
                f"未声明的内容单元类型：{outside}"
            ),
            location=Location(unit_id=units[0].unit_id, chapter_path=[chapter.title]),
            evidence=f"unit_types={slot.unit_types}",
            suggestion="把该单元类型加入槽位的 unit_types，或调整投影的 unit",
        )
    ]


# -- 落位槽与骨架的对账 ---------------------------------------------------


def referenced_slot_ids(chapters: Sequence[ChapterSkeletonItem]) -> list[str]:
    """骨架声明接收的槽位 id，按声明顺序去重。"""
    seen: dict[str, None] = {}
    for chapter in chapters:
        for slot in chapter.slots:
            seen.setdefault(slot.slot_id, None)
    return list(seen)


def orphan_slots(
    placed: Mapping[str, Sequence[ContentUnit]],
    chapters: Sequence[ChapterSkeletonItem],
) -> list[str]:
    """产出了内容、却没有任何章节接收的槽位 id（字典序）。"""
    referenced = set(referenced_slot_ids(chapters))
    return sorted(key for key, units in placed.items() if units and key not in referenced)
