"""为 OOXML 单测构造**真实** docx 夹具。

为什么必须用真实文件而不是手写 XML 字符串
------------------------------------------
``ooxml`` 的全部风险都来自"跟 Word 的真实产物打交道"：``w:tblStylePr`` 的位置、
``wp:extent`` 与 ``a:ext`` 的双写、目录域字符嵌在条目段落里、rsid 属性遍地都是。
用玩具 XML 测出来的绿，到了真实模板上照样崩。因此这里先用 ``python-docx`` 生成
一份形制正确的文档，再用 lxml 把"只有真实模板才有"的结构补进去：

* ``w:tblStylePr`` 条件格式（表头底纹）—— 跨文档克隆样式时最容易丢的东西
* ``w:rsid*`` 属性 —— 同一内容每次保存都不一样的易变标识
* ``_Toc`` 书签 + PAGEREF 域 —— 目录重建的作用对象
* 越界的 ``wp:extent``（``a:ext`` 同步给到）—— "只缩不放"的判据
* 两份层级签名相同的 ``abstractNum`` —— 编号合并的输入

本模块负责**文档本体**（封面 / 页眉页脚 / 目录 / 图片 / 样式装饰）。NFR-09 的单文件
800 行上限把另外三块拆了出去，各自的边界见其模块文档：

=================================  ==================================================================
:mod:`tests.fixtures.docx_parts`    命名空间常量 + 包部件读写（唯一的 ``zipfile`` 包装）
:mod:`tests.fixtures.docx_numbering` 编号契约（多级编号定义 / 标题绑定 / 项目符号段落）
:mod:`tests.fixtures.docx_perturb`  易变标识扰动与部件改写（"只改了一处"的对照件）
=================================  ==================================================================

三者仍从本模块**转出**，既有调用方无需关心拆分；拆的是文件，不是口径。
"""

from __future__ import annotations

import struct
import zlib
from collections.abc import Sequence
from pathlib import Path

from lxml import etree

# 命名空间 / 部件读写 / 编号契约 / 易变标识扰动**各自只有一份实现**，本模块只是转出，
# 既有调用方（``scripts/make_demo_templates.py`` 等）无需关心拆分。三个子模块的分工见
# 各自的模块文档；这里刻意**保留原有的私有别名拼法**（``_W`` / ``_parse`` …）——改名会
# 带来上百行无意义的改动，反而看不清这一轮真改了什么。
from .docx_numbering import (
    BULLET_NUM_ID,
    NUMBERING_CONTENT_TYPE,
    NUMBERING_REL_TYPE,
    add_bullet_paragraph,
)
from .docx_numbering import bind_heading_numbering as _bind_heading_numbering
from .docx_numbering import numbering_xml as _numbering_xml
from .docx_numbering import register_numbering as _register_numbering
from .docx_numbering import strip_list_numbering as _strip_list_numbering
from .docx_parts import CT_PREFIX as _CT
from .docx_parts import PIC, WP, A, R, W
from .docx_parts import PKG_REL_PREFIX as _PKGREL
from .docx_parts import R_PREFIX as _R
from .docx_parts import W_PREFIX as _W
from .docx_parts import parse as _parse
from .docx_parts import read_parts as _read_parts
from .docx_parts import serialize as _serialize
from .docx_parts import write_parts as _write_parts
from .docx_perturb import rewrite_part, shuffle_volatile_identifiers

#: 越界图片：9,000,000 EMU 宽，远超 A4 版心（约 5,943,600 EMU）。
OVERSIZED_CX = 9_000_000
OVERSIZED_CY = 3_000_000

#: 版心内小图：不触发缩放，用于验证"只缩不放"不会误伤。
FITTING_CX = 1_000_000
FITTING_CY = 800_000


def png_bytes(width: int, height: int) -> bytes:
    """生成一张最小合法 PNG（只用于让图片头解析有真东西可读）。"""

    def chunk(tag: bytes, payload: bytes) -> bytes:
        body = tag + payload
        return (
            struct.pack(">I", len(payload))
            + body
            + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)
        )

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    scanline = b"\x00" + b"\x40\x80\xc0" * width
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(scanline * height))
        + chunk(b"IEND", b"")
    )


