"""新材料接入组合面，DOCX 读取仅在这里联接底层。"""

from __future__ import annotations

import hashlib
from pathlib import PurePath

from docfidelity.contracts.errors import EngineError, ErrorCode
from docfidelity.contracts.materials import SourceMaterial
from docfidelity.ingest.materials import ingest_text, material_from_blocks
from docfidelity.ooxml.materials import read_docx_material

MAX_MATERIAL_BYTES = 32 * 1024 * 1024


def ingest_material(
    payload: bytes, *, name: str, material_id: str
) -> tuple[SourceMaterial, dict[str, bytes]]:
    """接入 text/md/docx 字节，不探测或重新解析任何模板。"""
    if not payload or len(payload) > MAX_MATERIAL_BYTES:
        raise EngineError(ErrorCode.SOURCE_LOAD_FAILED, "材料为空或超过上传上限")
    suffix = PurePath(name).suffix.lower()
    if suffix == ".docx":
        blocks, pending, assets = read_docx_material(payload)
        material = material_from_blocks(
            blocks,
            name=name,
            material_id=material_id,
            media_type="docx",
            digest=hashlib.sha256(payload).hexdigest(),
            pending=pending,
        )
        return material, assets
    if suffix not in {".txt", ".md", ".markdown"}:
        raise EngineError(ErrorCode.SOURCE_LOAD_FAILED, "只支持 txt、md 与 docx 新材料")
    try:
        text = payload.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise EngineError(ErrorCode.SOURCE_LOAD_FAILED, "文本材料必须使用 UTF-8 编码") from exc
    material = ingest_text(text, name=name, material_id=material_id, markdown=suffix != ".txt")
    return material.model_copy(update={"digest": hashlib.sha256(payload).hexdigest()}), {}
