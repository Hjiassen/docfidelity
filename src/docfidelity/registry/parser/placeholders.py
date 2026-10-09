"""入库流水线第 8 阶段：占位符清单（DD-7.1 / DD-7.2「占位」行；FR-M2-11）。

为什么要在**入库时**就登记占位符
--------------------------------
"此处填写 XXX"这类文字在生成期是一颗定时炸弹：内容注入不覆盖它，产物里就会
留下编辑痕迹。靠门禁在最后一步兜底是不够的——门禁只能报"发现了残留"，而
入库时登记才能回答"这份模板一共有几处待填"，从而让生成期逐处确认已替换或
已清除（FR-M2-11）。

扫描范围包含表格单元格与页眉页脚
--------------------------------
段落遍历天然覆盖了表格单元格（单元格里装的就是段落）。页眉页脚要单独扫：
"（此处填写项目名称）"这类提示最常出现在页眉，而它恰恰是**每一页**都会重复
出现的残留。
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Final

from docfidelity.contracts.template import BlockLocator, PlaceholderEntry, PlaceholderList
from docfidelity.ooxml import DocxPackage, Element
from docfidelity.ooxml.doc import iter_elements, paragraph_text

from .common import SCANNABLE_PART_PREFIXES, document_order_locator

#: 建议由哪条门禁规则校验"占位符是否被残留"（DD-7.2 的 ``proposed_rule_id``）
PLACEHOLDER_RULE_ID: Final = "TPL-PLACEHOLDER-RESIDUE"

#: 占位词表。中文提示词 + 少量英文缩写，覆盖常见模板写法。
#:
#: 刻意**不含**单字词（如"略"）："策略""省略""简略"都会命中，一份正常模板会
#: 被列出几百条假占位符，清单立刻失去可用性。
PLACEHOLDER_KEYWORDS: Final[tuple[str, ...]] = (
    "此处填写",
    "此处填",
    "请填写",
    "待补充",
    "待填",
    "待确认",
)

#: 连排符号也算占位：``XXX`` / ``××××`` / ``____`` 三种写法在真实模板里都常见。
#:
#: 允许符号之间夹空白（``× × × ×``），中文模板里这种"全角符号加全角空格"的写法
#: 很普遍。但**不允许跨族混排**：混排的串（``x×x×x``）几乎总是正常内容而不是占位。
_FILLER_RUN_RE: Final = re.compile(
    r"(?:(?:[Xx\uff38]\s*){3,}|(?:[\u00d7\uff58]\s*){3,}|_{3,}|-{4,})"
)

#: 尖括号占位，如 ``<项目名称>`` / ``《待定》``
_ANGLE_RE: Final = re.compile(r"[<\uff1c][^<>\uff1c\uff1e]{1,30}[>\uff1e]")

#: 英文缩写必须整词匹配，否则 "TODO_LIST" 之类的标识符会误报
_WORD_RE: Final = re.compile(r"\b(?:TBD|TODO|FIXME|XXX)\b")

#: 单条占位符文本的截断长度，避免把整段说明都塞进清单
MAX_TEXT_LENGTH: Final = 120


def parse_placeholders(pkg: DocxPackage) -> PlaceholderList:
    """扫描正文、表格与页眉页脚，登记全部占位符。"""
    entries: list[PlaceholderEntry] = []
    for part_name in _scannable_parts(pkg):
        part = pkg.find_part(part_name)
        if part is None:
            continue
        entries.extend(_scan_part(part_name, part.read_tree))
    return PlaceholderList(entries=entries)


def _scannable_parts(pkg: DocxPackage) -> list[str]:
    """正文 + 页眉页脚 + 脚注尾注，按稳定顺序返回。

    ``SCANNABLE_PART_PREFIXES`` 已经覆盖这几类，**不要**再单独并一遍页眉页脚：
    并了就会重复扫描，同一处占位符在清单里出现两次，而"待填 2 处"和"待填 1 处"
    会让使用者去翻一个根本不存在的地方。
    """
    return [
        name
        for name in sorted(pkg.part_names("all"))
        if name.endswith(".xml")
        and any(name.startswith(prefix) for prefix in SCANNABLE_PART_PREFIXES)
    ]


def _scan_part(part_name: str, root: Element) -> list[PlaceholderEntry]:
    entries: list[PlaceholderEntry] = []
    for ordinal, paragraph in enumerate(iter_elements(root, "w:p"), start=1):
        text = _collapse(paragraph_text(paragraph))
        matched = matched_placeholder(text)
        if matched is None:
            continue
        entries.append(
            PlaceholderEntry(
                text=text[:MAX_TEXT_LENGTH],
                locator=_locator(part_name, ordinal),
                proposed_rule_id=PLACEHOLDER_RULE_ID,
            )
        )
    return entries


def matched_placeholder(text: str) -> str | None:
    """返回命中的占位标记；未命中返回 ``None``。

    返回**命中片段**而不是布尔值：调用方要把它写进 evidence 让人一眼看出
    是哪种占位（"此处填写"和"待确认"的处置方式完全不同）。
    """
    if not text:
        return None
    for keyword in PLACEHOLDER_KEYWORDS:
        if keyword in text:
            return keyword
    for pattern in (_FILLER_RUN_RE, _ANGLE_RE, _WORD_RE):
        match = pattern.search(text)
        if match is not None:
            return match.group(0)
    return None


def _locator(part_name: str, ordinal: int) -> BlockLocator:
    """占位段的定位符：``(//w:p)[n]``，``n`` = :func:`iter_elements` 的枚举序号。

    序号与扫描天然同构——扫描用的就是同一个 ``iter_elements``。二者一旦不同源，
    登记的序号与定位到的段落就会错位，而错位的表现恰恰是"清单上有一处待填，
    产物里那一处却还在"。
    """
    return BlockLocator(part=part_name, xpath=document_order_locator("p", ordinal))


def _collapse(text: str) -> str:
    return " ".join(text.split())


def count_by_part(entries: Iterable[PlaceholderEntry]) -> dict[str, int]:
    """按部件统计占位符数量，供界面展示"待填 N 处"。"""
    counts: dict[str, int] = {}
    for entry in entries:
        counts[entry.locator.part] = counts.get(entry.locator.part, 0) + 1
    return counts


__all__ = [
    "MAX_TEXT_LENGTH",
    "PLACEHOLDER_KEYWORDS",
    "PLACEHOLDER_RULE_ID",
    "count_by_part",
    "matched_placeholder",
    "parse_placeholders",
]