def build_sample_docx(
    path: Path,
    *,
    with_table: bool = True,
    with_picture: bool = True,
    with_toc: bool = True,
    with_numbering: bool = True,
    with_heading_bindings: bool = True,
) -> Path:
    """生成带真实结构的样例模板，返回写入的路径。"""
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH

    document = Document()
    heading = document.add_heading("概述", level=1)
    document.add_paragraph("这是正文段落，用于验证直设格式检测不会误报。")

    direct = document.add_paragraph("带直设格式的段落")
    direct.paragraph_format.alignment = WD_ALIGN_PARAGRAPH.CENTER
    direct.runs[0].bold = True

    second = document.add_heading("范围", level=1)
    assert second.style is not None

    if with_table:
        table = document.add_table(rows=2, cols=3)
        table.style = "Table Grid"
        table.cell(0, 0).text = "项目"
        table.cell(0, 1).text = "说明"
        table.cell(0, 2).text = "备注"
        table.cell(1, 0).text = "A"
        table.cell(1, 1).text = "B"
        table.cell(1, 2).text = "C"
    if with_picture:
        document.add_paragraph("图 1-1 越界图片占位")

    document.add_heading("附录", level=2)
    assert heading is not None
    document.save(str(path))

    parts = _read_parts(path)
    document_root = _parse(parts["word/document.xml"])
    styles_root = _parse(parts["word/styles.xml"])

    _strip_rsids(document_root)
    _add_rsids(document_root)
    _add_bookmarks(document_root)
    if with_picture:
        _inject_pictures(document_root)
    if with_toc:
        _inject_toc(document_root)
        _add_toc_styles(styles_root)
    _decorate_table_style(styles_root)
    _decorate_normal_style(styles_root)

    if with_numbering:
        parts["word/numbering.xml"] = _numbering_xml()
        _register_numbering(parts, document_root)
        if with_heading_bindings:
            _bind_heading_numbering(styles_root)
        # 默认模板的列表样式自带 numPr，会让"被引用的 numId"集合变得不确定；
        # 清掉它们，让编号合并测试只面对夹具自己声明的引用关系。
        _strip_list_numbering(styles_root)

    if "word/settings.xml" not in parts:
        parts["word/settings.xml"] = _settings_xml()

    parts["word/document.xml"] = _serialize(document_root)
    parts["word/styles.xml"] = _serialize(styles_root)
    _write_parts(path, parts)
    return path


#: 页眉部件的内容类型
HEADER_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.header+xml"
HEADER_REL_TYPE = f"{R}/header"

#: 页脚部件的内容类型（示例模板要演示"页码"，故必须有页脚这条路径）
FOOTER_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.footer+xml"
FOOTER_REL_TYPE = f"{R}/footer"


