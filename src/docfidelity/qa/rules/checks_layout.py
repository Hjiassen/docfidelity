"""版式与结构类规则：FR-M5-04 / 05 / 06 / 07 / 11 / 13。

这几条的判据分两类，刻意不混：

* **产物上量的**（04 分页、06 表格越界、07 图片越界）：读最终 ``.docx``，用
  ``wp:extent`` / ``tblGrid`` / 分页点这类**几何或计数**判据。禁用页数反推（硬规则 4）。
* **IR 上算的**（05 结构合规）：层级、跳号、空章节、孤立标题都是**内容结构**的属性，
  在 IR 上判定比在产物上反推可靠——产物里"空章节"与"标题后紧跟标题"长得一样。

11 / 13 是**对照型**规则：只在给了基线时才判，且必须走
:func:`docfidelity.qa.compare.compare`（内部统一走易变标识归一化），不得另写比对。
"""

from __future__ import annotations

from itertools import pairwise
from typing import Any, Final

from docfidelity.contracts.ir import ContentUnit
from docfidelity.contracts.qa import Finding
from docfidelity.ooxml import (
    Element,
    attr,
    find_all,
    find_first,
    is_overflow,
    read_extents,
    usable_height_emu,
    usable_width,
    usable_width_emu,
)

from ..compare import compare
from ..engine import QaContext, RuleSpec, register_check
from ._support import content_units, finding, for_each_paragraph, locate, tables_of

__all__ = [
    "check_pagination",
    "check_regression_baseline",
    "check_stable_comparison",
    "check_structure",
    "check_table_overflow",
    "check_picture_overflow",
]

#: FR-M5-05 默认允许的标题层级深度。
_DEFAULT_LEVELS: Final[tuple[int, ...]] = (1, 2, 3)

#: FR-M5-06 默认容差（twips）。列宽换算与舍入会带来个位数偏差。
_DEFAULT_TOLERANCE: Final = 20

#: FR-M5-06 判据里的内容单元类型（表格）。
_TABLE_KIND: Final = "table"

#: 图片承载元素（DrawingML 与旧式 VML 各一种）。
_DRAWING_LOCALS: Final[tuple[str, ...]] = ("drawing", "pict")

#: 显式"关闭"某个开关的取值。
_FALSE_ATTRS: Final[frozenset[str]] = frozenset({"0", "false"})


# ------------------------------------------------------------------ FR-M5-04
def check_pagination(spec: RuleSpec, context: QaContext) -> list[Finding]:
    """FR-M5-04：正文分页符数 = 0（仅封面 / 修订记录 / 目录三处允许）。

    判据**复用渲染层的分页校验**（``whitelist_targets`` + ``validate_pagination``），
    但跑在**最终产物**上。这是"同一实现、独立执行"：口径只有一份，证据独立取证。
    若在 ``qa`` 里重写一份白名单推导，"门禁报的分页点"与"产物里的分页点"
    迟早不是同一处——而那正是最难查的一类不一致。

    白名单必须按**本次构建实际使用的策略**推导（``context.pagination``），
    不能就地取 ``PaginationPolicy()`` 默认值：开了 ``page_break_before_chapter``
    的产物，其章节分页是**合规**的，用默认策略判会把它们全报成 blocker——
    而报告上看起来只是"文档分了页"，没人会怀疑是判据拿错了策略。
    """
    from docfidelity.render import whitelist_targets
    from docfidelity.render.pagination import validate_pagination

    view = _require_view(context)
    if view is None:
        return []
    allowed = whitelist_targets(view, context.pagination)
    offenders = validate_pagination(view, allowed=allowed)
    if not offenders:
        return []
    return [
        finding(
            spec,
            "blocker",
            f"正文里存在 {len(offenders)} 处未声明的分页点（只允许封面 / 修订记录 / 目录处）",
            part=context.document_part,
            evidence=", ".join(offenders[:10]),
            suggestion="删除这些分页符，或把位置写进声明的强制分页点",
        )
    ]


