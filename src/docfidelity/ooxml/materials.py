"""只读新 DOCX 的正文语义与嵌入图片；不分析任何模板。"""

from __future__ import annotations

import hashlib
import posixpath
import stat
from io import BytesIO
from pathlib import PurePosixPath
from zipfile import BadZipFile, ZipFile

from lxml import etree
from pydantic import ValidationError

from docfidelity.contracts.errors import EngineError, ErrorCode
from docfidelity.contracts.materials import MaterialBlock

from .doc import Element, attr, localname, qn

MAX_ZIP_MEMBERS = 4096
MAX_EXPANDED_BYTES = 64 * 1024 * 1024
MAX_MEMBER_BYTES = 16 * 1024 * 1024


def _members(payload: bytes) -> dict[str, bytes]:
    members: dict[str, bytes] = {}
    total = 0
    with ZipFile(BytesIO(payload)) as archive:
        infos = archive.infolist()
        if len(infos) > MAX_ZIP_MEMBERS:
            raise EngineError(ErrorCode.UPLOAD_SIZE, "材料 ZIP 成员数量超限")
        seen: set[str] = set()
        for info in infos:
            name = info.orig_filename.rstrip("/") if info.is_dir() else info.orig_filename
            path = PurePosixPath(name)
            if (
                not name
                or "\\" in name
                or ":" in name
                or "\x00" in name
                or path.is_absolute()
                or any(part in {"", ".", ".."} for part in name.split("/"))
                or stat.S_ISLNK(info.external_attr >> 16)
            ):
                raise EngineError(ErrorCode.PATH_ESCAPE, "材料 ZIP 包含不安全路径")
            if name.casefold() in seen:
                raise EngineError(ErrorCode.SOURCE_LOAD_FAILED, "材料 ZIP 包含重复成员")
            seen.add(name.casefold())
            if info.flag_bits & 1 or name.casefold().endswith(("vbaproject.bin", "vbadata.xml")):
                raise EngineError(ErrorCode.UPLOAD_TYPE, "材料 ZIP 不允许加密或宏")
            if (
                info.file_size > MAX_MEMBER_BYTES
                or info.file_size > max(info.compress_size, 1) * 1000
            ):
                raise EngineError(ErrorCode.UPLOAD_SIZE, "材料 ZIP 成员或压缩比超限")
            chunks: list[bytes] = []
            size = 0
            with archive.open(info) as stream:
                while chunk := stream.read(1024 * 1024):
                    size += len(chunk)
                    total += len(chunk)
                    if size > MAX_MEMBER_BYTES or total > MAX_EXPANDED_BYTES:
                        raise EngineError(ErrorCode.UPLOAD_SIZE, "材料 ZIP 实际展开量超限")
                    chunks.append(chunk)
            if not info.is_dir():
                members[name] = b"".join(chunks)
    if not {"[Content_Types].xml", "word/document.xml", "_rels/.rels"}.issubset(members):
        raise EngineError(ErrorCode.SOURCE_LOAD_FAILED, "材料不是完整 DOCX")
    return members


def _xml(payload: bytes) -> Element:
    parser = etree.XMLParser(resolve_entities=False, load_dtd=False, no_network=True)
    root = etree.fromstring(payload, parser=parser)
    if root.getroottree().docinfo.doctype:
        raise EngineError(ErrorCode.SOURCE_LOAD_FAILED, "材料 XML 不允许 DTD")
    return root


def _text(root: Element) -> str:
    parts: list[str] = []
    for node in root.iter():
        if node.tag in {qn("w:t"), qn("w:delText"), qn("w:instrText")} or localname(node) == "t":
            parts.append(node.text or "")
        elif node.tag == qn("w:tab"):
            parts.append("\t")
        elif node.tag in {qn("w:br"), qn("w:cr")}:
            parts.append("\n")
    return "".join(parts)