def build_template_docx(path: Path) -> Path:
    """构造一份"像真模板"的 docx，供 ``registry`` 入库解析测试使用。

    与 :func:`build_sample_docx` 的差别：那份面向 ``ooxml`` 底层原语，这份面向
    **入库流水线**，因此刻意包含流水线各阶段真正要处理的结构：

    * **封面独占一节**（段落内的 ``pPr/sectPr``）—— 封面块定位的判据；
      单节文档没有封面，这正是"不能把正文当封面裁掉"的那条边界。
    * **修订记录表**（表头含多个识别词，且排在**第一个标题之前**）—— 版块定位；
      排在前置版块里是刻意的，见下方建表处的说明；
    * **页眉占位**（``此处填写``）—— 占位符清单必须扫到页眉，它每页都重复出现；
    * **多级标题**—— 章节骨架与槽位推导。

    封面上的文件编号与版本号是刻意留成"能识别出来"的形式（``V2.1``），
    以便验证身份识别走"从文档里读"这条高置信路径。
    """
    from docx import Document
    from docx.enum.section import WD_SECTION

    document = Document()
    document.add_paragraph("XX 项目说明书")
    document.add_paragraph("文件编号：TF-001")
    document.add_paragraph("版本：V2.1")
    document.add_paragraph("此处填写编制单位")

    # 封面到这里结束：``add_section`` 会把当前 sectPr 挪进一个段落的 pPr 里，
    # 于是正文里出现"段落内 sectPr"，正是封面块定位要找的东西。
    document.add_section(WD_SECTION.NEW_PAGE)

    # 修订记录表必须落在**前置版块**里，即**第一个骨架标题之前**
    # ------------------------------------------------
    # 示例内容区的判据是"第一个骨架标题样式段落 → 尾节属性之前"。修订记录是受保护
    # 版块（``kind=revision``），一旦它落在第一个标题**之后**，就会被判进示例内容区，
    # 注入时随示例内容一起被裁掉——"修订记录"这块体例凭空消失，而产物表面上完全正常。
    # 故这里刻意把它排在第一个标题之前：真实模板也是这个次序（封面 → 修订记录 →
    # 目录 → 正文），夹具必须与真实模板同构，否则它测的是一条不存在的链路。
    revision = document.add_table(rows=2, cols=5)
    revision.style = "Table Grid"
    for column, title in enumerate(("序号", "版本", "修改内容", "修改人", "日期")):
        revision.cell(0, column).text = title
    for column, value in enumerate(("1", "V2.1", "首次发布", "张三", "2026-01-01")):
        revision.cell(1, column).text = value

    document.add_heading("概述", level=1)
    document.add_paragraph("此处填写项目背景。")

    document.add_heading("范围", level=1)

    document.add_heading("附录", level=2)
    document.save(str(path))

    parts = _read_parts(path)
    document_root = _parse(parts["word/document.xml"])
    styles_root = _parse(parts["word/styles.xml"])

    _strip_rsids(document_root)
    _add_rsids(document_root)
    _add_bookmarks(document_root)
    _inject_toc(document_root)
    _add_toc_styles(styles_root)
    _decorate_normal_style(styles_root)
    _attach_header(parts, document_root)

    parts["word/document.xml"] = _serialize(document_root)
    parts["word/styles.xml"] = _serialize(styles_root)
    _write_parts(path, parts)
    return path


def _attach_header(parts: dict[str, bytes], document_root: etree._Element) -> None:
    """挂一个默认页眉，内含占位文字（见 :func:`attach_header_footer` 的说明）。"""
    attach_header_footer(parts, document_root, header_text="XX 项目说明书　此处填写项目编号")


def attach_header_footer(
    parts: dict[str, bytes],
    document_root: etree._Element,
    *,
    header_text: str,
    footer_text: str | None = None,
    styles_root: etree._Element | None = None,
) -> None:
    """挂页眉（可含占位文字）与可选页脚（含 ``PAGE`` 域）。

    页眉是占位符最容易被漏掉的地方：它不在正文里，却会在**每一页**重复出现，
    漏掉就等于让编辑痕迹印满整份文档。

    页脚走 ``w:fldSimple``（``instr=" PAGE "``）而不是自己拼 fldChar 三件套：
    页码是"域"，渲染层重建目录时按域字符扫描段落，多一套自造的域写法会让
    "哪些段落带域"这件事出现第二个口径。

    ``styles_root`` 给出时，会顺手补一个 ``Footer`` 段落样式（居中）。**不靠
    ``w:pPr/w:jc`` 直设居中**：直设格式会被 ``FR-M5-01`` 判成"未通过样式的属性直设"，
    而那条判定是对的——页脚居中在真实模板里本来就是一个段落样式的事。
    """
    _register_header(parts, document_root, header_text)
    if footer_text is None:
        return
    if styles_root is not None:
        ensure_footer_style(styles_root)
    _register_footer(parts, document_root, footer_text)


def ensure_footer_style(styles_root: etree._Element) -> None:
    """确保 ``styles.xml`` 里有 ``Footer`` 段落样式（居中）；已有则不动。"""
    for style in styles_root.findall(f"{_W}style"):
        if style.get(f"{_W}styleId") == "Footer":
            return
    style = etree.SubElement(styles_root, f"{_W}style")
    style.set(f"{_W}type", "paragraph")
    style.set(f"{_W}styleId", "Footer")
    name = etree.SubElement(style, f"{_W}name")
    name.set(f"{_W}val", "footer")
    properties = etree.SubElement(style, f"{_W}pPr")
    align = etree.SubElement(properties, f"{_W}jc")
    align.set(f"{_W}val", "center")


