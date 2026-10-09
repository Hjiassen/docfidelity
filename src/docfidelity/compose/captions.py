"""图注与表注编号（DD-8.3 / FR-M3-08）。

规格只有三行，但三行都容易做错：

* 编号格式 ``图 X-Y 名称`` / ``表 X-Y 名称``；
* ``X`` = **一级章节号**，``Y`` = **章内序号**（故进入新的一级章节时 ``Y`` 归零）；
* **连续不跳号** —— 序号在"参与编号的单元"上连续，而不是随手一数。

三个刻意的取舍
--------------
1. **只有声明了 ``caption_slot`` 的单元参与编号**。契约里 ``caption_slot=None`` 的
   语义就是"不编号"（``TableData`` / ``FigureData``）。若对所有带 caption 的单元都
   编号，"这张表本来就不该有编号"就无法表达。
2. **``assign`` 幂等**：已经形如 ``图 3-1 …`` 的标题只计入序号、不改写。装配链路里
   ``assign`` 只该被调一次，但"被调两次就产出两层编号"是那种没人会想到去测的缺陷。
3. **一级章节尚未出现时编号为 ``X=0``**（``图 0-1``）。这不是合法成果，但它是
   **可见的**：把 X 悄悄从 1 起算，会让"内容落在了所有章节之外"这件事彻底隐形。
"""

from __future__ import annotations

import re
from typing import Final

from docfidelity.contracts.ir import ContentIR, ContentUnit

__all__ = [
    "CAPTION_RULE_ID",
    "FIGURE_PREFIX",
    "NUMBERED_PATTERN",
    "TABLE_PREFIX",
    "assign",
    "parse_numbered_caption",
]

#: 图注编号能力的规则编号（FR-M3-08）。
CAPTION_RULE_ID: Final = "FR-M3-08"

FIGURE_PREFIX: Final = "图"
TABLE_PREFIX: Final = "表"

#: 已编号标题的形状：``图 3-1 名称`` / ``表 5-12``。
NUMBERED_PATTERN: Final = re.compile(
    r"^(?P<prefix>图|表)\s*(?P<chapter>\d+)-(?P<index>\d+)(?:\s+(?P<name>.*))?$"
)

#: ``ContentUnit.type`` → 编号前缀。未登记的取值**不编号**（``heading`` / ``para`` 等）。
_PREFIX_BY_TYPE: Final[dict[str, str]] = {
    "table": TABLE_PREFIX,
    "figure": FIGURE_PREFIX,
    "screenshot": FIGURE_PREFIX,
}

#: 章节号计入的标题层级。契约里 ``level`` 从 1 起，故 1 即一级章节。
_CHAPTER_LEVEL: Final = 1

_CAPTION_FIELD: Final = "caption"
_SLOT_FIELD: Final = "caption_slot"


def assign(ir: ContentIR) -> None:
    """就地编号整棵 IR。幂等（见模块文档第 2 条取舍）。"""
    chapter = 0
    per_prefix: dict[str, int] = {}
    for unit in ir.walk():
        if unit.type == "heading" and unit.level == _CHAPTER_LEVEL:
            chapter += 1
            per_prefix.clear()
            continue
        prefix = _PREFIX_BY_TYPE.get(unit.type)
        if prefix is None or not _is_numbered(unit):
            continue
        per_prefix[prefix] = per_prefix.get(prefix, 0) + 1
        if _already_numbered(unit):
            continue
        _write_caption(unit, f"{prefix} {chapter}-{per_prefix[prefix]}")


# -- 单元读写 -------------------------------------------------------------


def _is_numbered(unit: ContentUnit) -> bool:
    """该单元是否参与编号：``caption_slot`` 非 ``None``。"""
    return unit.fields.get(_SLOT_FIELD) is not None


def _already_numbered(unit: ContentUnit) -> bool:
    caption = unit.fields.get(_CAPTION_FIELD)
    return isinstance(caption, str) and parse_numbered_caption(caption) is not None


def _write_caption(unit: ContentUnit, number: str) -> None:
    name = unit.fields.get(_CAPTION_FIELD)
    trimmed = name.strip() if isinstance(name, str) else ""
    unit.fields[_CAPTION_FIELD] = f"{number} {trimmed}" if trimmed else number


def parse_numbered_caption(text: str) -> tuple[str, int, int] | None:
    """解析 ``图 3-1 名称`` → ``("图", 3, 1)``；不是编号标题返回 ``None``。

    渲染层据此判断"这个标题是否已带编号"，避免二次编号——判断口径只有这一处。
    """
    match = NUMBERED_PATTERN.match(text.strip())
    if match is None:
        return None
    return match.group("prefix"), int(match.group("chapter")), int(match.group("index"))
