"""OOXML 元素层的书写约定：局部名 / 子元素规范序 / 开关取值。

抽取出来的理由很直接：这几张表在渲染层被**比较**用到（``is_paragraph``、
``ensure_child_in_order`` 的定位表），而它们与"渲染状态"没有任何关系。
放在 :mod:`context` 里会让那一个文件同时承担"状态载体"与"XML 书写约定"两件事，
越过单文件行数上限之后只会被草率地砍断。

子元素顺序是**规范约束**，不是风格偏好
--------------------------------------
ECMA-376 对 ``w:pPr`` / ``w:tblPr`` / ``w:trPr`` / ``w:tcPr`` / ``w:rPr`` 的子元素
顺序都有明确规定。顺序写错的产物 Word **可能**能打开（它对顺序相当宽容），
但 LibreOffice 与严格校验器会拒收，而且报错信息完全指不到"顺序"上——症状通常
表现为"跨页表头不重复""单元格间距失效"这类看似无关的问题。故新增子元素一律
经 ``ooxml.ensure_child_in_order(parent, tag, ORDER)`` 定位插入。
"""

from __future__ import annotations

from typing import Final

from docfidelity.ooxml import Element, localname

__all__ = [
    "BULLET_FORMAT",
    "CELL_LOCAL",
    "FALSE_ATTR",
    "PARAGRAPH_LOCAL",
    "P_PR_ORDER",
    "ROW_LOCAL",
    "R_PR_ORDER",
    "TABLE_LOCAL",
    "TBL_ORDER",
    "TBL_PR_ORDER",
    "TC_MAR_ORDER",
    "TC_PR_ORDER",
    "TRUE_ATTR",
    "TR_ORDER",
    "TR_PR_ORDER",
    "is_paragraph",
    "is_table",
]

#: 段落 / 表格 / 单元格 / 行的局部名。写成常量而不是现场字面量：
#: 它们在同一批文件里被比较多次，一个拼写错误就是一次静默的"永远不匹配"。
PARAGRAPH_LOCAL: Final = "p"
TABLE_LOCAL: Final = "tbl"
CELL_LOCAL: Final = "tc"
ROW_LOCAL: Final = "tr"

#: 编号格式里表示"项目符号"的取值（``w:numFmt/@w:val``）。
BULLET_FORMAT: Final = "bullet"

#: "只表示开 / 关"的属性在 ``w:tblLook`` 这类元素上的取值。
TRUE_ATTR: Final = "1"
FALSE_ATTR: Final = "0"

#: ``w:tblPr`` 子元素的规范序（ECMA-376 Part 1 §17.4.60）。
TBL_PR_ORDER: Final[tuple[str, ...]] = (
    "tblStyle",
    "tblpPr",
    "tblOverlap",
    "bidiVisual",
    "tblStyleRowBandSize",
    "tblStyleColBandSize",
    "tblW",
    "jc",
    "tblCellSpacing",
    "tblInd",
    "tblBorders",
    "shd",
    "tblLayout",
    "tblCellMar",
    "tblLook",
    "tblCaption",
    "tblDescription",
)

#: ``w:trPr`` 子元素的规范序（§17.4.82）。
TR_PR_ORDER: Final[tuple[str, ...]] = (
    "cnfStyle",
    "divId",
    "gridBefore",
    "gridAfter",
    "wBefore",
    "wAfter",
    "cantSplit",
    "trHeight",
    "tblHeader",
    "tblCellSpacing",
    "jc",
    "hidden",
)

#: ``w:tcPr`` 子元素的规范序（§17.4.70）。
TC_PR_ORDER: Final[tuple[str, ...]] = (
    "cnfStyle",
    "tcW",
    "gridSpan",
    "hMerge",
    "vMerge",
    "tcBorders",
    "shd",
    "noWrap",
    "tcMar",
    "textDirection",
    "tcFitText",
    "vAlign",
    "hideMark",
)

#: ``w:tcMar`` 子元素的规范序。
TC_MAR_ORDER: Final[tuple[str, ...]] = ("top", "start", "left", "bottom", "end", "right")

#: ``w:pPr`` 子元素的规范序（§17.3.1.26）。
P_PR_ORDER: Final[tuple[str, ...]] = (
    "pStyle",
    "keepNext",
    "keepLines",
    "pageBreakBefore",
    "framePr",
    "widowControl",
    "numPr",
    "suppressLineNumbers",
    "pBdr",
    "shd",
    "tabs",
    "suppressAutoHyphens",
    "kinsoku",
    "wordWrap",
    "overflowPunct",
    "topLinePunct",
    "autoSpaceDE",
    "autoSpaceDN",
    "bidi",
    "adjustRightInd",
    "snapToGrid",
    "spacing",
    "ind",
    "contextualSpacing",
    "mirrorIndents",
    "suppressOverlap",
    "jc",
    "textDirection",
    "textAlignment",
    "textboxTightWrap",
    "outlineLvl",
    "divId",
    "cnfStyle",
    "rPr",
    "sectPr",
    "pPrChange",
)

#: ``w:rPr`` 子元素的规范序（§17.3.2.28）。
R_PR_ORDER: Final[tuple[str, ...]] = (
    "rStyle",
    "rFonts",
    "b",
    "bCs",
    "i",
    "iCs",
    "caps",
    "smallCaps",
    "strike",
    "dstrike",
    "outline",
    "shadow",
    "emboss",
    "imprint",
    "noProof",
    "snapToGrid",
    "vanish",
    "webHidden",
    "color",
    "spacing",
    "w",
    "kern",
    "position",
    "sz",
    "szCs",
    "highlight",
    "u",
    "effect",
    "bdr",
    "shd",
    "fitText",
    "vertAlign",
    "rtl",
    "cs",
    "em",
    "lang",
    "eastAsianLayout",
    "specVanish",
    "oMath",
)

#: ``w:tbl`` 子元素的规范序（§17.4.38）。
TBL_ORDER: Final[tuple[str, ...]] = ("tblPr", "tblGrid", "tr")

#: ``w:tr`` 子元素的规范序（§17.4.81）。``w:trPr`` **必须**排在所有 ``w:tc`` 之前；
#: 靠 ``TR_PR_ORDER`` 去定位 ``w:trPr`` 是常见的错误——那张表描述的是 ``w:trPr``
#: 的**内部**顺序，把它当成 ``w:tr`` 的子元素表用，会静默退化成"追加到末尾"。
TR_ORDER: Final[tuple[str, ...]] = ("tblPrEx", "trPr", "tc")


def is_paragraph(element: Element) -> bool:
    """是否为段落元素。"""
    return localname(element) == PARAGRAPH_LOCAL


def is_table(element: Element) -> bool:
    """是否为表格元素。"""
    return localname(element) == TABLE_LOCAL