def _register_header(parts: dict[str, bytes], document_root: etree._Element, text: str) -> None:
    parts["word/header1.xml"] = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<w:hdr xmlns:w="{W}" xmlns:r="{R}">'
        f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>"
        "</w:hdr>"
    ).encode()

    content_types = _parse(parts["[Content_Types].xml"])
    override = etree.SubElement(content_types, f"{_CT}Override")
    override.set("PartName", "/word/header1.xml")
    override.set("ContentType", HEADER_CONTENT_TYPE)
    parts["[Content_Types].xml"] = _serialize(content_types)

    rels_path = "word/_rels/document.xml.rels"
    rels = _parse(parts[rels_path])
    relationship = etree.SubElement(rels, f"{_PKGREL}Relationship")
    relationship.set("Id", "rIdHeader1")
    relationship.set("Type", HEADER_REL_TYPE)
    relationship.set("Target", "header1.xml")
    parts[rels_path] = _serialize(rels)

    body = document_root.find(f"{_W}body")
    if body is None:
        return
    section = body.find(f"{_W}sectPr")
    if section is None:
        return
    reference = etree.Element(f"{_W}headerReference")
    reference.set(f"{_W}type", "default")
    reference.set(f"{_R}id", "rIdHeader1")
    # 顺序是规范要求：headerReference 必须排在 pgSz / pgMar 之前。
    section.insert(0, reference)


def _register_footer(parts: dict[str, bytes], document_root: etree._Element, text: str) -> None:
    parts["word/footer1.xml"] = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<w:ftr xmlns:w="{W}" xmlns:r="{R}">'
        '<w:p><w:pPr><w:pStyle w:val="Footer"/></w:pPr>'
        f'<w:r><w:t xml:space="preserve">{text}</w:t></w:r>'
        '<w:fldSimple w:instr=" PAGE "><w:r><w:t>1</w:t></w:r></w:fldSimple>'
        "</w:p>"
        "</w:ftr>"
    ).encode()

    content_types = _parse(parts["[Content_Types].xml"])
    override = etree.SubElement(content_types, f"{_CT}Override")
    override.set("PartName", "/word/footer1.xml")
    override.set("ContentType", FOOTER_CONTENT_TYPE)
    parts["[Content_Types].xml"] = _serialize(content_types)

    rels_path = "word/_rels/document.xml.rels"
    rels = _parse(parts[rels_path])
    relationship = etree.SubElement(rels, f"{_PKGREL}Relationship")
    relationship.set("Id", "rIdFooter1")
    relationship.set("Type", FOOTER_REL_TYPE)
    relationship.set("Target", "footer1.xml")
    parts[rels_path] = _serialize(rels)

    body = document_root.find(f"{_W}body")
    if body is None:
        return
    section = body.find(f"{_W}sectPr")
    if section is None:
        return
    reference = etree.Element(f"{_W}footerReference")
    reference.set(f"{_W}type", "default")
    reference.set(f"{_R}id", "rIdFooter1")
    # 与 headerReference 同理：页脚引用也必须排在 pgSz / pgMar 之前。
    existing = section.findall(f"{_W}headerReference")
    section.insert(len(existing), reference)


# --------------------------------------------------------------------- 内部
def _strip_rsids(root: etree._Element) -> None:
    for element in root.iter():
        for name in [key for key in element.attrib if key.startswith(_W) and "rsid" in key]:
            del element.attrib[name]


def _add_rsids(root: etree._Element) -> None:
    """补上 rsid 属性：真实 Word 产物里遍地都是，且每次保存都变。"""
    body = root.find(f"{_W}body")
    if body is None:
        return
    for index, paragraph in enumerate(body.iter(f"{_W}p")):
        paragraph.set(f"{_W}rsidR", f"00AB{index:04d}")
        paragraph.set(f"{_W}rsidRPr", f"00AB{index:04d}F")


