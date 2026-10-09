"""分页策略（DD-9.3 / FR-M5-04 / FR-M4-08）。

DD-9.3 只给了三行伪代码，但那三行里藏着一处**自相矛盾**，必须先解掉：

* 第 2 步："按 ``policy.must_page_break`` 在**白名单**（cover / revision / toc
  及各文档声明的块）对应段落上重新设置 ``pageBreakBefore``"；
* 第 3 步："校验：**正文区间内 page_break 数必须为 0**"。

若白名单的落点就在正文区间内，两句话不可能同时成立。唯一自洽的解释是：**第 3 步
校验的是"有没有白名单之外的分页点"**——即"残留为 0"，不是"分页点为 0"。故本模块
的判据是：**每一个存在的分页点都必须落在白名单上**。白名单为空时，判据退化成
DD 原话的"正文区间内分页点为 0"。

另一处契约缺口：DD-9.3 引用 ``policy.must_page_break``，而 ``PaginationPolicy``
（``contracts.config``，已冻结）没有这个字段，它有 ``page_break_before_chapter``
加五个"成组"开关。故白名单来源只能是"受保护版块 + ``page_break_before_chapter``"。
加字段须重跑 ``export_schemas``（契约层纪律），本轮不加——现有表达能力已够。

两件容易混淆的事
----------------
* ``w:lastRenderedPageBreak`` **不是分页点**。它是 Word 记下的"上次渲染时这里断过
  页"，纯渲染残留。当成分页点统计，会让"正文无分页点"这条判据在任何用 Word 打开过
  的模板上恒假。故它被清除但不计数。
* 表格的两项（跨页重复表头、单行不跨页断开）分别由 ``TablePolicy.repeat_header_row``
  与 ``policy.avoid_row_split`` 管；前者在 ``render()`` 里由本策略的
  ``keep_table_header_row`` 派生，**同一个开关只有一处来源**。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from docfidelity.contracts.config import PaginationPolicy
from docfidelity.ooxml import (
    Element,
    attr,
    ensure_child_in_order,
    ensure_paragraph_properties,
    find_all,
    find_first,
    localname,
    qn,
    set_on_off,
)

from .context import (
    PAGINATION_RULE_ID,
    RenderContext,
    paragraph_style_id,
)
from .order import P_PR_ORDER, TR_ORDER, TR_PR_ORDER, is_paragraph, is_table

__all__ = [
    "CANT_SPLIT_LOCAL",
    "PAGE_BREAK_TYPE",
    "PaginationReport",
    "apply_pagination",
    "break_sites",
    "clear_page_breaks",
    "count_page_breaks",
    "validate_pagination",
    "whitelist_targets",
]

#: ``w:br`` 上表示"分页"的 ``w:type`` 取值。
PAGE_BREAK_TYPE: Final = "page"

#: 分页点的两种载体。``w:lastRenderedPageBreak`` 不在其中（见模块文档）。
BREAK_LOCAL: Final = "br"
BREAK_BEFORE_LOCAL: Final = "pageBreakBefore"

#: 渲染残留：Word 记下的"上次断页位置"。
RENDERED_BREAK_LOCAL: Final = "lastRenderedPageBreak"

#: ``w:trPr`` 上"单行不跨页断开"的开关。
CANT_SPLIT_LOCAL: Final = "cantSplit"

#: 需要在前面强制分页的受保护区类别（封面是文档开头，前面无页可分）。
_BREAK_BEFORE_KINDS: Final[tuple[str, ...]] = ("revision", "toc")

_BR_TAG: Final = qn("w:br")
_PPR_TAG: Final = qn("w:pPr")


@dataclass
class PaginationReport:
    """分页结果摘要。

    ``whitelist`` 存的是**落点标签**（``body[7]`` / ``nested:{path}``），不是元素：
    报告要可 JSON 化，而"是哪些段落"这件事由 ``validate_pagination`` 用元素本身
    判定（见该函数的说明）。
    """

    cleared_breaks: int = 0
    cleared_rendered_marks: int = 0
    applied_breaks: int = 0
    whitelist: list[str] = field(default_factory=list)
    off_whitelist: list[str] = field(default_factory=list)
    keep_next_set: int = 0
    rows_protected: int = 0

    def as_payload(self) -> dict[str, Any]:
        return {
            "cleared_breaks": self.cleared_breaks,
            "cleared_rendered_marks": self.cleared_rendered_marks,
            "applied_breaks": self.applied_breaks,
            "whitelist": list(self.whitelist),
            "off_whitelist": list(self.off_whitelist),
            "keep_next_set": self.keep_next_set,
            "rows_protected": self.rows_protected,
        }


# ------------------------------------------------------------------ 一、清除
def clear_page_breaks(body: Element) -> tuple[int, int]:
    """清除正文里全部分页点与渲染残留，返回 ``(分页点数, 渲染残留数)``。

    在整份正文里清、**不做区域判断**：残留分页点是模板自带的，它们散落在哪
    只有清一遍才知道（还可能藏在表格单元格里——那里出现的分页点必然是残留）。
    清完再按白名单重建，"有没有多出来的分页点"于是变成纯结构比对。
    """
    breaks = 0
    marks = 0
    for element in list(body.iter()):
        if element.tag == _BR_TAG and attr(element, "w:type") == PAGE_BREAK_TYPE:
            _detach(element)
            breaks += 1
        elif localname(element) == RENDERED_BREAK_LOCAL:
            _detach(element)
            marks += 1
    for properties in list(body.iter(_PPR_TAG)):
        break_before = find_first(properties, f"w:{BREAK_BEFORE_LOCAL}")
        if break_before is not None:
            properties.remove(break_before)
            breaks += 1
    return breaks, marks


def _detach(element: Element) -> None:
    parent = element.getparent()
    if parent is not None:
        parent.remove(element)


# ------------------------------------------------------------------ 二、重建
def whitelist_targets(ctx: RenderContext, policy: PaginationPolicy) -> list[Element]:
    """白名单落点（段落元素）。三个来源，都与"文档声明"挂钩，没有一处靠猜。

    ``ctx.declared_breaks`` 指注入期遇到的**显式分页单元**（IR 的 ``pagebreak``）。
    漏掉它，注入进去的强制分页会在第 9 步被 `clear_page_breaks` 清掉，
    而且校验会把它判成"未声明"——文档作者显式写下的分页点反而成了 blocker。

    ``page_break_before_chapter`` 那一支要看**两手来源**
    ---------------------------------------------------
    渲染期手上有 ``ctx.chapter_headings``（注入时登记的）；而门禁是拿**产物**重开一个
    视图，不重跑注入，那份登记是空的。只认登记的话，"按策略分页"的产物在门禁侧会被
    判成每一处章节分页都未声明——一个 blocker，而报告上看起来只是"文档分了页"。
    故登记为空时按**同一份 ``level_styles`` 与同一个"章"层级**在内容区里重新认一遍
    （见 :func:`_chapter_heading_paragraphs`）；不引入第二套口径。
    """
    targets: list[Element] = []
    for entry in ctx.region_entries(_BREAK_BEFORE_KINDS):
        paragraph = _first_paragraph_of(entry.element)
        if paragraph is not None:
            targets.append(paragraph)
    if policy.page_break_before_chapter:
        targets.extend(ctx.chapter_headings or _chapter_heading_paragraphs(ctx))
    targets.extend(ctx.declared_breaks)
    return targets


def _chapter_heading_paragraphs(ctx: RenderContext) -> list[Element]:
    """内容区里按"章"样式排版的段落（注入期登记不可用时的等价判据）。

    只取 ``ctx.chapter_level`` 那一层：更低层的标题**不是**章，按策略不该分页——
    把它们一并放进白名单，等于让"每章另起一页"这个开关悄悄变成"每个标题另起一页"。
    """
    style = ctx.level_styles.get(ctx.chapter_level)
    if style is None:
        return []
    start, end = ctx.content_bounds()
    children = list(ctx.body)
    return [
        element
        for element in children[start : min(end, len(children))]
        if is_paragraph(element) and paragraph_style_id(element) == style
    ]


def apply_pagination(ctx: RenderContext, policy: PaginationPolicy) -> PaginationReport:
    """先清除、再按白名单重建、最后落实"成组"开关并校验。"""
    report = PaginationReport()
    report.cleared_breaks, report.cleared_rendered_marks = clear_page_breaks(ctx.body)

    allowed: list[Element] = []
    for paragraph in whitelist_targets(ctx, policy):
        set_on_off(
            ensure_paragraph_properties(paragraph),
            f"w:{BREAK_BEFORE_LOCAL}",
            True,
            order=P_PR_ORDER,
        )
        allowed.append(paragraph)
        report.applied_breaks += 1
    report.whitelist = sorted(_label(ctx.body, item) for item in allowed)

    report.keep_next_set = _apply_keep_next(ctx, policy)
    report.rows_protected = _apply_row_protection(ctx, policy)
    report.off_whitelist = sorted(validate_pagination(ctx, allowed=allowed))
    return report


def _apply_keep_next(ctx: RenderContext, policy: PaginationPolicy) -> int:
    """标题与后继内容同页（``w:keepNext``）。

    ``min_body_lines_after_heading`` 的实现说明：Word 没有"标题后至少 N 行"这样的
    原语，可行手段只有 ``keepNext``（把下一段拉到同页）。故本实现把它近似为
    "标题 + 紧随的第一段"，大于 1 时把第一段也标上 ``keepNext``。近似必须说清：
    伪装成"精确控制 N 行"会让使用者以为调这个参数能一行行地控制。
    """
    count = 0
    if policy.keep_heading_with_next:
        count += _keep_headings_with_next(ctx, policy)
    if policy.avoid_figure_orphan:
        count += _keep_pictures_with_captions(ctx)
    return count


def _keep_headings_with_next(ctx: RenderContext, policy: PaginationPolicy) -> int:
    """标题与后继内容同页（``w:keepNext``）。

    遍历区间用 ``ctx.content_bounds()`` 而**不是** ``ctx.region``：后者是模板原件里
    的示例内容区，注入之后早已不是当前内容的范围。用错会让"注入出来的内容"
    整片落在区间之外——症状是"有些标题没有 keepNext，有些又有"，看起来像随机。
    """
    heading_styles = set(ctx.level_styles.values())
    start, end = ctx.content_bounds()
    children = list(ctx.body)
    count = 0
    for index in range(start, min(end, len(children))):
        element = children[index]
        if not is_paragraph(element) or paragraph_style_id(element) not in heading_styles:
            continue
        set_on_off(ensure_paragraph_properties(element), "w:keepNext", True, order=P_PR_ORDER)
        count += 1
        if policy.min_body_lines_after_heading > 1 and index + 1 < len(children):
            follower = children[index + 1]
            if is_paragraph(follower):
                set_on_off(
                    ensure_paragraph_properties(follower), "w:keepNext", True, order=P_PR_ORDER
                )
                count += 1
    return count


def _keep_pictures_with_captions(ctx: RenderContext) -> int:
    """图与图注同页：给含图形的段落挂 ``keepNext``。

    机制说明：``keepNext`` 的语义是"与**下一段**同页"，所以只要锁住图片段落，
    紧随其后的图注就跟着走。只锁图注段落是没用的——那时图已经留在上一页了。
    ``ooxml.pictures.bind_caption`` 对**注入**的图片已经挂过一次；这里覆盖
    "模板自带的图片"那部分。
    """
    count = 0
    for paragraph in [item for item in ctx.body.iter() if is_paragraph(item)]:
        if find_first(paragraph, "w:drawing") is None:
            continue
        set_on_off(ensure_paragraph_properties(paragraph), "w:keepNext", True, order=P_PR_ORDER)
        count += 1
    return count


def _apply_row_protection(ctx: RenderContext, policy: PaginationPolicy) -> int:
    """单行不跨页断开（``w:trPr/w:cantSplit``）。"""
    if not policy.avoid_row_split:
        return 0
    count = 0
    for table in [item for item in ctx.body.iter() if is_table(item)]:
        for row in find_all(table, "w:tr"):
            set_on_off(
                ensure_child_in_order(row, "w:trPr", TR_ORDER),
                f"w:{CANT_SPLIT_LOCAL}",
                True,
                order=TR_PR_ORDER,
            )
            count += 1
    return count


# ------------------------------------------------------------------ 三、校验
def break_sites(body: Element) -> list[tuple[Element, str, int | None]]:
    """正文里全部分页点：``(所在段落, 载体, body 子元素下标或 None)``。

    ``pageBreakBefore`` 的父节点是 ``w:pPr`` 而**不是** ``w:p``，故两处都用
    :func:`_owning_paragraph` 向上找段落。先前按 `getparent()` 取段落的写法让
    ``pageBreakBefore`` 一律取不到归属段落 → 被静默跳过 → "白名单之外的分页点"
    这条校验对 ``pageBreakBefore`` **从来不生效**，而症状是"校验通过"。
    这种"检查器恒返回空"的缺陷与检查器没写是一样的。

    下标为 ``None`` 表示该段落**不是 body 的直接子元素**（典型是藏在表格单元格
    里）——那不可能是声明白名单上的位置，必然是残留。
    """
    sites: list[tuple[Element, str, int | None]] = []
    for element in body.iter():
        if element.tag == _BR_TAG and attr(element, "w:type") == PAGE_BREAK_TYPE:
            kind = BREAK_LOCAL
        elif localname(element) == BREAK_BEFORE_LOCAL:
            kind = BREAK_BEFORE_LOCAL
        else:
            continue
        paragraph = _owning_paragraph(element)
        if paragraph is None:
            continue
        sites.append((paragraph, kind, _body_index(body, paragraph)))
    return sites


def count_page_breaks(body: Element) -> int:
    """正文里的分页点数（``w:br[@w:type=page]`` + ``w:pPr/pageBreakBefore``）。"""
    return len(break_sites(body))


def validate_pagination(ctx: RenderContext, *, allowed: Sequence[Element]) -> list[str]:
    """返回**不在白名单上**的分页点（空列表表示通过），并为每一处记 blocker。

    这就是 DD-9.3 第 3 步的判据（见模块文档对那处自相矛盾的拆解）。

    白名单按**元素本身**判定，不按下标：目录受保护区里的第一个段落藏在 ``w:sdt``
    里，根本不是 body 的直接子元素。用下标做键会让"我们自己放上去的分页点"
    被判成未声明 —— 校验器于是对最不该报的那一处恒真报警。
    用 ``==`` 比较（lxml 的 ``__eq__`` 比较底层节点，跨代理对象成立）。
    """
    offenders: list[str] = []
    for paragraph, kind, _index in break_sites(ctx.body):
        if any(paragraph == item for item in allowed):
            continue
        label = f"{kind}:{_label(ctx.body, paragraph)}"
        offenders.append(label)
        ctx.note_once(
            PAGINATION_RULE_ID,
            "blocker",
            f"off-whitelist-break:{label}",
            "正文里存在未声明的分页点（只允许保留白名单上的强制分页点）",
            part=ctx.document_part,
            element=paragraph,
            evidence=label,
            suggestion="删除该分页符，或把它的位置写进文档声明的强制分页点",
        )
    return offenders


# ------------------------------------------------------------------ 内部
def _owning_paragraph(element: Element) -> Element | None:
    for candidate in element.iterancestors():
        if is_paragraph(candidate):
            return candidate
    return None


def _first_paragraph_of(element: Element) -> Element | None:
    """区域内第一个段落（元素本身是段落就直接返回）。"""
    if is_paragraph(element):
        return element
    for child in element.iter():
        if is_paragraph(child):
            return child
    return None


def _body_index(body: Element, element: Element) -> int | None:
    """元素在 body 直接子元素中的下标；不是直接子元素返回 ``None``。

    用 ``is`` / ``==`` 比较而不是 XPath 字符串往返：lxml 的 ``__eq__`` 比较的是底层
    节点，跨代理对象也成立，而 ``getpath`` 给出的前缀依赖树根的 nsmap——模板换了
    声明前缀（``w`` 是惯例但不是强制）就再也查不回来。
    """
    for index, child in enumerate(body):
        if child is element or child == element:
            return index
    return None


def _label(body: Element, element: Element) -> str:
    """分页点落点的稳定标签：直接子元素给 ``body[i]``，嵌套的给它的 XPath。"""
    index = _body_index(body, element)
    if index is not None:
        return f"body[{index}]"
    try:
        return element.getroottree().getpath(element)
    except AttributeError:  # 游离元素（无 root tree）
        return "detached"
