"""编号契约夹具：多级编号定义、标题绑定与项目符号段落。

从 ``docx_builder`` 拆出的第二块（NFR-09 行数上限），内容未改。之所以值得独立成模块，
是因为这里是**编号契约**的输入面：``render.context._bullet_num_ids`` 按 ``numFmt``
判"项目符号 vs 有序"，``registry`` 按 ``nsid`` 判两份定义该不该合并——两件事都只看
这一份 XML。任何一处"顺手另写一份 abstractNum"都会让两个判据不再作用于同一组输入。
"""

from __future__ import annotations

from typing import Any

from lxml import etree

from .docx_parts import CT_PREFIX, PKG_REL_PREFIX, W_PREFIX, R, W, parse, serialize

NUMBERING_CONTENT_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.numbering+xml"
)
NUMBERING_REL_TYPE = f"{R}/numbering"

#: 项目符号编号实例的 ``numId``（见 :func:`numbering_xml` 的第四条定义）。
BULLET_NUM_ID = "3"

#: ``w:pPr`` 子元素的**模式声明顺序**（CT_PPrBase，只列到 ``outlineLvl``）。
#:
#: 用途只有一个：往已有的 ``w:pPr`` 里插 ``w:numPr`` 时要知道插在哪。写错顺序 Word 一般
#: 能忍（它会自己修），但"插在 index 0"把 ``numPr`` 排到了 ``keepNext`` 之前，是明确的
#: 模式违例——而**没有任何离线检查会报**：lxml 只验证良构、python-docx 只按名取元素、
#: 本仓库的解析层同理。故顺序作为常量固化在这里，别再用 ``insert(0, ...)``。
P_PPR_ORDER: tuple[str, ...] = (
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
)


def _localname(element: etree._Element) -> str:
    return etree.QName(element).localname


def insert_in_schema_order(properties: etree._Element, child: etree._Element) -> None:
    """把 ``child`` 插进 ``properties`` 的**模式顺序**位置（不认识的元素排到最后）。"""
    rank = P_PPR_ORDER.index(_localname(child))
    for index, sibling in enumerate(properties):
        name = _localname(sibling)
        if name in P_PPR_ORDER and P_PPR_ORDER.index(name) > rank:
            properties.insert(index, child)
            return
    properties.append(child)


def _style_properties(style: etree._Element) -> etree._Element:
    """取（必要时新建）样式自己的 ``w:pPr``，并保持它排在 ``w:rPr`` 之前。"""
    properties = style.find(f"{W_PREFIX}pPr")
    if properties is not None:
        return properties
    properties = etree.Element(f"{W_PREFIX}pPr")
    run_properties = style.find(f"{W_PREFIX}rPr")
    if run_properties is not None:
        style.insert(list(style).index(run_properties), properties)
    else:
        style.append(properties)
    return properties


def register_numbering(parts: dict[str, bytes], document_root: etree._Element) -> None:
    """把 ``word/numbering.xml`` 挂进内容类型与文档关系（引用方是文档本体，故此形参）。

    **本函数必须幂等**——这是一处真实缺陷的落点：python-docx 的基线包里
    ``word/numbering.xml`` 已经登记过一次，再挂一遍就会写出**第二个**
    ``<Override PartName="/word/numbering.xml">``。一份包对同一部件只能有一条内容类型
    声明，多出来的那条 Word 直接判"文件已经损坏"，而 lxml / python-docx / 本仓库的
    解析器全都照读不误（重复键在 ``dict`` 里被静默折叠成一条，对拍看不出来）。
    关系同理：同一个 ``numbering`` 关系只能留一条，之后若发现包里的编号定义没生效，
    先回来查这里有没有多挂一条。
    """
    content_types = parse(parts["[Content_Types].xml"])
    part_name = "/word/numbering.xml"
    declared = [
        element
        for element in content_types
        if element.tag == f"{CT_PREFIX}Override" and element.get("PartName") == part_name
    ]
    for duplicate in declared[1:]:
        content_types.remove(duplicate)
    if declared:
        declared[0].set("ContentType", NUMBERING_CONTENT_TYPE)
    else:
        override = etree.SubElement(content_types, f"{CT_PREFIX}Override")
        override.set("PartName", part_name)
        override.set("ContentType", NUMBERING_CONTENT_TYPE)
    parts["[Content_Types].xml"] = serialize(content_types)

    rels_path = "word/_rels/document.xml.rels"
    rels = parse(parts[rels_path])
    bound = [element for element in rels if element.get("Type") == NUMBERING_REL_TYPE]
    for duplicate in bound[1:]:
        rels.remove(duplicate)
    if bound:
        bound[0].set("Target", "numbering.xml")
    else:
        used = {element.get("Id") for element in rels}
        index = 1
        while f"rIdNum{index}" in used:
            index += 1
        relationship = etree.SubElement(rels, f"{PKG_REL_PREFIX}Relationship")
        relationship.set("Id", f"rIdNum{index}")
        relationship.set("Type", NUMBERING_REL_TYPE)
        relationship.set("Target", "numbering.xml")
    parts[rels_path] = serialize(rels)


def bind_heading_numbering(styles_root: etree._Element) -> None:
    """给 Heading1 / Heading2 挂上 ``w:numPr``，让 numId=1 成为"被引用的实例"。"""
    for style_id, level in (("Heading1", 0), ("Heading2", 1)):
        for style in styles_root.findall(f"{W_PREFIX}style"):
            if style.get(f"{W_PREFIX}styleId") != style_id:
                continue
            properties = _style_properties(style)
            num_properties = etree.Element(f"{W_PREFIX}numPr")
            ilvl = etree.SubElement(num_properties, f"{W_PREFIX}ilvl")
            ilvl.set(f"{W_PREFIX}val", str(level))
            num_id = etree.SubElement(num_properties, f"{W_PREFIX}numId")
            num_id.set(f"{W_PREFIX}val", "1")
            insert_in_schema_order(properties, num_properties)