def _add_bookmarks(root: etree._Element) -> None:
    """给每个标题段落挂一个 ``_Toc`` 书签（正文侧定位目录项用）。"""
    body = root.find(f"{_W}body")
    if body is None:
        return
    counter = 1
    for paragraph in body.iter(f"{_W}p"):
        style = paragraph.find(f"{_W}pPr/{_W}pStyle")
        if style is None or not (style.get(f"{_W}val") or "").startswith("Heading"):
            continue
        name = f"_Toc{counter:08d}"
        start = etree.Element(f"{_W}bookmarkStart")
        start.set(f"{_W}id", str(counter))
        start.set(f"{_W}name", name)
        end = etree.Element(f"{_W}bookmarkEnd")
        end.set(f"{_W}id", str(counter))
        properties = paragraph.find(f"{_W}pPr")
        paragraph.insert(1 if properties is not None else 0, start)
        paragraph.append(end)
        counter += 1


def _inject_pictures(root: etree._Element) -> None:
    body = root.find(f"{_W}body")
    if body is None:
        return
    for cx, cy, name in (
        (OVERSIZED_CX, OVERSIZED_CY, "oversized.png"),
        (FITTING_CX, FITTING_CY, "fitting.png"),
    ):
        body.append(_drawing_paragraph(cx, cy, name))


def _drawing_paragraph(cx: int, cy: int, name: str) -> etree._Element:
    """构造一段内联图形；``wp:extent`` 与 ``a:ext`` 同值，模拟 Word 的真实写法。"""
    paragraph = etree.Element(f"{_W}p")
    run = etree.SubElement(paragraph, f"{_W}r")
    drawing = etree.SubElement(run, f"{_W}drawing")
    inline = etree.SubElement(drawing, f"{{{WP}}}inline")
    for attribute in ("distT", "distB", "distL", "distR"):
        inline.set(attribute, "0")

    extent = etree.SubElement(inline, f"{{{WP}}}extent")
    extent.set("cx", str(cx))
    extent.set("cy", str(cy))
    effect = etree.SubElement(inline, f"{{{WP}}}effectExtent")
    for attribute in ("l", "t", "r", "b"):
        effect.set(attribute, "0")

    doc_pr = etree.SubElement(inline, f"{{{WP}}}docPr")
    doc_pr.set("id", str(abs(hash(name)) % 1000))
    doc_pr.set("name", name)

    frame = etree.SubElement(inline, f"{{{WP}}}cNvGraphicFramePr")
    etree.SubElement(frame, f"{{{A}}}graphicFrameLocks").set("noChangeAspect", "1")

    graphic = etree.SubElement(inline, f"{{{A}}}graphic")
    data = etree.SubElement(graphic, f"{{{A}}}graphicData")
    data.set("uri", f"{PIC}")
    picture = etree.SubElement(data, f"{{{PIC}}}pic")
    non_visual = etree.SubElement(picture, f"{{{PIC}}}nvPicPr")
    canvas = etree.SubElement(non_visual, f"{{{PIC}}}cNvPr")
    canvas.set("id", "0")
    canvas.set("name", name)
    etree.SubElement(non_visual, f"{{{PIC}}}cNvPicPr")

    blip_fill = etree.SubElement(picture, f"{{{PIC}}}blipFill")
    etree.SubElement(blip_fill, f"{{{A}}}blip").set(f"{_R}embed", "rIdUnknown")
    etree.SubElement(etree.SubElement(blip_fill, f"{{{A}}}stretch"), f"{{{A}}}fillRect")

    shape = etree.SubElement(picture, f"{{{PIC}}}spPr")
    transform = etree.SubElement(shape, f"{{{A}}}xfrm")
    offset = etree.SubElement(transform, f"{{{A}}}off")
    offset.set("x", "0")
    offset.set("y", "0")
    shape_extent = etree.SubElement(transform, f"{{{A}}}ext")
    shape_extent.set("cx", str(cx))
    shape_extent.set("cy", str(cy))
    geometry = etree.SubElement(shape, f"{{{A}}}prstGeom")
    geometry.set("prst", "rect")
    etree.SubElement(geometry, f"{{{A}}}avLst")
    return paragraph