# ------------------------------------------------------------------ FR-M5-05
def check_structure(spec: RuleSpec, context: QaContext) -> list[Finding]:
    """FR-M5-05：层级深度 1–3、编号连续无跳号、无空章节、无孤立标题。

    在**内容 IR** 上判定。产物上反推做不到："1. 概述 / 1.1 范围 / 正文" 与
    "1. 概述 / 正文" 在版面上完全同形，只有 IR 知道 概述 底下直接挂着什么。
    """
    ordered = list(content_units(context.ir))
    headings = [unit for unit in ordered if unit.type == "heading"]
    allowed = _allowed_levels(spec)
    findings: list[Finding] = []

    for unit in headings:
        level = unit.level
        if level is None or level not in allowed:
            findings.append(
                finding(
                    spec,
                    "major",
                    f"标题层级 {level!r} 不在允许范围 {sorted(allowed)}",
                    unit_id=unit.unit_id,
                    evidence=(unit.text or "")[:80],
                    suggestion="调整投影规则的层次，或把该层级并入允许范围",
                )
            )

    for previous, current in pairwise(headings):
        if (current.level or 0) > (previous.level or 0) + 1:
            findings.append(
                finding(
                    spec,
                    "major",
                    f"标题层级跳号：{previous.level} → {current.level}（编号不连续）",
                    unit_id=current.unit_id,
                    evidence=(current.text or "")[:80],
                    suggestion="补上中间层级，或修正投影规则里的分组层次",
                )
            )

    for index, unit in enumerate(ordered):
        if unit.type != "heading":
            continue
        if not _section_has_content(unit):
            findings.append(
                finding(
                    spec,
                    "major",
                    "空章节：该标题下没有任何内容单元",
                    unit_id=unit.unit_id,
                    evidence=(unit.text or "")[:80],
                    suggestion="补上该章节的内容，或从档案骨架里移除该章节",
                )
            )
        following = ordered[index + 1] if index + 1 < len(ordered) else None
        if following is not None and following.type == "heading":
            findings.append(
                finding(
                    spec,
                    "minor",
                    "孤立标题：该标题后紧接另一个标题，中间没有正文",
                    unit_id=unit.unit_id,
                    evidence=(unit.text or "")[:80],
                    suggestion="确认这是刻意的分组标题；否则在两者之间补上概述性正文",
                )
            )
    return findings


def _allowed_levels(spec: RuleSpec) -> set[int]:
    raw = spec.params.get("allowed_levels", list(_DEFAULT_LEVELS))
    if not isinstance(raw, list):
        return set(_DEFAULT_LEVELS)
    levels: set[int] = set()
    for item in raw:
        try:
            levels.add(int(item))
        except (TypeError, ValueError):
            continue
    return levels or set(_DEFAULT_LEVELS)


def _section_has_content(heading: ContentUnit) -> bool:
    """该标题的**子树**里有没有承载内容的单元（不含标题自身）。"""
    for child in heading.children:
        if child.text and child.text.strip():
            return True
        if child.type in _CONTENT_BEARING:
            return True
        if any(key in child.fields for key in _CONTENT_FIELDS):
            return True
        if _section_has_content(child):
            return True
    return False


#: 自带内容的单元类型（标题之外）。
_CONTENT_BEARING: Final[tuple[str, ...]] = (
    "para",
    "bullets",
    "numbered",
    "table",
    "figure",
    "screenshot",
)

#: 带这几个字段的单元也算有内容（表格/图片走 fields）。
_CONTENT_FIELDS: Final[tuple[str, ...]] = ("columns", "rows", "image_ref", "caption")


# ------------------------------------------------------------------ FR-M5-06
def check_table_overflow(spec: RuleSpec, context: QaContext) -> list[Finding]:
    """FR-M5-06：表格总宽不超版心宽。

    判据 = ``Σ w:tblGrid/w:gridCol/@w:w`` 与 ``usable_width`` 比较。用网格**声明**宽度
    而不是渲染后的视觉宽度：后者要排版引擎才算得出来，而门禁必须在没有 Word 的
    机器上也能跑（NFR-05 的同一条理由）。
    """
    from docfidelity.render.tables import table_grid

    available = usable_width(context.template.page_setup)
    tolerance = _tolerance(spec)
    findings: list[Finding] = []
    for part, table in tables_of(context.pkg):
        widths = table_grid(table)
        if not widths:
            continue
        total = sum(widths)
        if total <= available + tolerance:
            continue
        findings.append(
            finding(
                spec,
                "blocker",
                f"表格总宽 {total} twips 超出版心 {available} twips",
                part=part,
                xpath=locate(table),
                evidence=f"grid={widths}",
                suggestion="让渲染层按版心归一列宽，或减少列数 / 调窄固定列",
            )
        )
    return findings


def _tolerance(spec: RuleSpec) -> int:
    raw = spec.params.get("tolerance_twips", _DEFAULT_TOLERANCE)
    try:
        return int(raw)
    except (TypeError, ValueError):
        return _DEFAULT_TOLERANCE


