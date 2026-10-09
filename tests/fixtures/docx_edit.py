"""产物包的就地编辑助手（测试专用）。

门禁的判据全部长在产物上，所以"造一个该被拦下的产物"最直接的办法是：真渲染一份
产物，再**只改坏一处**，然后看门禁有没有发现它。手搓一份"看起来像产物"的 docx
能跑通签名，却跑不出真实形态——分节属性挂在 ``w:pPr`` 里、图片尺寸 ``wp:extent``
与 ``a:ext`` 双写、目录藏在内容控件里。这些形态正是这些规则唯一会出错的地方。

助手只做**最小改动**：加一个属性、换一段文字、改一个网格宽度。改动越局部，
"规则命中"与"改动本身"的因果就越清楚——否则一条规则报红，说不清是它判对了
还是别的改动顺手触发了它。
"""

from __future__ import annotations

from typing import Any

from lxml import etree

from docfidelity.ooxml import (
    DOCUMENT_PART,
    DocxPackage,
    Element,
    ensure_child_in_order,
    ensure_paragraph_properties,
    find_all,
    find_first,
    iter_elements,
    localname,
    paragraph_text,
    qn,
    set_attr,
    set_text,
)
from docfidelity.render.order import P_PR_ORDER, R_PR_ORDER

__all__ = [
    "add_paragraph_attribute",
    "body_of",
    "body_paragraphs",
    "body_tables",
    "cell_paragraphs",
    "content_paragraph",
    "content_paragraphs",
    "first_text_paragraph",
    "front_matter_tables",
    "injected_tables",
    "make_bold",
    "row_cells",
    "set_grid_width",
    "set_paragraph_text",
    "set_picture_width",
]


def body_of(pkg: DocxPackage) -> Element:
    part = pkg.find_part(DOCUMENT_PART)
    assert part is not None, "产物缺少 word/document.xml"
    body = find_first(part.read_tree, "w:body")
    assert body is not None, "word/document.xml 缺少 w:body"
    return body


def body_paragraphs(pkg: DocxPackage) -> list[Element]:
    """``w:body`` 的**直接子段落**（不含表格单元格里的段落）。"""
    return find_all(body_of(pkg), "w:p")


def body_tables(pkg: DocxPackage) -> list[Element]:
    return find_all(body_of(pkg), "w:tbl")


def cell_paragraphs(table: Element) -> list[Element]:
    """表格里全部单元格段落（按文档序）。"""
    return list(iter_elements(table, "w:p"))


def row_cells(table: Element, row_index: int) -> list[Element]:
    rows = find_all(table, "w:tr")
    assert rows, "表格没有任何行"
    return find_all(rows[row_index], "w:tc")


def content_paragraph(pkg: DocxPackage, index: int = 0) -> Element:
    """正文中第 ``index`` 个**有文字**的直接子段落（不限内容区）。

    用于"改坏某个一定存在的段"这类与区间无关的改动（直设格式、分页）。
    要落在**注入内容区**里的段落请用 :func:`content_paragraphs`。
    """
    texts = [item for item in body_paragraphs(pkg) if paragraph_text(item).strip()]
    assert len(texts) > index, f"正文里没有第 {index + 1} 个有文字的段落"
    return texts[index]


def first_text_paragraph(paragraphs: list[Element]) -> Element:
    texts = [item for item in paragraphs if paragraph_text(item).strip()]
    assert texts, "给定的段落集合里没有有文字的段落"
    return texts[0]


def content_paragraphs(context: Any, *, include_tables: bool = False) -> list[Element]:
    """**注入内容区**（骨架标题之后、尾节属性之前）里的段落。

    判据复用渲染层的 ``content_bounds()``——门禁的三条样式规则用的就是它，
    测试另算一遍区间必然与规则漂移（"改了产物但规则没看见"）。

    ``include_tables=True`` 时连表格单元格段落一并返回：注入表格的文字同样是
    内容，模板保真（FR-M5-02）必须把它算进去。
    """
    view = _render_view(context)
    start, end = view.content_bounds()
    children = list(view.body)[start:end]
    if not include_tables:
        return [child for child in children if localname(child) == "p"]
    texts: list[Element] = []
    for child in children:
        if localname(child) == "p":
            texts.append(child)
        elif localname(child) == "tbl":
            texts.extend(iter_elements(child, "w:p"))
    return texts


def front_matter_tables(context: Any) -> list[Element]:
    """内容区**之前**的表格（封面 / 修订记录 / 目录自带，属受保护的前置版块）。"""
    view = _render_view(context)
    start, _end = view.content_bounds()
    return [child for child in list(view.body)[:start] if localname(child) == "tbl"]


def injected_tables(context: Any) -> list[Element]:
    """内容区**之内**的表格（渲染层注入的，样式规则要判的就是它们）。"""
    view = _render_view(context)
    start, end = view.content_bounds()
    return [child for child in list(view.body)[start:end] if localname(child) == "tbl"]


def _render_view(context: Any) -> Any:
    """取（并缓存）渲染视图；复用门禁规则的同一条构造路径。"""
    from docfidelity.qa.rules._support import render_view

    view = render_view(context)
    assert view is not None, "上下文缺少档案，构造不出渲染视图"
    return view


def add_paragraph_attribute(paragraph: Element, tag: str, value: str | None = None) -> Element:
    """给段落 ``w:pPr`` 加一个子元素（按 ``P_PR_ORDER`` 就位）。

    ``value`` 为 ``None`` 时只建空开关元素（如 ``w:pageBreakBefore``）。
    """
    element = ensure_child_in_order(ensure_paragraph_properties(paragraph), tag, P_PR_ORDER)
    if value is not None:
        set_attr(element, "w:val", value)
    return element


def set_paragraph_text(paragraph: Element, text: str) -> None:
    """改写段落里第一个 ``w:t`` 的文字（没有运行时就补一个）。"""
    texts = list(iter_elements(paragraph, "w:t"))
    if texts:
        set_text(texts[0], text)
        return
    run = find_first(paragraph, "w:r")
    if run is None:
        run = etree.SubElement(paragraph, qn("w:r"))
    set_text(etree.SubElement(run, qn("w:t")), text)


def make_bold(cell: Element) -> None:
    """把单元格里第一个运行标成直设加粗（``w:rPr/w:b``）。

    ``w:r`` 挂在单元格的 ``w:p`` 底下而不是 ``w:tc`` 的直接子元素，故用后代遍历。
    """
    runs = list(iter_elements(cell, "w:r"))
    assert runs, "单元格里没有运行"
    run_properties = ensure_child_in_order(runs[0], "w:rPr", R_PR_ORDER)
    bold = ensure_child_in_order(run_properties, "w:b", R_PR_ORDER)
    set_attr(bold, "w:val", "1")


def set_grid_width(table: Element, width: int) -> None:
    """把表格每一列的 ``w:tblGrid/w:gridCol/@w:w`` 改成指定宽度。"""
    grid = find_first(table, "w:tblGrid")
    assert grid is not None, "表格缺少 w:tblGrid"
    for column in find_all(grid, "w:gridCol"):
        set_attr(column, "w:w", str(width))


def set_picture_width(pkg: DocxPackage, width_emu: int) -> None:
    """把第一张图的 ``wp:extent/@cx`` 改到指定 EMU 宽度。

    ``cx`` 是**裸属性**（无命名空间），故直接 ``set``：``ooxml.set_attr`` 走
    ``qn`` 只接受带前缀的标签，对属性不适用。
    """
    found = list(iter_elements(body_of(pkg), "wp:extent"))
    assert found, "产物里没有图片 extent"
    found[0].set("cx", str(width_emu))