def _toc_entry(
    style_id: str,
    title: str,
    anchor: str,
    *,
    toc_begin: bool = False,
    toc_end: bool = False,
) -> etree._Element:
    """构造一个目录条目：样式 + 制表位 + PAGEREF 域，外加可选的 TOC 域字符。

    形态严格对齐 Word 的真实产物——**每个条目自带 PAGEREF 域**，而包裹整块的
    TOC 域 ``begin`` 在第一条、``end`` 在最后一条。夹具采用这个形态，才能验证
    "重建目录时不能把带域字符的段落整个删掉"。

    **TOC 域的开合必须落在段落层、不能落在 ``w:hyperlink`` 里**：域字符一旦跨过
    ``w:hyperlink`` 的边界（``begin`` 在第一条的超链接内、``end`` 在另一条的超链接内），
    Word 会把整份文件判成"文件已经损坏"而拒绝打开——lxml / python-docx 全程不报错，
    本仓库自己的一切检查也都是绿的。PAGEREF 域则**整对都在同一条超链接内**，它是合法的。
    """
    paragraph = etree.Element(f"{_W}p")
    properties = etree.SubElement(paragraph, f"{_W}pPr")
    style = etree.SubElement(properties, f"{_W}pStyle")
    style.set(f"{_W}val", style_id)

    if toc_begin:
        _field_run(paragraph, "begin")
        _instruction_run(paragraph, ' TOC \\o "1-3" \\h \\z \\u ')
        _field_run(paragraph, "separate")

    # 制表位刻意不放段落的 pPr 里：真实模板让它来自 TOC 样式，
    # 直接设在段落上属于"直设格式"，会被 FR-M5-01 规则正确地报出来。
    hyperlink = etree.SubElement(paragraph, f"{_W}hyperlink")
    hyperlink.set(f"{_W}anchor", anchor)
    etree.SubElement(etree.SubElement(hyperlink, f"{_W}r"), f"{_W}t").text = title
    etree.SubElement(etree.SubElement(hyperlink, f"{_W}r"), f"{_W}tab")

    _field_run(hyperlink, "begin")
    _instruction_run(hyperlink, f" PAGEREF {anchor} \\h ")
    _field_run(hyperlink, "separate")
    etree.SubElement(etree.SubElement(hyperlink, f"{_W}r"), f"{_W}t").text = "1"
    _field_run(hyperlink, "end")

    if toc_end:
        _field_run(paragraph, "end")
    return paragraph


def _field_run(parent: etree._Element, kind: str) -> None:
    fld = etree.SubElement(etree.SubElement(parent, f"{_W}r"), f"{_W}fldChar")
    fld.set(f"{_W}fldCharType", kind)


def _instruction_run(parent: etree._Element, text: str) -> None:
    instruction = etree.SubElement(etree.SubElement(parent, f"{_W}r"), f"{_W}instrText")
    instruction.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
    instruction.text = text


def inject_toc(root: etree._Element, entries: Sequence[tuple[str, str, str]] | None = None) -> None:
    """把目录控件插到正文最前面，形制与 Word 生成的目录一致。

    ``entries`` 为 ``(TOC 样式, 条目文本, 书签名)`` 三元组；不给时用夹具自己的两行
    样例（``概述`` / ``范围``）。首条带 TOC 域的 ``begin``、末条带 ``end``——这个
    "域字符夹住整块、每条自带 PAGEREF" 的形态就是渲染层重建目录要面对的真实结构。
    """
    body = root.find(f"{_W}body")
    if body is None:
        return
    chosen = list(entries or (("TOC1", "概述", "_Toc00000001"), ("TOC2", "范围", "_Toc00000002")))
    sdt = etree.Element(f"{_W}sdt")
    sdt_properties = etree.SubElement(sdt, f"{_W}sdtPr")
    doc_part = etree.SubElement(sdt_properties, f"{_W}docPartObj")
    gallery = etree.SubElement(doc_part, f"{_W}docPartGallery")
    gallery.set(f"{_W}val", "Table of Contents")
    etree.SubElement(doc_part, f"{_W}docPartUnique")

    content = etree.SubElement(sdt, f"{_W}sdtContent")
    last = len(chosen) - 1
    for index, (style_id, title, anchor) in enumerate(chosen):
        content.append(
            _toc_entry(
                style_id,
                title,
                anchor,
                toc_begin=index == 0,
                toc_end=index == last,
            )
        )
    body.insert(0, sdt)


def _inject_toc(root: etree._Element) -> None:
    """夹具内部的旧入口（保持既有调用点不变）。"""
    inject_toc(root)


