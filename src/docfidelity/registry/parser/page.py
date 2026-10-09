"""入库流水线第 2 阶段：版心契约（DD-7.1 / DD-7.2「页面」行）。

分节的排列顺序（容易写反）
--------------------------
OOXML 里一个 ``w:sectPr`` 描述的是**到它为止**那一节的属性：

* 段落内的 ``w:pPr/w:sectPr`` = 该段落所在节的**结束**标记；
* ``w:body`` 直属的 ``w:sectPr``（永远是 body 的最后一个子元素）= **最后一节**。

所以按文档顺序遍历 ``w:sectPr`` 得到的正是第 1、2、…、末节。DD-7.2 的表述
（「首节取 body/sectPr」）与 OOXML 语义相反，这里按语义实现——因为
``geometry.usable_width(page, section_index=0)`` 被用来给首屏算版心宽，把末节
的页面尺寸当成第 0 节会让分节文档的图片缩放和表格列宽整体算错。

页眉页脚引用存"部件路径"而不是 r:id
----------------------------------
契约字段 ``header_ref`` / ``footer_ref`` 要求是**归一化后**的值。关系 ID
（``rId5``）本身是易变量（Word 插入页眉就会整片重排），存它等于把不稳定值写进
契约，指纹会随 Word 的任意一次编辑而变。因此这里解析到目标部件路径
（``word/header1.xml``），它既稳定又直接可用。
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Final, Literal

from docfidelity.contracts.template import (
    HeaderFooterRole,
    Margins,
    PageSetupContract,
    PageSize,
    SectionSpec,
)
from docfidelity.ooxml import DocxPackage, Element, resolve_target_path
from docfidelity.ooxml.doc import attr, child_of, iter_elements, qn

#: 分节起始方式。与 ``SectionSpec.start_type`` 的取值范围一致。
SectionStartType = Literal["continuous", "nextPage", "evenPage", "oddPage"]

#: 纸张方向。与 ``PageSize.orientation`` 一致。
PageOrientation = Literal["portrait", "landscape"]

#: 缺省纸张尺寸（twips）：Letter。缺失 ``w:pgSz`` 时按 Word 的默认值处理。
DEFAULT_PAGE_WIDTH: Final = 12240
DEFAULT_PAGE_HEIGHT: Final = 15840

#: 缺省页边距（twips）：1 英寸。
DEFAULT_MARGIN: Final = 1440

_ROLE_BY_TYPE: Final[dict[str, HeaderFooterRole]] = {
    "default": "default",
    "first": "first",
    "even": "even",
}

#: 角色与取值的合法顺序。用元组遍历而不是集合成员判断，是为了让"取值合法"
#: 这件事由类型检查器保证——集合判断在静态类型下只能推出 str。
_ROLE_ORDER: Final[tuple[HeaderFooterRole, ...]] = ("default", "first", "even")

_START_TYPES: Final[tuple[SectionStartType, ...]] = (
    "continuous",
    "nextPage",
    "evenPage",
    "oddPage",
)

_ORIENTATIONS: Final[tuple[PageOrientation, ...]] = ("portrait", "landscape")

#: ``diff_from_previous`` 参与比较的字段名（顺序即界面呈现顺序）
_COMPARED_FIELDS: Final[tuple[str, ...]] = (
    "page_size",
    "margins",
    "header_ref",
    "footer_ref",
)


def parse_page_setup(pkg: DocxPackage) -> PageSetupContract:
    """解析全部节，产出版心契约。

    单节文档（绝大多数模板）与多节文档走同一条代码路径：单节时结果就是
    ``sections`` 长度为 1，且 ``page_size`` / ``margins`` 与它一致。
    """
    sections = [_section_spec(pkg, index, element) for index, element in enumerate(_sections(pkg))]
    if not sections:
        # 完全没有 sectPr 的包不是合法 Word 文档，但既然能打开就按默认版心处理，
        # 而不是让整个流水线失败——版心拿不到默认值比拿不到契约温和得多。
        fallback = PageSize(width_twips=DEFAULT_PAGE_WIDTH, height_twips=DEFAULT_PAGE_HEIGHT)
        return PageSetupContract(
            page_size=fallback,
            margins=_default_margins(),
        )
    _annotate_diffs(sections)
    return PageSetupContract(
        page_size=sections[0].page_size,
        margins=sections[0].margins,
        sections=sections,
    )


def _sections(pkg: DocxPackage) -> Iterator[Element]:
    part = pkg.find_part("word/document.xml")
    if part is None:
        return
    yield from iter_elements(part.read_tree, "w:sectPr")


def _section_spec(pkg: DocxPackage, index: int, element: Element) -> SectionSpec:
    role_targets = _header_footer_targets(pkg, element)
    header, footer = role_targets.get("default", (None, None))
    roles: list[HeaderFooterRole] = [role for role in _ROLE_ORDER if role in role_targets]
    return SectionSpec(
        index=index,
        page_size=_page_size(element),
        margins=_margins(element),
        start_type=_start_type(element),
        header_ref=header,
        footer_ref=footer,
        header_footer_roles=roles,
    )


def _page_size(element: Element) -> PageSize:
    size = child_of(element, "w:pgSz")
    width = _int_attr(size, "w:w") or DEFAULT_PAGE_WIDTH
    height = _int_attr(size, "w:h") or DEFAULT_PAGE_HEIGHT
    return PageSize(
        width_twips=width,
        height_twips=height,
        orientation=_orientation(attr(size, "w:orient"), width, height),
    )


def _orientation(value: str | None, width: int, height: int) -> PageOrientation:
    """显式 ``w:orient`` 优先；否则按尺寸推断。

    横版文档常省略该属性，直接用默认的 portrait 会让后续"版心宽"取错边，
    图片缩放与表格列宽随之整体算错。
    """
    for candidate in _ORIENTATIONS:
        if value == candidate:
            return candidate
    return "landscape" if width > height else "portrait"


def _margins(element: Element) -> Margins:
    margins = child_of(element, "w:pgMar")
    if margins is None:
        return _default_margins()
    return Margins(
        top_twips=_int_attr(margins, "w:top") or DEFAULT_MARGIN,
        bottom_twips=_int_attr(margins, "w:bottom") or DEFAULT_MARGIN,
        left_twips=_int_attr(margins, "w:left") or DEFAULT_MARGIN,
        right_twips=_int_attr(margins, "w:right") or DEFAULT_MARGIN,
        header_twips=_int_attr(margins, "w:header"),
        footer_twips=_int_attr(margins, "w:footer"),
        gutter_twips=_int_attr(margins, "w:gutter"),
    )


def _default_margins() -> Margins:
    return Margins(
        top_twips=DEFAULT_MARGIN,
        bottom_twips=DEFAULT_MARGIN,
        left_twips=DEFAULT_MARGIN,
        right_twips=DEFAULT_MARGIN,
    )


def _start_type(element: Element) -> SectionStartType | None:
    value = attr(child_of(element, "w:type"), "w:val")
    for candidate in _START_TYPES:
        if value == candidate:
            return candidate
    return None


def _header_footer_targets(
    pkg: DocxPackage, element: Element
) -> dict[str, tuple[str | None, str | None]]:
    """``{角色: (页眉目标, 页脚目标)}``。

    同一角色在不同节里可以指向不同部件，所以这张表是逐节建的，不缓存。
    """
    owner = "word/document.xml"
    relationships = {item.rel_id: item for item in pkg.relationships(owner)}
    roles: dict[str, tuple[str | None, str | None]] = {}
    for tag, slot in (("w:headerReference", 0), ("w:footerReference", 1)):
        for reference in element:
            if reference.tag != qn(tag):
                continue
            role = _ROLE_BY_TYPE.get(attr(reference, "w:type") or "")
            if role is None:
                continue
            rel_id = attr(reference, "r:id")
            target: str | None = None
            if rel_id is not None and rel_id in relationships:
                target = resolve_target_path(owner, relationships[rel_id])
            current = list(roles.get(role, (None, None)))
            current[slot] = target
            roles[role] = (current[0], current[1])
    return roles


def _int_attr(element: Element | None, name: str) -> int | None:
    """读整数属性；非数字一律当缺失，而不是抛错让整个阶段失败。"""
    raw = attr(element, name)
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _annotate_diffs(sections: list[SectionSpec]) -> None:
    """就地标注每节相对前一节变化的字段。第 0 节无前节，列表为空。"""
    for index in range(1, len(sections)):
        previous = sections[index - 1]
        current = sections[index]
        changed = [
            field
            for field in _COMPARED_FIELDS
            if getattr(previous, field) != getattr(current, field)
        ]
        current.diff_from_previous = changed


__all__ = [
    "DEFAULT_MARGIN",
    "DEFAULT_PAGE_HEIGHT",
    "DEFAULT_PAGE_WIDTH",
    "parse_page_setup",
]