def numbering_xml() -> bytes:
    """两份层级签名相同、nsid 不同的定义（应被合并），加两份不同的（应被保留）。

    第四份（``numId=3``）是**项目符号**定义（``numFmt=bullet``）。它不是装饰：
    ``render.context._bullet_num_ids`` 按编号契约里的 ``numFmt`` 判"符号 vs 有序"，
    夹具没有一份 bullet 定义，示例模板就永远取不到项目符号原型——而"列表原型取不到"
    的降级形态是**静默回落**到正文原型（列表没了符号，但报告全绿）。
    """
    header = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    return (
        header
        + f'<w:numbering xmlns:w="{W}">'
        + _abstract_num(0, "11111111")
        + _abstract_num(1, "22222222")
        + _abstract_num_other(2, "33333333")
        + _abstract_num_bullet(3, "44444444")
        + '<w:num w:numId="1"><w:abstractNumId w:val="1"/></w:num>'
        + '<w:num w:numId="2"><w:abstractNumId w:val="2"/></w:num>'
        + f'<w:num w:numId="{BULLET_NUM_ID}"><w:abstractNumId w:val="3"/></w:num>'
        + "</w:numbering>"
    ).encode("utf-8")


def _abstract_num_bullet(identifier: int, nsid: str) -> str:
    return (
        f'<w:abstractNum w:abstractNumId="{identifier}">'
        f'<w:nsid w:val="{nsid}"/>'
        '<w:lvl w:ilvl="0"><w:start w:val="1"/><w:numFmt w:val="bullet"/>'
        '<w:lvlText w:val="●"/><w:pPr><w:ind w:left="425" w:hanging="425"/></w:pPr></w:lvl>'
        "</w:abstractNum>"
    )


def _abstract_num(identifier: int, nsid: str) -> str:
    return (
        f'<w:abstractNum w:abstractNumId="{identifier}">'
        f'<w:nsid w:val="{nsid}"/>'
        '<w:lvl w:ilvl="0"><w:start w:val="1"/><w:numFmt w:val="decimal"/>'
        '<w:lvlText w:val="%1"/><w:pPr><w:ind w:left="425" w:hanging="425"/></w:pPr></w:lvl>'
        '<w:lvl w:ilvl="1"><w:start w:val="1"/><w:numFmt w:val="decimal"/>'
        '<w:lvlText w:val="%1.%2"/><w:pPr><w:ind w:left="851" w:hanging="425"/></w:pPr></w:lvl>'
        "</w:abstractNum>"
    )


def _abstract_num_other(identifier: int, nsid: str) -> str:
    return (
        f'<w:abstractNum w:abstractNumId="{identifier}">'
        f'<w:nsid w:val="{nsid}"/>'
        '<w:lvl w:ilvl="0"><w:start w:val="1"/><w:numFmt w:val="lowerLetter"/>'
        '<w:lvlText w:val="%1)"/></w:lvl>'
        "</w:abstractNum>"
    )


def strip_list_numbering(styles_root: etree._Element) -> None:
    """摘掉内置列表样式的 ``w:numPr``，让夹具的编号引用关系完全可控。"""
    for style in styles_root.findall(f"{W_PREFIX}style"):
        style_id = style.get(f"{W_PREFIX}styleId") or ""
        if not style_id.startswith("List"):
            continue
        for num_properties in style.findall(f"{W_PREFIX}pPr/{W_PREFIX}numPr"):
            num_properties.getparent().remove(num_properties)


def add_bullet_paragraph(paragraph: Any, *, level: int = 0, num_id: str = BULLET_NUM_ID) -> Any:
    """把一个 ``python-docx`` 段落改造成**项目符号列表项**（``w:pPr/w:numPr`` 直设）。

    直设 ``numPr`` 而不是挂 ``ListParagraph`` 之类样式：渲染层判"符号 vs 有序"看的是
    段落直接引用的 ``numId`` 在编号契约里的 ``numFmt``（``render.context._bullet_num_ids``），
    按样式名猜的那一路三种写法都见过（``ListParagraph`` / ``列表段落`` / ``List``），不可靠。
    直设还有一条好处——``strip_list_numbering`` 清的是**样式里的** ``numPr``，碰不到这里。

    参数就是 :meth:`docx.document.Document.add_paragraph` 的返回值（仅用它的 ``_p``）。
    用 ``get_or_add_*`` 而不是 ``etree.SubElement``：``w:pPr`` 必须排在 ``w:r`` 之前，
    自己 append 会产出 schema 非法的段落，而 Word 打开时只会静默忽略那段格式。
    """
    properties = paragraph._p.get_or_add_pPr()
    numbering = properties.get_or_add_numPr()
    numbering.get_or_add_ilvl().val = level
    numbering.get_or_add_numId().val = int(num_id)
    return paragraph


__all__ = [
    "BULLET_NUM_ID",
    "NUMBERING_CONTENT_TYPE",
    "NUMBERING_REL_TYPE",
    "P_PPR_ORDER",
    "add_bullet_paragraph",
    "bind_heading_numbering",
    "insert_in_schema_order",
    "numbering_xml",
    "register_numbering",
    "strip_list_numbering",
]