def _add_toc_styles(styles_root: etree._Element) -> None:
    """补两个目录条目样式（真实模板里由 Word 自动生成，默认模板没有）。

    补上它们是为了让"被引用但未定义"的检查有一个干净基线——否则 ``TOC1`` / ``TOC2``
    会一直是"缺失引用"，掩盖掉真正需要报出来的悬挂引用。
    """
    for style_id, name, left in (("TOC1", "toc 1", 0), ("TOC2", "toc 2", 220)):
        style = etree.SubElement(styles_root, f"{_W}style")
        style.set(f"{_W}type", "paragraph")
        style.set(f"{_W}styleId", style_id)
        style_name = etree.SubElement(style, f"{_W}name")
        style_name.set(f"{_W}val", name)
        properties = etree.SubElement(style, f"{_W}pPr")
        tabs = etree.SubElement(properties, f"{_W}tabs")
        tab = etree.SubElement(tabs, f"{_W}tab")
        tab.set(f"{_W}val", "right")
        tab.set(f"{_W}leader", "dot")
        tab.set(f"{_W}pos", "8306")
        if left:
            indent = etree.SubElement(properties, f"{_W}ind")
            indent.set(f"{_W}left", str(left))


def _decorate_table_style(styles_root: etree._Element) -> None:
    """给 ``Table Grid`` 补条件格式，并新增一个可作重映射目标的自定义表样式。"""
    for style in styles_root.findall(f"{_W}style"):
        if style.get(f"{_W}styleId") != "TableGrid":
            continue
        conditional = etree.Element(f"{_W}tblStylePr")
        conditional.set(f"{_W}type", "firstRow")
        run_properties = etree.SubElement(conditional, f"{_W}rPr")
        etree.SubElement(run_properties, f"{_W}b")
        shading = etree.SubElement(run_properties, f"{_W}shd")
        shading.set(f"{_W}val", "clear")
        shading.set(f"{_W}fill", "D9E2F3")
        table_properties = style.find(f"{_W}tblPr")
        if table_properties is not None:
            table_properties.addnext(conditional)
        else:
            style.append(conditional)

    custom = etree.SubElement(styles_root, f"{_W}style")
    custom.set(f"{_W}type", "table")
    custom.set(f"{_W}styleId", "TblSpec")
    name = etree.SubElement(custom, f"{_W}name")
    name.set(f"{_W}val", "规范表格")
    based_on = etree.SubElement(custom, f"{_W}basedOn")
    based_on.set(f"{_W}val", "TableGrid")


def _decorate_normal_style(styles_root: etree._Element) -> None:
    """给 Normal 样式补中英文字体与字号，让字体解析有真东西可断言。

    ``w:eastAsia`` 单独建模不是形式主义：缺省时用 ``w:ascii`` 顶替会让中文标题
    在 Word 里换字体，"看起来样式变了"却查不出原因。
    """
    for style in styles_root.findall(f"{_W}style"):
        if style.get(f"{_W}styleId") != "Normal":
            continue
        for existing in style.findall(f"{_W}rPr"):
            style.remove(existing)
        run_properties = etree.Element(f"{_W}rPr")
        fonts = etree.SubElement(run_properties, f"{_W}rFonts")
        fonts.set(f"{_W}ascii", "Calibri")
        fonts.set(f"{_W}eastAsia", "等线")
        fonts.set(f"{_W}hAnsi", "Calibri")
        size = etree.SubElement(run_properties, f"{_W}sz")
        size.set(f"{_W}val", "21")
        style.append(run_properties)


def _settings_xml() -> bytes:
    header = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    return (header + f'<w:settings xmlns:w="{W}"><w:zoom w:percent="100"/></w:settings>').encode(
        "utf-8"
    )


__all__ = [
    "BULLET_NUM_ID",
    "FITTING_CX",
    "FITTING_CY",
    "FOOTER_CONTENT_TYPE",
    "FOOTER_REL_TYPE",
    "NUMBERING_CONTENT_TYPE",
    "NUMBERING_REL_TYPE",
    "OVERSIZED_CX",
    "OVERSIZED_CY",
    "add_bullet_paragraph",
    "attach_header_footer",
    "build_sample_docx",
    "build_template_docx",
    "ensure_footer_style",
    "inject_toc",
    "png_bytes",
    "rewrite_part",
    "shuffle_volatile_identifiers",
]