# ------------------------------------------------------------------ FR-M5-07
def check_picture_overflow(spec: RuleSpec, context: QaContext) -> list[Finding]:
    """FR-M5-07：逐张量 ``wp:extent`` 的 cx/cy，全部落在版心内；图注与图不分离。

    "图注与图不分离"只做**可机械判定**的那一半：含图片的段落必须置 ``w:keepNext``。
    为什么不判"下一段到底是不是图注"——那需要档案声明题注样式；没有声明时靠文本
    猜（"图 " / "图：" / "Figure"）会在中英模板上给出两种结论，**而猜错的代价是
    一条永远正确的告警**（不猜，硬规则 8 的同一条纪律）。
    """
    available_w = usable_width_emu(context.page_setup)
    available_h = usable_height_emu(context.page_setup)
    findings: list[Finding] = []

    for extent in read_extents(context.pkg):
        if not is_overflow(extent.cx, extent.cy, available_w, available_h):
            continue
        findings.append(
            finding(
                spec,
                "blocker",
                "图片尺寸超出可用区域（判据：wp:extent 的 cx/cy）",
                part=extent.part,
                xpath=extent.xpath,
                evidence=f"cx={extent.cx}, cy={extent.cy}, 版心={available_w}×{available_h} EMU",
                suggestion="缩小该图，或把它所在分节改为横向",
            )
        )

    floating: list[str] = []
    for paragraph in for_each_paragraph(context.pkg, context.document_part):
        if not _has_drawing(paragraph):
            continue
        keep = find_first(find_first(paragraph, "w:pPr"), "w:keepNext")
        if keep is None or attr(keep, "w:val") in _FALSE_ATTRS:
            floating.append(locate(paragraph) or "<unknown>")
    if floating:
        findings.append(
            finding(
                spec,
                "major",
                f"{len(floating)} 张图片所在段落未置 keepNext，图与紧随的图注可能被分页拆开",
                part=context.document_part,
                evidence=", ".join(floating[:10]),
                suggestion="为图片段落置 w:keepNext（渲染层的图注绑定本应完成这一步）",
            )
        )
    return findings


def _has_drawing(paragraph: Element) -> bool:
    for run in find_all(paragraph, "w:r"):
        if any(find_first(run, f"w:{name}") is not None for name in _DRAWING_LOCALS):
            return True
    return False


# ------------------------------------------------------------------ FR-M5-11 / 13
def check_stable_comparison(spec: RuleSpec, context: QaContext) -> list[Finding]:
    """FR-M5-11：与基线**归一化后**比对（避免易变标识造成的假差异）。

    语义上它是**幂等性**判据：基线是同一输入的上一次产物时，任何差异都说明
    "连续两次构建产出了不同的文档"（NFR-02）。比对一律走 :func:`compare`
    （内部用 ``normalized_parts``），故 ``_Toc`` 书签 ID 这类波动不会报出来。
    """
    if context.baseline is None:
        return []
    diff = compare(
        context.baseline,
        context.pkg,
        mode="text",
        baseline_ref=context.baseline_ref,
        candidate_ref="current",
    )
    if diff.equal:
        return []
    return [
        finding(
            spec,
            "major",
            f"产物与基线在归一化后仍不一致：{diff.summary()}",
            part=context.document_part,
            evidence=" | ".join(
                [*diff.snapshot.added, *diff.snapshot.removed, *diff.snapshot.changed][:10]
            ),
            suggestion="检查构建是否引入了非确定性（时间戳 / 关系 ID 分配顺序 / 迭代顺序）",
        )
    ]


def check_regression_baseline(spec: RuleSpec, context: QaContext) -> list[Finding]:
    """FR-M5-13：与上一版产物比对，输出结构差异（信息性，minor）。"""
    if context.baseline is None:
        return []
    diff = compare(
        context.baseline,
        context.pkg,
        mode="structure",
        baseline_ref=context.baseline_ref,
        candidate_ref="current",
    )
    if diff.equal:
        return []
    return [
        finding(
            spec,
            "minor",
            f"与上一版产物的结构存在差异：{diff.summary()}",
            part=context.document_part,
            evidence=" | ".join(diff.snapshot.changed[:10]),
            suggestion="确认这些结构变化是本次改动的预期结果",
        )
    ]


def _require_view(context: QaContext) -> Any:
    from ._support import render_view

    return render_view(context)


register_check("pagination", check_pagination)
register_check("structure", check_structure)
register_check("table_overflow", check_table_overflow)
register_check("picture_overflow", check_picture_overflow)
register_check("stable_comparison", check_stable_comparison)
register_check("regression_baseline", check_regression_baseline)
