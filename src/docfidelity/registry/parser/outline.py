"""入库流水线第 4 阶段：章节骨架与槽位（DD-7.1 / DD-7.2「骨架」「槽位」行）。

层级从哪来
----------
标题的层级只有两个可信来源，按优先级：

1. 段落自己的 ``w:pPr/w:outlineLvl``（作者在大纲视图里手工指定的级别）；
2. 样式的 ``w:style/w:pPr/w:outlineLvl``。

样式**名**（"标题 1" / "Heading 2"）只作为最后兜底，且必须严格锚定到
"名称 + 数字"的整串匹配：靠"名字里含不含数字"这种宽松规则去猜，会把
"2024 年度总结"判成 1 级标题，把一个普通段落变成章节。

非连续层级
----------
真实模板里常见"只有 1 级和 3 级"的骨架。构建树时用**层级栈**而不是
"必须恰好差 1 级"：遇到 3 级就挂在最近的更高级节点下，于是 1→3 自然嵌套，
不需要为每种层级组合写特例。
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping, Sequence
from typing import Final

from docfidelity.contracts.qa import Finding
from docfidelity.contracts.template import (
    BlockKind,
    ChapterNode,
    OutlineSkeleton,
    SlotSpec,
)
from docfidelity.ooxml import DocxPackage, Element
from docfidelity.ooxml.doc import attr, child_of, iter_elements, paragraph_text

#: 骨架相关 Finding 的规则名
OUTLINE_RULE_ID: Final = "REG-OUTLINE"

#: 样式名兜底规则：整串匹配「Heading 1」/「标题 1」/「标题1」。刻意不容忍前后缀，
#: 因为容忍之后「标题 1 的说明」也会被当成 1 级标题。
_HEADING_NAME_RE: Final = re.compile(r"^(?:Heading|标题)\s*(\d+)$")

_WHITESPACE_RE: Final = re.compile(r"\s+")


def heading_levels(pkg: DocxPackage) -> dict[str, int]:
    """``styleId -> 1 起的层级``。仅包含可作为标题的段落样式。

    显式 ``outlineLvl`` 优先于名称兜底；两者都没有的样式不进表，于是它不会
    被当标题——宁可漏判成"没有骨架"，也不要误判出一堆不存在的章节。
    """
    part = pkg.find_part("word/styles.xml")
    if part is None:
        return {}
    levels: dict[str, int] = {}
    for style in iter_elements(part.read_tree, "w:style"):
        style_id = attr(style, "w:styleId")
        if style_id is None or attr(style, "w:type") not in (None, "paragraph"):
            continue
        explicit = _outline_lvl(child_of(style, "w:pPr"))
        if explicit is not None:
            levels[style_id] = explicit
            continue
        fallback = _level_from_style_name(_style_name(style))
        if fallback is not None:
            levels[style_id] = fallback
    return levels


def _style_name(style: Element) -> str:
    name = child_of(style, "w:name")
    return (attr(name, "w:val") or "").strip()


def _outline_lvl(properties: Element | None) -> int | None:
    """``w:outlineLvl/@w:val`` 是 **0 起**，契约里用 1 起，故 +1。"""
    if properties is None:
        return None
    element = child_of(properties, "w:outlineLvl")
    raw = attr(element, "w:val")
    if raw is None:
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    return value + 1 if value >= 0 else None


def _level_from_style_name(name: str) -> int | None:
    match = _HEADING_NAME_RE.match(name)
    return int(match.group(1)) if match else None


def paragraph_heading_level(
    paragraph: Element, levels: Mapping[str, int]
) -> tuple[int | None, str | None]:
    """段落若是标题，返回 ``(层级, styleId)``；否则 ``(None, None)``。

    段落级的 ``outlineLvl`` 覆盖样式级——作者手工调过的级别就是他看到的级别。
    """
    properties = child_of(paragraph, "w:pPr")
    style_id = attr(child_of(properties, "w:pStyle"), "w:val")
    direct = _outline_lvl(properties)
    if direct is not None:
        return direct, style_id
    if style_id is not None and style_id in levels:
        return levels[style_id], style_id
    return None, None


def parse_outline(
    pkg: DocxPackage,
    *,
    allowed_block_types: Sequence[BlockKind] | None = None,
) -> tuple[OutlineSkeleton, list[Finding]]:
    """从正文标题段落序列构造章节骨架，并为每一章推导一个槽位。

    返回 ``(骨架, Findings)``。槽位的 ``unit_types`` **必须**由档案声明，
    骨架阶段无从得知；因此这里不猜，而是把它们整体列入待确认（NFR-26）。
    """
    levels = heading_levels(pkg)
    headings = list(_iter_headings(pkg, levels))
    if not headings:
        return (
            OutlineSkeleton(allowed_block_types=list(allowed_block_types or _all_block_kinds())),
            [
                Finding(
                    rule_id=OUTLINE_RULE_ID,
                    severity="major",
                    message="未能从模板正文识别出任何标题段落，章节骨架为空",
                    suggestion=(
                        "确认模板的标题样式带有 outlineLvl，或有形如「标题 1 / Heading 1」的样式名；"
                        "也可在 overrides 中显式指定标题样式"
                    ),
                )
            ],
        )

    tree: list[ChapterNode] = []
    stack: list[ChapterNode] = []
    for level, title, style_id in headings:
        node = ChapterNode(level=level, title=title, style_id=style_id)
        while stack and stack[-1].level >= level:
            stack.pop()
        if stack:
            stack[-1].children.append(node)
        else:
            tree.append(node)
        stack.append(node)

    slots = _derive_slots(tree)
    _attach_slots(tree, slots)
    findings: list[Finding] = []
    if slots:
        findings.append(
            Finding(
                rule_id=OUTLINE_RULE_ID,
                severity="minor",
                message=(f"已按骨架推导出 {len(slots)} 个槽位，但其允许的内容单元类型尚未声明"),
                evidence=", ".join(slot.slot_id for slot in slots[:8]),
                suggestion="在档案（DocTypeProfile）中声明各槽位的 unit_types，或确认其保持开放",
            )
        )

    return (
        OutlineSkeleton(
            chapter_tree=tree,
            allowed_block_types=list(allowed_block_types or _all_block_kinds()),
            slots=slots,
        ),
        findings,
    )


def _all_block_kinds() -> tuple[BlockKind, ...]:
    """默认全开，由档案 / ``overrides`` 收窄。

    取"全开"而不是"猜一个子集"：猜小了会让合法模板被拒，而这类拒绝信息
    对使用者来说毫无可操作性。
    """
    return ("cover", "revision", "toc", "header", "table")


def _iter_headings(
    pkg: DocxPackage, levels: Mapping[str, int]
) -> Iterator[tuple[int, str, str | None]]:
    part = pkg.find_part("word/document.xml")
    if part is None:
        return
    for paragraph in iter_elements(part.read_tree, "w:p"):
        level, style_id = paragraph_heading_level(paragraph, levels)
        if level is None:
            continue
        title = _collapse(paragraph_text(paragraph))
        if title:
            yield level, title, style_id


def _collapse(text: str) -> str:
    """合并连续空白。Word 的标题里常有制表符与多空格，直接进骨架会让槽位名参差。"""
    return _WHITESPACE_RE.sub(" ", text).strip()


def _slot_id(path: tuple[int, ...]) -> str:
    """槽位 id 的唯一构造入口。

    生成与回挂两处共用同一个函数，而不是各自拼一遍字符串——拼两遍的话，
    哪天改格式就会有节点的槽位永远回挂不上，而且**不报错**，只是槽位凭空消失。
    """
    return "c" + ".".join(str(part) for part in path)


def _derive_slots(tree: Sequence[ChapterNode]) -> list[SlotSpec]:
    """一章一个槽位。``slot_id`` 由**结构位置**决定，不由标题文本决定。

    用结构位置（``c1`` / ``c1.2``）而不是标题文本：标题会被改写，而槽位 id 是
    档案与生成期引用的键，它会出现在配置里、出现在 docx 里，改一次就是一次
    破坏性变更。
    """
    slots: list[SlotSpec] = []

    def walk(nodes: Sequence[ChapterNode], path: tuple[int, ...]) -> None:
        for ordinal, node in enumerate(nodes, start=1):
            current = (*path, ordinal)
            slots.append(
                SlotSpec(
                    slot_id=_slot_id(current),
                    required=True,
                    unit_types=[],
                    locator=_locator_of(current),
                    description=node.title,
                )
            )
            walk(node.children, current)

    walk(tree, ())
    return slots


def _locator_of(path: tuple[int, ...]) -> str:
    """``(2,) -> 'chapter[2]'``；``(2, 1) -> 'chapter[2].section[1]'``。"""
    head = f"chapter[{path[0]}]"
    return head + "".join(f".section[{part}]" for part in path[1:])


def _attach_slots(tree: Sequence[ChapterNode], slots: Sequence[SlotSpec]) -> None:
    """把槽位挂回它所属的章节节点（``OutlineSkeleton.slots`` 是扁平索引的副本）。"""
    by_id = {slot.slot_id: slot for slot in slots}

    def walk(nodes: Sequence[ChapterNode], path: tuple[int, ...]) -> None:
        for ordinal, node in enumerate(nodes, start=1):
            current = (*path, ordinal)
            slot = by_id.get(_slot_id(current))
            if slot is not None:
                node.slots.append(slot)
            walk(node.children, current)

    walk(tree, ())


__all__ = [
    "OUTLINE_RULE_ID",
    "heading_levels",
    "paragraph_heading_level",
    "parse_outline",
]