def _paragraph_kind(
    paragraph: Element,
    styles: dict[str, Element],
    numbering: Element | None,
    pending: list[str],
    locator: str,
) -> tuple[str, int | None]:
    properties = paragraph.find(qn("w:pPr"))
    style = attr(
        paragraph.find("w:pPr/w:pStyle", namespaces={"w": qn("w:p").split("}")[0][1:]}), "w:val"
    )
    outline = properties.find(qn("w:outlineLvl")) if properties is not None else None
    number = properties.find(qn("w:numPr")) if properties is not None else None
    number_id = attr(number.find(qn("w:numId")), "w:val") if number is not None else None
    level = attr(number.find(qn("w:ilvl")), "w:val") if number is not None else None
    seen: set[str] = set()
    while style:
        if style in seen:
            pending.append(f"{locator}：段落样式继承循环，已使用最近可用属性，待确认")
            break
        seen.add(style)
        inherited = styles.get(style)
        if inherited is None:
            pending.append(f"{locator}：段落基础样式缺失，待确认")
            break
        inherited_properties = inherited.find(qn("w:pPr"))
        if inherited_properties is not None:
            if outline is None:
                outline = inherited_properties.find(qn("w:outlineLvl"))
            inherited_number = inherited_properties.find(qn("w:numPr"))
            if inherited_number is not None:
                if number_id is None:
                    number_id = attr(inherited_number.find(qn("w:numId")), "w:val")
                if level is None:
                    level = attr(inherited_number.find(qn("w:ilvl")), "w:val")
        style = attr(inherited.find(qn("w:basedOn")), "w:val")
    if outline is not None:
        value = int(attr(outline, "w:val") or "0")
        if 0 <= value <= 8:
            return "heading", value + 1
    if number_id not in {None, "0"}:
        kind = "numbered"
        level = level or "0"
        if numbering is not None:
            for concrete in numbering.findall(qn("w:num")):
                if attr(concrete, "w:numId") != number_id:
                    continue
                abstract_id = attr(concrete.find(qn("w:abstractNumId")), "w:val")
                for abstract in numbering.findall(qn("w:abstractNum")):
                    if attr(abstract, "w:abstractNumId") == abstract_id:
                        for item in abstract.findall(qn("w:lvl")):
                            if (
                                attr(item, "w:ilvl") == level
                                and attr(item.find(qn("w:numFmt")), "w:val") == "bullet"
                            ):
                                kind = "bullets"
        return kind, None
    return "para", None


