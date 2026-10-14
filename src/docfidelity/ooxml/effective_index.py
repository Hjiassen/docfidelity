"""只读结构索引：节点标识以源摘要和位置为作用域。"""

from docfidelity.contracts.imports import StructureKind, StructureNode

from .doc import Element, find_first, localname, paragraph_text, qn, xpath
from .effective_clean import unsupported_content
from .package import DOCUMENT_PART, DocxPackage
from .styles import style_element_index

_DRAWING_TAGS = frozenset(
    "drawing inline anchor extent effectExtent docPr cNvGraphicFramePr graphicFrameLocks positionH positionV align posOffset simplePos wrapNone wrapSquare wrapTight wrapThrough wrapTopAndBottom wrapPolygon start lineTo graphic graphicData pic nvPicPr cNvPr cNvPicPr picLocks blipFill blip srcRect stretch fillRect tile spPr xfrm off ext prstGeom avLst".split()
)


def supported_image(drawing: Element) -> bool:
    """仅支持不包含附加文本、图表或对象的单图片绘图。"""
    blips = xpath(drawing, ".//a:blip")
    return (
        len(blips) == 1
        and bool(blips[0].get(qn("r:embed")))
        and not blips[0].get(qn("r:link"))
        and all(localname(e) in _DRAWING_TAGS for e in drawing.iter())
    )


def content_roots(package: DocxPackage) -> dict[str, Element]:
    """枚举正文和被分节关系引用的页眉页脚。"""
    body = find_first(package.part(DOCUMENT_PART).read_tree, "w:body")
    if body is None:
        from docfidelity.contracts.errors import EngineError, ErrorCode

        raise EngineError(ErrorCode.TEMPLATE_UNREADABLE, "原件缺少正文容器")
    roots = {DOCUMENT_PART: body}
    for relationship in package.resolve_rels(DOCUMENT_PART):
        if relationship.relationship.role in {"header", "footer"} and relationship.part is not None:
            roots[relationship.target_path] = relationship.part.read_tree
    return roots


def heading_level(paragraph: Element, styles: dict[str, Element] | None = None) -> int | None:
    """优先采用大纲级别，再识别内置样式标识。"""
    levels = xpath(paragraph, "./w:pPr/w:outlineLvl")
    if levels:
        value = levels[0].get(qn("w:val"), "")
        if value.isdigit() and int(value) < 9:
            return int(value) + 1
    references = xpath(paragraph, "./w:pPr/w:pStyle")
    value = references[0].get(qn("w:val"), "") if references else ""
    visited: set[str] = set()
    while value and value not in visited:
        visited.add(value)
        style = (styles or {}).get(value)
        if style is not None:
            level = xpath(style, "./w:pPr/w:outlineLvl")
            if level:
                raw = level[0].get(qn("w:val"), "")
                return int(raw) + 1 if raw.isdigit() and int(raw) < 9 else None
        if value.lower().startswith("heading") and value[7:].isdigit():
            return int(value[7:])
        parent = find_first(style, "w:basedOn")
        value = parent.get(qn("w:val"), "") if parent is not None else ""
    return None


def index_structure(package: DocxPackage) -> list[StructureNode]:
    """索引完整块及可绑定的段落、绘图与不支持对象。"""
    nodes: list[StructureNode] = []
    styles = style_element_index(package) if package.has_part("word/styles.xml") else {}
    for part, root in content_roots(package).items():
        for block_index, block in enumerate(root):
            if block.tag == qn("w:sectPr"):
                continue
            for element in block.iter():
                tag = localname(element)
                if (
                    tag
                    not in {
                        "p",
                        "tbl",
                        "drawing",
                        "object",
                        "altChunk",
                        "OLEObject",
                        "oleObject",
                        "sdt",
                        "customXml",
                    }
                    and element is not block
                    and not unsupported_content(element)
                ):
                    continue
                kind = {
                    "p": "paragraph",
                    "tbl": "table",
                    "drawing": "image",
                    "sdt": "container",
                }.get(tag, "object")
                position = root.getroottree().getpath(element)
                # 真实路径由 XML 的命名空间前缀决定，冻结时只使用本索引。
                nodes.append(
                    StructureNode(
                        node_id=f"{part}#{len(nodes)}",
                        part=part,
                        xpath=position,
                        block_index=block_index,
                        kind=StructureKind(kind),
                        text_preview=paragraph_text(element)[:160] if kind == "paragraph" else "",
                        heading_level=heading_level(element, styles)
                        if kind == "paragraph"
                        else None,
                        supported=kind != "object"
                        and (kind != "image" or supported_image(element)),
                        top_level=element is block,
                    )
                )
    return nodes


def locate_node(package: DocxPackage, node: StructureNode) -> Element:
    """按已验证索引定位，不进行文字搜索。"""
    return xpath(package.part(node.part).read_tree, node.xpath)[0]
