"""档案 × 模板包 可满足性预检（DD-7.5 第 4 条 / DD-8.1 前置防线）。

这是"档案与模板包错配"的机械防线：构建之前先把"档案要的章节能不能在这个模板里
落位"对一遍，而不是等渲染到一半才发现有一章无处安放。

对齐口径：**以 ``SlotSpec.locator`` 为准，不以 ``slot_id`` 为准**
----------------------------------------------------------------
模板包里的槽位由 ``registry.parser.outline`` 按**结构位置**推导（``c1`` / ``c1.2``，
``locator`` 形如 ``chapter[1]`` / ``chapter[1].section[2]``）；档案里的槽位 id 则是
档案作者自己起的名字（``overview`` / ``apis``）。二者**本就不同源**，用 ``slot_id``
对齐会把每一份档案都判成不适配。``locator`` 是双方共享的结构坐标，也是契约里唯一
声明了"语义定位"的字段。

槽位来源只有一处：``TemplatePackage.outline.slots``（扁平索引）。刻意不去遍历
``chapter_tree`` 补一遍——两套口径迟早漂移，而漂移的表现是"预检说可满足、渲染期
查不到槽位"。``ContractQuery.slot()`` 走的也是这个扁平索引。

已知缺口（不臆造）
------------------
DD-7.5(4) 还要求对齐"块类型 / 版块引用"，但 ``DocTypeProfile`` 目前**没有任何字段**
声明它需要哪些可复用块（``Bindings`` 只有模板包与规则集）。缺了声明就无法机械判定，
凭空推断只会产出假告警。补齐须给档案增加块需求字段（契约变更，走 L3）。
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Final, NamedTuple

from docfidelity.contracts.profile import ChapterSkeletonItem, DocTypeProfile
from docfidelity.contracts.qa import Finding, Location
from docfidelity.contracts.template import SlotSpec, TemplatePackage

__all__ = ["PRECHECK_RULE_ID", "satisfiable"]

#: 档案双向校验的规则编号（FR-M2-23）。DD-7.5 第 4 条同属本条。
PRECHECK_RULE_ID: Final = "FR-M2-23"


class _DeclaredSlot(NamedTuple):
    chapter: ChapterSkeletonItem
    slot: SlotSpec


def satisfiable(profile: DocTypeProfile, package: TemplatePackage) -> list[Finding]:
    """档案声明的槽位能否被模板包骨架满足。

    Returns
    ------
    list[Finding]
        为空表示可满足；任何 ``major`` 都应让调用方在构建前列出并阻断
        （服务端对应 ``E_PRECHECK_FAILED``）。
    """
    declared = list(_declared_slots(profile))
    provided = {slot.locator: slot for slot in package.outline.slots}

    if not provided:
        return [
            Finding(
                rule_id=PRECHECK_RULE_ID,
                severity="major",
                message="模板骨架未声明任何槽位，档案声明的章节内容无处落位",
                evidence=f"template={package.manifest.ref}",
                suggestion=(
                    "确认模板的标题样式可被识别（带 outlineLvl 或名为「标题 N」），"
                    "或在模板入库时用 overrides 显式指定标题样式"
                ),
            )
        ]

    findings: list[Finding] = []
    for item in declared:
        template_slot = provided.get(item.slot.locator)
        if template_slot is None:
            findings.append(_missing_finding(profile, package, item))
            continue
        findings.extend(_type_findings(profile, item, template_slot))

    used = {item.slot.locator for item in declared}
    for locator, slot in provided.items():
        if locator in used:
            continue
        findings.append(
            Finding(
                rule_id=PRECHECK_RULE_ID,
                severity="minor",
                message=(f"模板骨架的槽位 {slot.slot_id!r}（{locator}）没有任何档案槽位与之对齐"),
                evidence=f"template={package.manifest.ref}",
                suggestion="在档案的章节骨架里为该位置声明一个槽位，或确认模板的这一章确实不需要填内容",
            )
        )
    return findings


def _missing_finding(
    profile: DocTypeProfile, package: TemplatePackage, item: _DeclaredSlot
) -> Finding:
    return Finding(
        rule_id=PRECHECK_RULE_ID,
        severity="major",
        message=(
            f"档案 {profile.profile_id} 的槽位 {item.slot.slot_id!r}"
            f"（{item.slot.locator}）在模板骨架中不存在"
        ),
        location=Location(chapter_path=[item.chapter.title]),
        evidence=f"archive_locator={item.slot.locator}",
        suggestion=(
            f"确认模板 {package.manifest.ref} 的骨架与该档案匹配，"
            "或修正档案槽位的 locator（chapter[i] / chapter[i].section[j]）"
        ),
    )


def _type_findings(
    profile: DocTypeProfile, item: _DeclaredSlot, template_slot: SlotSpec
) -> list[Finding]:
    """槽位允许的单元类型须被模板骨架接受。

    模板侧 ``unit_types`` 为空表示**保持开放**（``parse_outline`` 无从得知类型，
    刻意不猜），此时不判定——把"模板没声明"当成"不接受"会让所有模板都被判不适配。
    """
    if not item.slot.unit_types or not template_slot.unit_types:
        return []
    outside = [unit for unit in item.slot.unit_types if unit not in template_slot.unit_types]
    if not outside:
        return []
    return [
        Finding(
            rule_id=PRECHECK_RULE_ID,
            severity="major",
            message=(
                f"档案 {profile.profile_id} 的槽位 {item.slot.slot_id!r} 声明了模板骨架"
                f"不接受的内容单元类型：{outside}"
            ),
            location=Location(chapter_path=[item.chapter.title]),
            evidence=f"template_unit_types={template_slot.unit_types}",
            suggestion="收窄档案槽位的 unit_types，或放宽模板骨架对该槽位的类型约束",
        )
    ]


def _declared_slots(profile: DocTypeProfile) -> Iterator[_DeclaredSlot]:
    for chapter in profile.genre.chapter_skeleton:
        for slot in chapter.slots:
            yield _DeclaredSlot(chapter, slot)
