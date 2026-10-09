"""图片与图注：``wp:extent`` 读写、只缩不放、图注绑定（DD-4.5）。

两条硬规则在本模块落地
----------------------
* **越界的唯一判据是 ``wp:extent`` 的 ``cx``/``cy``**（FR-M5-07 / 1.2 第 6 条）：
  禁止用页数、渲染像素或 PDF 目检反推。:func:`read_extents` 是读取这一判据的
  唯一入口。
* **图片只缩不放**（1.4 原则）：缩放一律经
  :func:`~docfidelity.ooxml.geometry.fit_picture`，且 ``allow_grow=False``。

另有一条容易漏的：``wp:extent`` 改了但 ``a:xfrm/a:ext`` 没改时，Word 以 ``a:ext``
为准渲染，于是"程序认为已缩到版心内、Word 里仍然溢出"。两者必须同时改，
:func:`resize_pictures` 因此同时更新两处。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from lxml import etree

from docfidelity.contracts.errors import EngineError, ErrorCode

from .doc import (
    Element,
    child_of,
    ensure_paragraph_properties,
    find_first,
    localname,
    new_tree,
    paragraph_text,
    qn,
    set_on_off,
    set_paragraph_text,
    xpath,
    xpath_count,
)
from .geometry import fit_picture, pixels_to_emu
from .package import DOCUMENT_PART, DocxPackage, Part, content_type_for_extension

#: 关系类型与图形数据 URI（写成常量而非现场拼接，前缀写错会静默生成无效关系）。
IMAGE_REL_TYPE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/image"
PICTURE_GRAPHIC_URI = "http://schemas.openxmlformats.org/drawingml/2006/picture"

#: PNG 无 DPI 信息时按 96 处理——Word 对无 ``pHYs`` 的 PNG 也用这个值。
DEFAULT_DPI = 96


@dataclass(frozen=True)
class Extent:
    """一张图片的显示尺寸与位置。``cx`` / ``cy`` 单位为 EMU。"""

    part: str
    xpath: str
    cx: int
    cy: int
    anchor: bool
    paragraph_index: int

    @property
    def is_inline(self) -> bool:
        return not self.anchor


@dataclass
class ResizeReport:
    """缩放结果。"""

    resized: int = 0
    unchanged: int = 0
    skipped: int = 0
    min_scale: float = 1.0
    parts_touched: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


# --------------------------------------------------------------------- 读取
def read_extents(pkg: DocxPackage) -> list[Extent]:
    """逐张读取 ``wp:extent``，含所在部件、段落序号与内联 / 浮动形态。"""
    extents: list[Extent] = []
    for part in pkg.parts(scope="content"):
        tree = part.read_tree
        for element in tree.iter(qn("wp:extent")):
            cx = _int_or_none(element.get("cx"))
            cy = _int_or_none(element.get("cy"))
            if cx is None or cy is None:
                continue
            container = element.getparent()
            anchor = container is not None and localname(container) == "anchor"
            extents.append(
                Extent(
                    part=part.path,
                    xpath=tree.getroottree().getpath(element),
                    cx=cx,
                    cy=cy,
                    anchor=anchor,
                    paragraph_index=_paragraph_index_of(element),
                )
            )
    return extents


def _paragraph_index_of(element: Element) -> int:
    """元素所在段落的文档序。

    用 XPath 的 ``preceding`` 轴计数，而不是"先取全部 ``w:p`` 建 id 映射再反查"：
    lxml 的元素是**代理对象**，同一底层节点在不同时刻可能给出不同的代理，
    以 ``id()`` 为键会随机查不到（实测表现为序号恒为 -1），而这类错误只在
    特定文档上复现。``preceding::w:p`` 直接按文档序数，不依赖对象身份。
    """
    paragraphs = xpath(element, "ancestor::w:p[1]")
    if not paragraphs:
        return -1
    return xpath_count(paragraphs[0], "preceding::w:p")


def _int_or_none(raw: str | None) -> int | None:
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


# --------------------------------------------------------------------- 缩放
def resize_pictures(pkg: DocxPackage, *, avail_w_emu: int, avail_h_emu: int) -> ResizeReport:
    """把越界的图片等比缩到版心内；**不放大**。"""
    report = ResizeReport()
    for part in pkg.parts(scope="content"):
        if _resize_part(part.tree, avail_w_emu, avail_h_emu, report):
            report.parts_touched.append(part.path)
    return report


def _resize_part(tree: Element, avail_w_emu: int, avail_h_emu: int, report: ResizeReport) -> bool:
    touched = False
    for element in tree.iter(qn("wp:extent")):
        cx = _int_or_none(element.get("cx"))
        cy = _int_or_none(element.get("cy"))
        if cx is None or cy is None or cx <= 0 or cy <= 0:
            report.skipped += 1
            continue
        new_cx, new_cy, scale = fit_picture(cx, cy, avail_w_emu, avail_h_emu)
        if scale >= 1.0 or (new_cx == cx and new_cy == cy):
            report.unchanged += 1
            continue
        element.set("cx", str(new_cx))
        element.set("cy", str(new_cy))
        container = element.getparent()
        drawing = container.getparent() if container is not None else None
        if drawing is not None and localname(drawing) == "drawing":
            _sync_drawing_extents(drawing, new_cx, new_cy)
        report.resized += 1
        report.min_scale = min(report.min_scale, scale)
        touched = True
    return touched


def _sync_drawing_extents(drawing: Element, cx: int, cy: int) -> None:
    """同步 DrawingML 的 ``a:xfrm/a:ext``。

    不同步的后果是"程序算出的越界判定与 Word 的渲染结果不一致"，且只有真正打开
    文档时才发现。``a:chExt`` 是源矩形、**不得**改动，因此这里只走 ``pic:spPr``
    这条路径，不做 ``.//a:ext`` 的通配匹配。
    """
    for shape_properties in drawing.iter(qn("pic:spPr")):
        extent = child_of(child_of(shape_properties, "a:xfrm"), "a:ext")
        if extent is None:
            continue
        extent.set("cx", str(cx))
        extent.set("cy", str(cy))


# --------------------------------------------------------------------- 放置
def place_picture(
    pkg: DocxPackage,
    paragraph: Element,
    image: bytes,
    *,
    filename: str,
    avail_w_emu: int,
    avail_h_emu: int,
    owner: str = DOCUMENT_PART,
    cx_emu: int | None = None,
    cy_emu: int | None = None,
    alt_text: str | None = None,
) -> Extent:
    """把图片作为**内联**图形放进段落，返回其 ``wp:extent``。

    浮动（``wp:anchor``）图形暂不在此实现：锚定定位涉及相对定位参数与绕排方式，
    属渲染层的版式决策（TASK-P0-09）。此函数只负责"图确实进去了、确实只缩不放"。

    ``w:drawing`` 必须**包在运行里**（``w:p/w:r/w:drawing``）
    -----------------------------------------------------
    ``w:drawing`` 不是一个可以直接挂在 ``w:p`` 下的元素：OOXML 的 ``CT_P`` 只允许
    运行（``w:r``）作为内容的容器，图形则是运行的 ``EG_RunInnerContent``。直接
    ``paragraph.append(drawing)`` 产出的 ``w:p/w:drawing`` 属于**非法结构**，
    Word 与 LibreOffice 都宽容到照样显示，于是缺陷长期不可见；但它有两个可验证的
    后果：图形拿不到 ``w:rPr``（字符级属性全丢），且任何按"运行里有图形"取图的
    代码（含门禁的图注绑定检查、``python-docx`` 这类库）都取不到它。
    """
    width_emu, height_emu = _resolve_size(image, cx_emu=cx_emu, cy_emu=cy_emu, filename=filename)
    new_cx, new_cy, _scale = fit_picture(width_emu, height_emu, avail_w_emu, avail_h_emu)

    media_path = _unique_media_path(pkg, filename)
    pkg.add_binary_part(media_path, content_type_for_extension(_extension(filename)), image)
    relationship = pkg.ensure_relationship(
        owner, type=IMAGE_REL_TYPE, target=f"media/{media_path.rpartition('/')[2]}"
    )

    drawing = _build_inline_drawing(
        cx=new_cx,
        cy=new_cy,
        rel_id=relationship.rel_id,
        drawing_id=next_drawing_id(pkg),
        name=alt_text or filename,
    )
    run = etree.SubElement(paragraph, qn("w:r"))
    run.append(drawing)
    if owner_part := pkg.find_part(owner):
        owner_part.mark_dirty()
    inline = find_first(drawing, "wp:inline")
    node = inline if inline is not None else drawing
    return Extent(
        part=owner,
        xpath=paragraph.getroottree().getpath(node),
        cx=new_cx,
        cy=new_cy,
        anchor=False,
        paragraph_index=_paragraph_index_of(paragraph),
    )


def _resolve_size(
    image: bytes, *, cx_emu: int | None, cy_emu: int | None, filename: str
) -> tuple[int, int]:
    if cx_emu is not None and cy_emu is not None:
        return cx_emu, cy_emu
    pixel_size = _image_pixel_size(image)
    if pixel_size is None:
        raise EngineError(
            ErrorCode.SCHEMA_INVALID,
            f"无法从图片头读取尺寸：{filename}",
            where={"filename": filename},
            remediation=[
                "在文档条目中显式声明图片宽高（px + dpi）",
                "或改用 PNG / JPEG / GIF 格式",
            ],
        )
    return (
        pixels_to_emu(pixel_size[0], DEFAULT_DPI),
        pixels_to_emu(pixel_size[1], DEFAULT_DPI),
    )


def _build_inline_drawing(*, cx: int, cy: int, rel_id: str, drawing_id: int, name: str) -> Element:
    """按 OOXML 元素顺序构造 ``w:drawing``。

    顺序不能改：``wp:extent`` -> ``wp:effectExtent`` -> ``wp:docPr`` ->
    ``wp:cNvGraphicFramePr`` -> ``a:graphic``。顺序错误的产物 Word 可能能打开
    但图片不显示，属最难定位的一类问题。
    """
    drawing = new_tree("w:drawing")
    inline = etree.SubElement(drawing, qn("wp:inline"))
    inline.set("distT", "0")
    inline.set("distB", "0")
    inline.set("distL", "0")
    inline.set("distR", "0")

    extent = etree.SubElement(inline, qn("wp:extent"))
    extent.set("cx", str(cx))
    extent.set("cy", str(cy))
    effect = etree.SubElement(inline, qn("wp:effectExtent"))
    for attribute_name in ("l", "t", "r", "b"):
        effect.set(attribute_name, "0")

    doc_pr = etree.SubElement(inline, qn("wp:docPr"))
    doc_pr.set("id", str(drawing_id))
    doc_pr.set("name", f"Picture {drawing_id}")
    if name:
        doc_pr.set("descr", name)

    frame_properties = etree.SubElement(inline, qn("wp:cNvGraphicFramePr"))
    etree.SubElement(frame_properties, qn("a:graphicFrameLocks")).set("noChangeAspect", "1")

    graphic_data = etree.SubElement(etree.SubElement(inline, qn("a:graphic")), qn("a:graphicData"))
    graphic_data.set("uri", PICTURE_GRAPHIC_URI)
    picture = etree.SubElement(graphic_data, qn("pic:pic"))
    non_visual = etree.SubElement(picture, qn("pic:nvPicPr"))
    canvas_pr = etree.SubElement(non_visual, qn("pic:cNvPr"))
    canvas_pr.set("id", "0")
    canvas_pr.set("name", name or "image")
    etree.SubElement(non_visual, qn("pic:cNvPicPr"))

    blip_fill = etree.SubElement(picture, qn("pic:blipFill"))
    etree.SubElement(blip_fill, qn("a:blip")).set(qn("r:embed"), rel_id)
    etree.SubElement(etree.SubElement(blip_fill, qn("a:stretch")), qn("a:fillRect"))

    shape_properties = etree.SubElement(picture, qn("pic:spPr"))
    transform = etree.SubElement(shape_properties, qn("a:xfrm"))
    offset = etree.SubElement(transform, qn("a:off"))
    offset.set("x", "0")
    offset.set("y", "0")
    shape_extent = etree.SubElement(transform, qn("a:ext"))
    shape_extent.set("cx", str(cx))
    shape_extent.set("cy", str(cy))
    geometry = etree.SubElement(shape_properties, qn("a:prstGeom"))
    geometry.set("prst", "rect")
    etree.SubElement(geometry, qn("a:avLst"))
    return drawing


def _extension(filename: str) -> str:
    extension = filename.rpartition(".")[2]
    return extension or "png"


def _unique_media_path(pkg: DocxPackage, filename: str) -> str:
    stem, _, extension = filename.rpartition(".")
    if not stem:
        stem, extension = filename, "png"
    candidate = f"word/media/{stem}.{extension}"
    index = 1
    while pkg.has_part(candidate):
        candidate = f"word/media/{stem}_{index}.{extension}"
        index += 1
    return candidate


def next_drawing_id(pkg: DocxPackage) -> int:
    """下一个可用的 ``wp:docPr/@id``。

    ``docPr/@id`` 在同一部件内必须唯一；重复时 Word 会直接报"文档已损坏"并要求
    修复，而不是静默容错。因此不能用固定值，只能扫描现有取最大值。
    """
    highest = 0
    for part in pkg.parts(scope="content"):
        for element in part.read_tree.iter():
            if localname(element) not in {"docPr", "cNvPr"}:
                continue
            value = _int_or_none(element.get("id"))
            if value is not None:
                highest = max(highest, value)
    return highest + 1


def _image_pixel_size(data: bytes) -> tuple[int, int] | None:
    """从图片头部读尺寸，避免为一个尺寸引入 Pillow 依赖。"""
    if len(data) >= 24 and data[:8] == b"\x89PNG\r\n\x1a\n":
        return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
    if len(data) >= 10 and data[:6] in {b"GIF87a", b"GIF89a"}:
        return int.from_bytes(data[6:8], "little"), int.from_bytes(data[8:10], "little")
    if data[:2] == b"\xff\xd8":
        return _jpeg_pixel_size(data)
    return None


def _jpeg_pixel_size(data: bytes) -> tuple[int, int] | None:
    index = 2
    length = len(data)
    while index + 9 < length:
        if data[index] != 0xFF:
            index += 1
            continue
        marker = data[index + 1]
        if marker in {0xD8, 0x01} or 0xD0 <= marker <= 0xD7:
            index += 2
            continue
        segment_length = int.from_bytes(data[index + 2 : index + 4], "big")
        if 0xC0 <= marker <= 0xCF and marker not in {0xC4, 0xC8, 0xCC}:
            height = int.from_bytes(data[index + 5 : index + 7], "big")
            width = int.from_bytes(data[index + 7 : index + 9], "big")
            return width, height
        index += 2 + segment_length
    return None


# --------------------------------------------------------------------- 图注
def bind_caption(
    pkg: DocxPackage,
    picture_paragraph: Element,
    caption_text: str,
    *,
    style_id: str | None = None,
) -> Element:
    """把图注绑定到图片段落，返回承载图注的段落（FR-M3-08）。

    "同一段落组、禁止跨页分离"的实现方式不是把图注塞进图片所在段落（那会让
    图片居中、图注跟随的排版失效），而是：**图片段落加 ``w:keepNext``**，
    并让图注段落紧邻其后。``keepNext`` 是 Word 里"与下一段同页"的唯一可靠手段。
    """
    owner = _owner_part_of(pkg, picture_paragraph)
    properties = ensure_paragraph_properties(picture_paragraph)
    set_on_off(properties, "w:keepNext", True)

    caption = _following_paragraph(picture_paragraph)
    if caption is None:
        caption = etree.Element(qn("w:p"))
        picture_paragraph.addnext(caption)

    if style_id:
        style_element = find_first(ensure_paragraph_properties(caption), "w:pStyle")
        if style_element is None:
            style_element = etree.SubElement(ensure_paragraph_properties(caption), qn("w:pStyle"))
        style_element.set(qn("w:val"), style_id)
    set_paragraph_text(caption, caption_text)
    owner.mark_dirty()
    return caption


def _following_paragraph(paragraph: Element) -> Element | None:
    """紧邻的下一个段落；中间夹着表格 / 书签等节点时返回 ``None``。

    夹着别的节点说明这里没有"图注位置"，继续向后找到的段落会属于下一个内容块，
    绑定到那里就等于把图注挂到了别人的图上。
    """
    sibling = paragraph.getnext()
    if sibling is not None and sibling.tag == qn("w:p"):
        return sibling
    return None


def _owner_part_of(pkg: DocxPackage, element: Element) -> Part:
    """定位元素所属部件。

    找不到即抛错：这说明该元素不属于本包，继续往下写会把改动丢在游离的元素树上
    （``save`` 时静默消失）。
    """
    root = element.getroottree().getroot()
    for part in pkg.parts(scope="content"):
        if part.read_tree is root:
            return part
    raise EngineError(
        ErrorCode.INTERNAL,
        "元素不属于本模板包的任何正文部件，改动不会被保存",
        where={"tag": localname(element)},
        remediation=["确认传入的段落来自 pkg.part(...).tree"],
    )


def caption_text_of(paragraph: Element) -> str:
    """读取图注段落的文本（供 qa 校验编号格式）。"""
    return paragraph_text(paragraph)


__all__ = [
    "DEFAULT_DPI",
    "IMAGE_REL_TYPE",
    "PICTURE_GRAPHIC_URI",
    "Extent",
    "ResizeReport",
    "bind_caption",
    "caption_text_of",
    "next_drawing_id",
    "place_picture",
    "read_extents",
    "resize_pictures",
]
