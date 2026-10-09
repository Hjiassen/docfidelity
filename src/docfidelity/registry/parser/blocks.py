"""入库流水线第 5 阶段：可复用块索引（DD-7.1 / DD-7.2「版块」行）。

为什么块要"按原样复用"
----------------------
封面、修订记录表、目录这些版块的价值恰恰在于**它们不是内容**——列宽、合并单元格、
底纹、甚至手工排的换行都是模板资产。生成期重新构造一个"看起来一样"的表格，
几乎必然在某些细节上与原版不同，而这些细节又没人会逐格去核对。

因此本阶段只做一件事：把块**定位**清楚并登记，生成期按 locator 深拷贝原节点。

定位的稳定性
------------
locator 用**文档序位置型 XPath**（``(//w:tbl)[3]``），不用书签或关系 ID。后者是易变量
（Word 插一页页眉就整片重排），用它做 locator 会让同一份模板两次入库得到不同
的块引用，进而把"块消失了"这种硬阻断项误报出来。

位置型 locator 必须带括号（``(//w:tbl)[3]``）——``//w:tbl[3]`` 的含义是"每个父节点下的
第 3 张表"，与"整篇第 3 张表"不是一回事。唯一的产生器在
:func:`docfidelity.registry.parser.common.document_order_locator`。
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from typing import Any, Final

from docfidelity.contracts.qa import Finding
from docfidelity.contracts.template import BlockEntry, BlockIndex, BlockLocator
from docfidelity.ooxml import DocxPackage, Element, resolve_target_path
from docfidelity.ooxml.doc import iter_elements, localname, qn, text_of

from .common import (
    document_order_locator,
    element_text,
    header_footer_parts,
    localname_of_part,
    paragraph_texts,
)

BLOCK_RULE_ID: Final = "REG-BLOCKS"

#: 正文部件的路径。封面 / 修订记录 / 目录的定位符都落在这里。
DOCUMENT_PART: Final = "word/document.xml"

#: 修订记录的默认识别词。命中"表头行里出现两个以上"即判定。
#: 与 DD-7.3 的 ``block_locator.revision.header_keywords`` 同义，可被 overrides 覆盖。
DEFAULT_REVISION_KEYWORDS: Final[tuple[str, ...]] = (
    "序号",
    "版本",
    "修改",
    "修订",
    "日期",
    "变更",
)

#: 判定为修订记录所需的表头命中数下限。
#: 取 2 而不是 1：正文里出现一次"日期"的普通表格非常多，阈值太低会满屏误报。
REVISION_KEYWORD_THRESHOLD: Final = 2

#: 表头关键词匹配的最大列数。只看首行前若干格，避免把整张表的长文本也算进来。
REVISION_HEADER_CELLS: Final = 8

_TOC_FIELD_RE: Final = re.compile(r"\bTOC\b")


def parse_blocks(pkg: DocxPackage) -> tuple[BlockIndex, list[Finding]]:
    """定位封面 / 修订记录 / 目录 / 页眉页脚四类块。"""
    findings: list[Finding] = []
    entries: list[BlockEntry] = []

    entries.extend(_cover_blocks(pkg))
    revision, revision_findings = _revision_blocks(pkg)
    entries.extend(revision)
    findings.extend(revision_findings)
    entries.extend(_toc_blocks(pkg))
    entries.extend(_header_footer_blocks(pkg))

    return BlockIndex(blocks=entries), findings


def _cover_blocks(pkg: DocxPackage) -> list[BlockEntry]:
    """封面 = 文档首个分节符之前的连续段落。

    单节文档没有"首个分节符之前的段落"这一说法（整篇都在同一节里），此时按
    前若干段是否含标题样式来判定没有意义，直接不产出封面块——**漏一个块**
    只会让生成期跳过封面处理，**错一个块**却会让正文被当成封面裁掉。

    定位符指向封面的**末段**
    ------------------------
    封面是"从正文开头起的一段**连续段落**"（``registry`` 的词），这个区间只有**终点**
    边界可定位：终点 = 那个带 ``pPr/sectPr`` 的段落（分节符就写在它身上）。起点没有
    可写的判据——"正文第一个段落"在有了内容控件（目录 ``w:sdt``）之后不再是正文开头。
    故 locator 给末段，消费侧由它向前游走连续的段落同胞得到整个封面区间
    （见 ``render.blocks._paragraph_run_ending_at``）。这一条约定是两层的接缝，
    改任一侧都要同时改另一侧。
    """
    part = pkg.find_part(DOCUMENT_PART)
    if part is None:
        return []
    root = part.read_tree
    sections = list(iter_elements(root, "w:sectPr"))
    body = root.find(qn("w:body"))
    if body is None or not sections:
        return []
    last_section = sections[-1]
    if last_section.getparent() is not body:
        return []

    paragraphs = list(iter_elements(root, "w:p"))
    cut = _first_section_break_index(paragraphs)
    if cut <= 0:
        return []
    first = _cover_first_index(paragraphs, cut, body)
    if first <= 0:
        return []
    return [
        BlockEntry(
            block_id="cover-1",
            kind="cover",
            locator=BlockLocator(part=DOCUMENT_PART, xpath=document_order_locator("p", cut)),
            structure={"paragraph_range": [first, cut]},
        )
    ]


def _first_section_break_index(paragraphs: Sequence[Element]) -> int:
    """首个带 ``pPr/sectPr`` 的段落在全篇段落里的 1 起序号；没有则 0。"""
    for ordinal, paragraph in enumerate(paragraphs, start=1):
        properties = paragraph.find(qn("w:pPr"))
        if properties is not None and properties.find(qn("w:sectPr")) is not None:
            return ordinal
    return 0


def _cover_first_index(paragraphs: Sequence[Element], cut: int, body: Element) -> int:
    """封面首段在全篇段落里的 1 起序号。

    ``paragraphs`` 是**全篇**文档序（含内容控件与表格单元格里的段落），而封面是
    "正文**直属**的一段连续段落"。故从末段向前游走，遇到第一个非正文直属的段落即停：
    走到的那一段就是封面首段。

    不这样做的后果有一个很具体的形态：内容控件（目录 ``w:sdt``）插在封面之前时，
    它内部的两个目录条目段落会被算进"封面"，于是封面区间从全篇第 1 段起算——
    而那个序号指向的是**目录条目**。区间错一格，下游"受保护区"就整个挪到别处。
    """
    first = cut
    for ordinal in range(cut, 0, -1):
        if paragraphs[ordinal - 1].getparent() is not body:
            break
        first = ordinal
    return first


def _revision_blocks(pkg: DocxPackage) -> tuple[list[BlockEntry], list[Finding]]:
    part = pkg.find_part(DOCUMENT_PART)
    if part is None:
        return [], []
    entries: list[BlockEntry] = []
    for ordinal, table in enumerate(iter_elements(part.read_tree, "w:tbl"), start=1):
        header_cells = _first_row_cells(table)
        hits = [word for word in DEFAULT_REVISION_KEYWORDS if _contains_any(header_cells, word)]
        if len(hits) < REVISION_KEYWORD_THRESHOLD:
            continue
        entries.append(
            BlockEntry(
                block_id=f"revision-{len(entries) + 1}",
                kind="revision",
                locator=BlockLocator(
                    part=DOCUMENT_PART, xpath=document_order_locator("tbl", ordinal)
                ),
                structure={
                    "position": ordinal,
                    "row_count": len(list(iter_elements(table, "w:tr"))),
                    "header_keywords": hits,
                },
            )
        )
    findings: list[Finding] = []
    if not entries:
        findings.append(
            Finding(
                rule_id=BLOCK_RULE_ID,
                severity="minor",
                message="未识别出修订记录块",
                suggestion=(
                    "若模板确实含修订记录表，请在 overrides 的 block_locator.revision 中"
                    "显式给出 table_index 与 header_keywords"
                ),
            )
        )
    return entries, findings


def _first_row_cells(table: Element) -> list[str]:
    rows = list(iter_elements(table, "w:tr"))
    if not rows:
        return []
    cells = list(iter_elements(rows[0], "w:tc"))
    return [element_text(cell).strip() for cell in cells[:REVISION_HEADER_CELLS]]


def _contains_any(values: Iterable[str], needle: str) -> bool:
    return any(needle in value for value in values)


def _toc_blocks(pkg: DocxPackage) -> list[BlockEntry]:
    """目录 = 内容控件（``w:sdt``）里带 ``TOC`` 域的那个。"""
    part = pkg.find_part(DOCUMENT_PART)
    if part is None:
        return []
    entries: list[BlockEntry] = []
    for ordinal, control in enumerate(iter_elements(part.read_tree, "w:sdt"), start=1):
        if not _has_toc_field(control):
            continue
        entries.append(
            BlockEntry(
                block_id=f"toc-{len(entries) + 1}",
                kind="toc",
                locator=BlockLocator(
                    part=DOCUMENT_PART, xpath=document_order_locator("sdt", ordinal)
                ),
                structure={"paragraph_count": len(list(iter_elements(control, "w:p")))},
            )
        )
    return entries


def _has_toc_field(control: Element) -> bool:
    for instruction in iter_elements(control, "w:instrText"):
        if _TOC_FIELD_RE.search(text_of(instruction)):
            return True
    return False


def _header_footer_blocks(pkg: DocxPackage) -> list[BlockEntry]:
    """页眉页脚按角色登记。角色从 document.xml 的引用里反查。"""
    roles = _role_by_target(pkg)
    entries: list[BlockEntry] = []
    for name in header_footer_parts(pkg):
        role = roles.get(name)
        family = localname_of_part(name)
        entries.append(
            BlockEntry(
                block_id=f"{family}-{role}" if role else f"{family}-{len(entries) + 1}",
                kind="header",
                locator=BlockLocator(part=name, xpath=document_order_locator("p", 1)),
                structure={
                    "family": family,
                    "role": role,
                    "paragraph_count": len(paragraph_texts(pkg, name)),
                },
            )
        )
    return entries


def _role_by_target(pkg: DocxPackage) -> dict[str, str]:
    """``{页眉/页脚部件路径: 角色}``。同一部件可被多节以不同角色引用，取首个。"""
    part = pkg.find_part(DOCUMENT_PART)
    if part is None:
        return {}
    owner = DOCUMENT_PART
    relationships = {item.rel_id: item for item in pkg.relationships(owner)}
    roles: dict[str, str] = {}
    for element in part.read_tree.iter():
        tag = localname(element)
        if tag not in {"headerReference", "footerReference"}:
            continue
        role = element.get(qn("w:type"))
        rel_id = element.get(qn("r:id"))
        if role is None or rel_id is None or rel_id not in relationships:
            continue
        target = resolve_target_path(owner, relationships[rel_id])
        roles.setdefault(target, role)
    return roles


def describe_block(entry: BlockEntry) -> dict[str, Any]:
    """块的可读摘要，供界面列表与日志使用。"""
    return {"block_id": entry.block_id, "kind": entry.kind, **entry.structure}


__all__ = [
    "BLOCK_RULE_ID",
    "DEFAULT_REVISION_KEYWORDS",
    "REVISION_KEYWORD_THRESHOLD",
    "describe_block",
    "parse_blocks",
]
