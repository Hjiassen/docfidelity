"""可编辑文档到稳定内容 IR，不读取模板与文件系统。"""

from __future__ import annotations

from docfidelity.contracts.documents import DocumentDraft, DraftBlock
from docfidelity.contracts.errors import EngineError, ErrorCode
from docfidelity.contracts.ir import ContentIR, ContentUnit, OriginRef
from docfidelity.contracts.materials import SourceMaterial

from .draft import rule_section, validate_section
from .outline import MaterialIndex, material_index, rule_pending


def _origin(refs: list[str], index: MaterialIndex, *, verified: bool) -> OriginRef:
    if not refs:
        return OriginRef(source="manual", locator="用户编辑", verified=False)
    material, block = index[refs[0]]
    return OriginRef(source=material.material_id, locator=block.locator, verified=verified)


def _unit(block: DraftBlock, unit_id: str, index: MaterialIndex, *, edited: bool) -> ContentUnit:
    fields = {}
    if block.columns:
        fields = {"columns": block.columns, "rows": block.rows}
    elif block.items:
        fields = {"items": block.items}
    elif block.image_ref:
        fields = {"image_ref": block.image_ref, "caption": block.text or None}
    exact = any(
        block.model_dump(exclude={"source_refs"})
        == DraftBlock.model_validate(
            index[r][1].model_dump(exclude={"block_id", "locator", "level"}) | {"source_refs": [r]}
        ).model_dump(exclude={"source_refs"})
        for r in block.source_refs
        if index[r][1].kind != "heading"
    )
    return ContentUnit(
        unit_id=unit_id,
        type=block.kind,
        text=block.text or None,
        fields=fields,
        trace=block.source_refs,
        origin=_origin(block.source_refs, index, verified=not edited and exact),
    )


def assemble_content(document: DocumentDraft, materials: list[SourceMaterial]) -> ContentIR:
    """尊重已确认大纲的改名、增删、顺序，保留表图与人工来源状态。"""
    index = material_index(document, materials)
    if not document.outline_confirmed:
        raise EngineError(ErrorCode.STATE_INVALID, "请先确认当前大纲")
    drafts = {section.section_id: section for section in document.sections}
    unresolved = [*document.pending, *rule_pending(document, materials)]
    units = []
    for outline in document.outline:
        section = drafts.get(outline.section_id) or rule_section(outline, index)
        if section.blocks:
            validate_section(section, outline, index)
        unresolved.extend(section.pending)
        if not section.blocks:
            unresolved.append(f"待确认：{outline.title}缺少正文。")
        children = [
            _unit(block, f"{outline.section_id}/b{n:04d}", index, edited=section.edited)
            for n, block in enumerate(section.blocks, 1)
        ]
        if section.edited:
            unresolved.append(f"待确认：{outline.title}含人工编辑，来源未经自动核实。")
        elif any(child.origin is not None and not child.origin.verified for child in children):
            unresolved.append(
                f"待确认：{outline.title}含改写，来源及数字校验不代表语义可靠，请审核。"
            )
        units.append(
            ContentUnit(
                unit_id=outline.section_id,
                type="heading",
                text=outline.title,
                level=outline.level,
                children=children,
                trace=outline.source_refs,
                origin=_origin(outline.source_refs, index, verified=False),
            )
        )
    return ContentIR(
        project_id=document.document_id,
        doc_key=document.document_id,
        template_ref=f"{document.template_id}@{document.template_version}",
        units=units,
        source_digests={m.material_id: m.digest for m in materials},
        unresolved=list(dict.fromkeys(unresolved)),
    )
