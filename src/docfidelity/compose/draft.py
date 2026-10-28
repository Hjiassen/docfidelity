"""逐章撰写和来源验证；人工修改不被自动覆盖。"""

from __future__ import annotations

from collections import Counter

from pydantic import ValidationError

from docfidelity.contracts.documents import DocumentDraft, DraftBlock, OutlineSection, SectionDraft
from docfidelity.contracts.errors import EngineError, ErrorCode
from docfidelity.contracts.generation import ContentGenerator, WritingRequest, WritingResult
from docfidelity.contracts.imports import StructureKind
from docfidelity.contracts.materials import SourceMaterial

from .outline import (
    REVIEW,
    SEMANTIC_KINDS,
    MaterialIndex,
    call_generator,
    material_index,
    require_refs,
    rule_pending,
    validate_numbers,
)


def rule_section(section: OutlineSection, index: MaterialIndex) -> SectionDraft:
    """按当前大纲引用原序拷贝正文，标题引用不重复写入正文。"""
    blocks = []
    for ref in section.source_refs:
        source = index[ref][1]
        if source.kind == "heading":
            continue
        blocks.append(
            DraftBlock.model_validate(
                source.model_dump(exclude={"block_id", "locator", "level", "image_extent"})
                | {"source_refs": [ref]}
            )
        )
    return SectionDraft(
        section_id=section.section_id,
        blocks=blocks,
        pending=[] if blocks else ["待确认：本章缺少正文材料。"],
    )


def validate_section(section: SectionDraft, outline: OutlineSection, index: MaterialIndex) -> None:
    """校验实际来源、章节所有权、数字语句和图片资产所有权。"""
    if section.section_id != outline.section_id or not section.blocks:
        raise EngineError(ErrorCode.SCHEMA_INVALID, "模型返回空正文或其他章节")
    for block in section.blocks:
        require_refs(block.source_refs, index)
        if section.edited:
            continue
        if not block.source_refs or not set(block.source_refs) <= set(outline.source_refs):
            raise EngineError(ErrorCode.SCHEMA_INVALID, "正文来源不属于当前章节")
        sources = [index[ref][1] for ref in block.source_refs]
        if block.kind == "figure" and block.image_ref not in {
            s.image_ref for s in sources if s.kind == "figure"
        }:
            raise EngineError(ErrorCode.SCHEMA_INVALID, "图片不属于所引材料")
        if block.kind == StructureKind.TABLE.value and any(
            source.kind == StructureKind.TABLE.value and block.columns != source.columns
            for source in sources
        ):
            raise EngineError(ErrorCode.SCHEMA_INVALID, "表格列头与所引材料不一致")
        texts = (
            [" | ".join(block.columns), *(" | ".join(row) for row in block.rows)]
            if block.kind == StructureKind.TABLE.value
            else [block.text, *block.items]
        )
        validate_numbers(texts, sources)
    if not section.edited:
        for ref in outline.source_refs:
            source = index[ref][1]
            if source.kind not in {StructureKind.TABLE.value, "figure"}:
                continue
            if not any(
                ref in block.source_refs
                and block.kind == source.kind
                and (source.image_ref is None or block.image_ref == source.image_ref)
                and (
                    source.kind != StructureKind.TABLE.value
                    or (
                        block.columns == source.columns
                        and Counter(map(tuple, source.rows)) <= Counter(map(tuple, block.rows))
                    )
                )
                for block in section.blocks
            ):
                raise EngineError(ErrorCode.SCHEMA_INVALID, "模型遗漏表格或图片，不能静默丢弃材料")


def generate_section(
    document: DocumentDraft,
    materials: list[SourceMaterial],
    section_id: str,
    generator: ContentGenerator | None,
) -> WritingResult:
    """确认大纲后逐章生成；失败保留成功章节，未成功章节按材料回退。"""
    index = material_index(document, materials)
    outline = next((s for s in document.outline if s.section_id == section_id), None)
    if not document.outline_confirmed or outline is None:
        raise EngineError(ErrorCode.STATE_INVALID, "请先确认当前大纲并指定有效章节")
    prior = next((s for s in document.sections if s.section_id == section_id), None)
    pending = rule_pending(document, materials)
    if prior is not None and prior.edited:
        return WritingResult(section=prior, pending=[*pending, *prior.pending])
    fallback = prior if prior is not None and prior.blocks else rule_section(outline, index)
    if document.mode == "format_only":
        return WritingResult(section=fallback, pending=[*pending, *fallback.pending])
    if generator is None:
        return WritingResult(
            section=fallback,
            pending=[*pending, *fallback.pending, "待确认：模型未启用，已保留正文或按材料拷贝。"],
            error="模型未启用",
        )
    try:
        request = WritingRequest.model_validate(
            dict(
                task="section",
                name=document.name,
                instructions=document.instructions,
                materials=materials,
                outline=document.outline,
                section_id=section_id,
                capabilities=SEMANTIC_KINDS,
            )
        )
    except ValidationError as exc:
        raise EngineError(ErrorCode.SCHEMA_INVALID, "生成请求超出限制，不得截断材料") from exc
    candidate = call_generator(generator, request)
    failure = candidate.error
    if not failure:
        try:
            if candidate.section is None or candidate.outline or candidate.section.edited:
                raise EngineError(ErrorCode.SCHEMA_INVALID, "模型正文结构非法或伪造人工编辑标记")
            validate_section(candidate.section, outline, index)
        except EngineError as exc:
            failure = exc.message
    if failure:
        return WritingResult(
            section=fallback,
            pending=[*pending, *fallback.pending, f"待确认：{failure}，已保留正文或按材料回退。"],
            error=failure,
            model=candidate.model,
            tokens_in=candidate.tokens_in,
            tokens_out=candidate.tokens_out,
        )
    return candidate.model_copy(update={"pending": [*pending, REVIEW]})