def _extract(members: dict[str, bytes]) -> tuple[list[MaterialBlock], list[str], dict[str, bytes]]:
    root = _xml(members["word/document.xml"])
    content_types = _xml(members["[Content_Types].xml"])
    if any(
        "macroenabled" in node.get("ContentType", "").lower()
        or "vbaproject" in node.get("ContentType", "").lower()
        for node in content_types
    ):
        raise EngineError(ErrorCode.UPLOAD_TYPE, "材料 DOCX 含宏类型")
    body = root.find(qn("w:body"))
    if body is None:
        raise EngineError(ErrorCode.SOURCE_LOAD_FAILED, "材料 DOCX 缺少正文")
    styles_root = _xml(members["word/styles.xml"]) if "word/styles.xml" in members else None
    styles = (
        {attr(node, "w:styleId") or "": node for node in styles_root}
        if styles_root is not None
        else {}
    )
    numbering = _xml(members["word/numbering.xml"]) if "word/numbering.xml" in members else None
    rels_root = (
        _xml(members["word/_rels/document.xml.rels"])
        if "word/_rels/document.xml.rels" in members
        else None
    )
    relations = {node.get("Id", ""): node for node in rels_root} if rels_root is not None else {}
    blocks: list[MaterialBlock] = []
    pending: list[str] = []
    assets: dict[str, bytes] = {}

    def append(kind: str, locator: str, **values: object) -> None:
        if len(blocks) >= 2000:
            raise EngineError(ErrorCode.UPLOAD_SIZE, "材料块数量超限")
        blocks.append(
            MaterialBlock.model_validate(
                dict(block_id=f"b{len(blocks) + 1:04d}", kind=kind, locator=locator, **values)
            )
        )

    def figures(node: Element, locator: str) -> None:
        for image in node.iter(qn("a:blip")):
            relationship_id = attr(image, "r:embed") or attr(image, "r:link") or ""
            relation = relations.get(relationship_id)
            if relation is None:
                pending.append(f"{locator}：图片关系缺失，待确认")
                continue
            if relation.get("TargetMode", "").casefold() == "external":
                pending.append(f"{locator}：外部图片未读取，待确认")
                continue
            target = relation.get("Target", "")
            if "\\" in target or ":" in target or target.startswith("/"):
                pending.append(f"{locator}：图片关系路径不安全，待确认")
                continue
            path = posixpath.normpath(posixpath.join("word", target))
            data = members.get(path)
            extension = PurePosixPath(path).suffix.lower()
            if not path.startswith("word/media/") or data is None:
                pending.append(f"{locator}：图片资产缺失或位置不支持，待确认")
                continue
            if extension not in {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tif", ".tiff", ".webp"}:
                pending.append(f"{locator}：图片格式 {extension} 不支持，待确认")
                continue
            key = f"images/{hashlib.sha256(data).hexdigest()}{extension}"
            assets[key] = data
            append("figure", locator, image_ref=key)

    def read(node: Element, locator: str) -> None:
        for descendant in node.iter():
            if localname(descendant) in {
                "object",
                "pict",
                "txbxContent",
                "sdt",
                "altChunk",
                "del",
                "ins",
                "fldChar",
                "footnoteReference",
                "endnoteReference",
                "oMath",
                "oMathPara",
            }:
                pending.append(
                    f"{locator}：{localname(descendant)} 对象需人工确认，文字已尽可能保留"
                )
            if (
                descendant.tag == qn("w:drawing")
                and next(descendant.iter(qn("a:blip")), None) is None
            ):
                pending.append(f"{locator}：drawing 复合图形不支持，待确认")
        if node.tag == qn("w:p"):
            text = _text(node)
            if text.strip():
                kind, level = _paragraph_kind(node, styles, numbering, pending, locator)
                if kind in {"bullets", "numbered"}:
                    append(kind, locator, items=[text])
                else:
                    append(
                        kind, locator, text=text, **({"level": level} if level is not None else {})
                    )
            figures(node, locator)
        elif node.tag == qn("w:tbl"):
            rows = [
                [
                    "\n".join(_text(p) for p in cell.iter(qn("w:p")))
                    for cell in row.findall(qn("w:tc"))
                ]
                for row in node.findall(qn("w:tr"))
            ]
            if rows and rows[0]:
                width = max(len(row) for row in rows)
                if any(len(row) != width for row in rows) or any(
                    localname(item) in {"gridSpan", "vMerge", "tbl"}
                    for item in list(node.iter())[1:]
                ):
                    pending.append(f"{locator}：复杂表格按文字网格保留，待确认")
                append(
                    "table",
                    locator,
                    columns=rows[0] + [""] * (width - len(rows[0])),
                    rows=[row + [""] * (width - len(row)) for row in rows[1:]],
                )
            figures(node, locator)
        elif node.tag != qn("w:sectPr"):
            pending.append(f"{locator}：{localname(node)} 正文容器按文字保留，待确认")
            for index, child in enumerate(node, 1):
                if child.tag in {qn("w:p"), qn("w:tbl"), qn("w:sdtContent")}:
                    read(child, f"{locator}/{index}")
            if (
                not any(child.tag in {qn("w:p"), qn("w:tbl"), qn("w:sdtContent")} for child in node)
                and _text(node).strip()
            ):
                append("para", locator, text=_text(node))
                figures(node, locator)

    for index, node in enumerate(body, 1):
        read(node, f"word/document.xml/body/{index}")
    for path in members:
        if path.startswith(
            ("word/header", "word/footer", "word/footnotes", "word/endnotes")
        ) and path.endswith(".xml"):
            text = _text(_xml(members[path]))
            if text.strip():
                pending.append(f"{path}：非正文内容作为文字保留，待确认")
                append("para", path, text=text)
    return blocks, pending, assets


def read_docx_material(payload: bytes) -> tuple[list[MaterialBlock], list[str], dict[str, bytes]]:
    """有界读取材料正文及图片字节；外链永不抓取。"""
    if not payload or len(payload) > 32 * 1024 * 1024:
        raise EngineError(ErrorCode.UPLOAD_SIZE, "材料 DOCX 大小超限或为空")
    try:
        return _extract(_members(payload))
    except (
        BadZipFile,
        OSError,
        ValueError,
        RuntimeError,
        NotImplementedError,
        etree.XMLSyntaxError,
        ValidationError,
    ) as exc:
        raise EngineError(ErrorCode.SOURCE_LOAD_FAILED, "材料 DOCX 损坏或结构超出边界") from exc
