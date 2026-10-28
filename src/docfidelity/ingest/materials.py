"""纯文本与 Markdown 新材料接入；不访问模板或远程资源。"""

from __future__ import annotations

import hashlib

from markdown_it import MarkdownIt
from markdown_it.token import Token
from pydantic import ValidationError

from docfidelity.contracts.errors import EngineError, ErrorCode
from docfidelity.contracts.materials import MaterialBlock, SourceMaterial

MAX_TEXT_BYTES = 4 * 1024 * 1024


def material_from_blocks(
    blocks: list[MaterialBlock],
    *,
    name: str,
    material_id: str,
    media_type: str,
    digest: str,
    pending: list[str],
) -> SourceMaterial:
    """在接入边界把契约错误转换为统一内核错误。"""
    try:
        return SourceMaterial.model_validate(
            dict(
                material_id=material_id,
                name=name,
                media_type=media_type,
                digest=digest,
                blocks=blocks,
                pending=pending,
            )
        )
    except ValidationError as exc:
        raise EngineError(ErrorCode.SCHEMA_INVALID, "材料为空或超过契约边界") from exc


def _block(index: int, kind: str, locator: str, **values: object) -> MaterialBlock:
    return MaterialBlock.model_validate(
        dict(block_id=f"b{index:04d}", kind=kind, locator=locator, **values)
    )


def _inline(token: Token, pending: list[str], locator: str) -> str:
    parts: list[str] = []
    for child in token.children or []:
        if child.type in {"text", "code_inline", "html_inline"}:
            parts.append(child.content)
            if child.type == "html_inline":
                pending.append(f"{locator}：HTML 作为文字保留，待确认")
        elif child.type in {"softbreak", "hardbreak"}:
            parts.append("\n")
        elif child.type == "image":
            source = child.attrGet("src") or ""
            parts.append(f"![{child.content}]({source})")
            pending.append(f"{locator}：图片链接未读取，待确认")
        elif child.type == "link_open":
            target = child.attrGet("href") or ""
            if target:
                parts.append(f"({target})")
    return "".join(parts)


def _location(token: Token) -> str:
    start, stop = token.map or [0, 1]
    return f"line:{start + 1}-{stop}"


def _markdown(text: str) -> tuple[list[MaterialBlock], list[str]]:
    tokens = MarkdownIt("commonmark", {"html": True}).enable("table").parse(text)
    blocks: list[MaterialBlock] = []
    pending: list[str] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        locator = _location(token)
        values: dict[str, object] | None = None
        kind = "para"
        if token.type in {"heading_open", "paragraph_open"}:
            values = {"text": _inline(tokens[index + 1], pending, locator)}
            if token.type == "heading_open":
                kind = "heading"
                values["level"] = int(token.tag[1:])
            index += 3
        elif token.type in {"fence", "code_block", "html_block"}:
            values = {"text": token.content}
            if token.type == "html_block":
                pending.append(f"{locator}：HTML 作为文字保留，待确认")
            index += 1
        elif token.type in {"bullet_list_open", "ordered_list_open"}:
            kind = "bullets" if token.type == "bullet_list_open" else "numbered"
            stop = index + 1
            depth = 1
            items: list[str] = []
            while stop < len(tokens) and depth:
                current = tokens[stop]
                if current.type in {"bullet_list_open", "ordered_list_open"}:
                    depth += 1
                    pending.append(f"{_location(current)}：嵌套列表按原顺序展开，待确认")
                elif current.type in {"bullet_list_close", "ordered_list_close"}:
                    depth -= 1
                elif current.type == "inline":
                    items.append(_inline(current, pending, _location(current)))
                elif current.type in {"fence", "code_block", "html_block"}:
                    items.append(current.content)
                    pending.append(f"{_location(current)}：列表内复合内容按文字保留，待确认")
                stop += 1
            values = {"items": items}
            index = stop
        elif token.type == "table_open":
            rows: list[list[str]] = []
            row: list[str] = []
            index += 1
            while index < len(tokens) and tokens[index].type != "table_close":
                current = tokens[index]
                if current.type == "tr_open":
                    row = []
                elif current.type == "inline":
                    row.append(_inline(current, pending, locator))
                elif current.type == "tr_close":
                    rows.append(row)
                index += 1
            kind, values = "table", {"columns": rows[0], "rows": rows[1:]}
            index += 1
        elif token.type in {"blockquote_open", "hr"}:
            pending.append(f"{locator}：{token.type} 格式未支持，正文按文字保留，待确认")
            if token.type == "hr":
                start, stop = token.map or [0, 1]
                values = {"text": "\n".join(text.splitlines()[start:stop])}
            index += 1
        else:
            index += 1
        if values is not None:
            blocks.append(_block(len(blocks) + 1, kind, locator, **values))
    return blocks, pending


def ingest_text(
    text: str, *, name: str, material_id: str, markdown: bool = False
) -> SourceMaterial:
    """接入 UTF-8 文本；原始输入（含空白）参与摘要。"""
    try:
        payload = text.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise EngineError(ErrorCode.SOURCE_LOAD_FAILED, "材料包含无效 Unicode 字符") from exc
    if len(payload) > MAX_TEXT_BYTES or not text.strip():
        raise EngineError(ErrorCode.SOURCE_LOAD_FAILED, "材料为空或超过文本大小上限")
    try:
        if markdown:
            blocks, pending = _markdown(text)
        else:
            blocks, pending = [], []
            lines = text.splitlines()
            start = 0
            while start < len(lines):
                if not lines[start].strip():
                    start += 1
                    continue
                stop = start + 1
                while stop < len(lines) and lines[stop].strip():
                    stop += 1
                blocks.append(
                    _block(
                        len(blocks) + 1,
                        "para",
                        f"line:{start + 1}-{stop}",
                        text="\n".join(lines[start:stop]),
                    )
                )
                start = stop
        return material_from_blocks(
            blocks,
            name=name,
            material_id=material_id,
            media_type="markdown" if markdown else "text",
            digest=hashlib.sha256(payload).hexdigest(),
            pending=pending,
        )
    except (ValidationError, ValueError, RecursionError) as exc:
        raise EngineError(ErrorCode.SOURCE_LOAD_FAILED, "材料结构不受支持或超过上限") from exc
