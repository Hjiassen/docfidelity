"""仅从当前新材料生成有来源的大纲，不读取模板。"""

from __future__ import annotations

import re
from hashlib import sha256

from pydantic import ValidationError

from docfidelity.contracts.documents import DocumentDraft, OutlineSection
from docfidelity.contracts.errors import EngineError, ErrorCode
from docfidelity.contracts.generation import ContentGenerator, WritingRequest, WritingResult
from docfidelity.contracts.materials import BlockKind, MaterialBlock, SourceMaterial

SEMANTIC_KINDS: list[BlockKind] = ["heading", "para", "bullets", "numbered", "table", "figure"]
REVIEW = "待确认：来源和数字校验不代表语义正确，请审核模型改写。"
MaterialIndex = dict[str, tuple[SourceMaterial, MaterialBlock]]


def material_index(document: DocumentDraft, materials: list[SourceMaterial]) -> MaterialIndex:
    """完整校验当前输入与实际块归属；不得静默丢弃或截断材料。"""
    try:
        DocumentDraft.model_validate(document.model_dump())
        for material in materials:
            SourceMaterial.model_validate(material.model_dump())
    except (ValidationError, ValueError, TypeError) as exc:
        raise EngineError(ErrorCode.SCHEMA_INVALID, "文档或材料契约非法") from exc
    ids = [material.material_id for material in materials]
    if len(set(ids)) != len(ids) or set(ids) != set(document.material_ids):
        raise EngineError(ErrorCode.SCHEMA_INVALID, "当前材料与文档绑定不一致")
    index = {f"{m.material_id}/{b.block_id}": (m, b) for m in materials for b in m.blocks}
    references = [ref for section in document.outline for ref in section.source_refs]
    references.extend(ref for s in document.sections for b in s.blocks for ref in b.source_refs)
    require_refs(list(dict.fromkeys(references)), index)
    return index


def require_refs(refs: list[str], index: MaterialIndex) -> None:
    """来源必须确实存在于本次材料，重复边也不采用。"""
    if len(set(refs)) != len(refs) or any(ref not in index for ref in refs):
        raise EngineError(ErrorCode.SCHEMA_INVALID, "来源引用不存在、重复或不属于当前材料")


def source_texts(block: MaterialBlock) -> list[str]:
    """表格按整行核查，防止同列数值交换被集合校验放行。"""
    if block.rows or block.columns:
        return [" | ".join(block.columns), *(" | ".join(row) for row in block.rows)]
    return [block.text, *block.items]


def validate_numbers(texts: list[str], sources: list[MaterialBlock]) -> None:
    """含数字语句必须保留来源原句；自由数字改写保守待确认。"""
    supported = {
        re.sub(r"\s+", "", sentence)
        for block in sources
        for text in source_texts(block)
        for sentence in re.split(r"[。！？!?;；\n]", text)
    }
    for text in texts:
        for sentence in re.split(r"[。！？!?;；\n]", text):
            normalized = re.sub(r"\s+", "", sentence)
            if (
                re.search(r"[\d零〇一二三四五六七八九十百千万亿两]", normalized)
                and normalized not in supported
            ):
                raise EngineError(ErrorCode.SCHEMA_INVALID, "数字事实与所引材料不一致或无法核实")


def suggest_outline(materials: list[SourceMaterial]) -> list[OutlineSection]:
    """标题和顺序只来自材料；无标题材料以其文件名分组。"""
    result: list[OutlineSection] = []
    for material in materials:
        current: OutlineSection | None = None
        for block in material.blocks:
            ref = f"{material.material_id}/{block.block_id}"
            if current is None or block.kind == "heading":
                title = block.text if block.kind == "heading" else material.name
                try:
                    current = OutlineSection(
                        section_id="s-" + sha256(ref.encode()).hexdigest()[:24],
                        title=title if len(title) <= 512 else title[:511] + "…",
                        level=block.level or 1,
                    )
                except ValidationError as exc:
                    raise EngineError(
                        ErrorCode.SCHEMA_INVALID, "材料无法转换为合法规则大纲"
                    ) from exc
                result.append(current)
            current.source_refs.append(ref)
    return result


def rule_pending(document: DocumentDraft, materials: list[SourceMaterial]) -> list[str]:
    """材料自身问题与只有项目名的缺项必须贯通。"""
    pending = [p for material in materials for p in material.pending]
    blocks = [block for m in materials for block in m.blocks]
    if any(block.kind == "heading" and len(block.text) > 512 for block in blocks):
        pending.append("待确认：材料标题超过大纲长度上限，已缩短显示；完整原文保留在来源引用。")
    if len(blocks) == 1 and blocks[0].text.strip() == document.name.strip():
        pending.append("待确认：仅有项目名，缺少正文材料，未补写业务事实。")
    return list(dict.fromkeys(pending))


def call_generator(generator: ContentGenerator, request: WritingRequest) -> WritingResult:
    """一次有界调用；异常和服务端正文不进入持久化错误。"""
    try:
        result = generator.generate(request)
        parsed = WritingResult.model_validate(result.model_dump())
        if parsed.error:
            return WritingResult(
                model=parsed.model,
                tokens_in=parsed.tokens_in,
                tokens_out=parsed.tokens_out,
                error="模型未返回可用内容",
            )
        return parsed
    except Exception:
        return WritingResult(error="模型调用失败、超时或输出契约非法")


def generate_outline(
    document: DocumentDraft, materials: list[SourceMaterial], generator: ContentGenerator | None
) -> WritingResult:
    """格式整理零请求；智能整理仅采用完整、来源合法的语义大纲。"""
    index = material_index(document, materials)
    fallback = document.outline or suggest_outline(materials)
    pending = rule_pending(document, materials)
    if document.mode == "format_only":
        return WritingResult(outline=fallback, pending=pending)
    if generator is None:
        return WritingResult(
            outline=fallback,
            pending=[*pending, "待确认：模型未启用，已采用材料规则大纲。"],
            error="模型未启用",
        )
    try:
        request = WritingRequest(
            task="outline",
            name=document.name,
            instructions=document.instructions,
            materials=materials,
            outline=document.outline,
            capabilities=SEMANTIC_KINDS,
        )
    except ValidationError as exc:
        raise EngineError(ErrorCode.SCHEMA_INVALID, "生成请求超出限制，不得截断材料") from exc
    candidate = call_generator(generator, request)
    failure = candidate.error
    if not failure:
        try:
            if (
                not candidate.outline
                or candidate.section is not None
                or len({s.section_id for s in candidate.outline}) != len(candidate.outline)
            ):
                raise EngineError(ErrorCode.SCHEMA_INVALID, "模型大纲结构非法")
            used: set[str] = set()
            for section in candidate.outline:
                require_refs(section.source_refs, index)
                if not section.source_refs:
                    raise EngineError(ErrorCode.SCHEMA_INVALID, "模型大纲缺少来源")
                validate_numbers([section.title], [index[r][1] for r in section.source_refs])
                used.update(section.source_refs)
            if used != set(index):
                raise EngineError(ErrorCode.SCHEMA_INVALID, "模型大纲遗漏材料")
        except EngineError as exc:
            failure = exc.message
    if failure:
        return WritingResult(
            outline=fallback,
            pending=[*pending, f"待确认：{failure}，已保留原大纲或材料规则大纲。"],
            model=candidate.model,
            tokens_in=candidate.tokens_in,
            tokens_out=candidate.tokens_out,
            error=failure,
        )
    return candidate.model_copy(update={"pending": [*pending, REVIEW]})
