"""新版撰写门面；模板能力只作本地渲染预检。"""

from docfidelity.compose.assembly import assemble_content
from docfidelity.compose.draft import generate_section
from docfidelity.compose.outline import generate_outline, material_index, suggest_outline
from docfidelity.contracts.documents import DocumentDraft
from docfidelity.contracts.errors import EngineError, ErrorCode
from docfidelity.contracts.imports import StructureKind, TemplateVersion
from docfidelity.contracts.ir import ContentIR
from docfidelity.contracts.materials import BlockKind, SourceMaterial


def validate_document(document: DocumentDraft, materials: list[SourceMaterial]) -> None:
    """校验完整文档和实际来源；编辑阶段无需预先确认大纲。"""
    material_index(document, materials)
    if not document.outline:
        suggest_outline(materials)


def semantic_capabilities(version: TemplateVersion) -> list[BlockKind]:
    """只暴露通用语义类型，不暴露原型、旧文字或样式标识。"""
    bindings = version.format_bindings
    kinds = {p.prototype_id: p.kind for p in version.prototypes}
    result: list[BlockKind] = []
    if any(kinds.get(key) == "heading" for key in bindings.heading.values()):
        result.append("heading")
    if kinds.get(bindings.body or "") == "body":
        result.append("para")
    if kinds.get(bindings.list_paragraph or "") == "list":
        result.extend(["bullets", "numbered"])
    if any(kinds.get(key) == StructureKind.TABLE.value for key in bindings.tables):
        result.append("table")
    if kinds.get(bindings.image or "") == "image":
        result.append("figure")
    return result


def validate_render_capabilities(content: ContentIR, version: TemplateVersion) -> None:
    """冻结能力缺失必须报错，禁止静默丢弃表格或图片。"""
    available = semantic_capabilities(version)
    prototypes = {p.prototype_id: p for p in version.prototypes}
    for unit in content.walk():
        if unit.type not in available:
            raise EngineError(
                ErrorCode.RENDER_BLOCKED,
                "有效模板缺少所需内容能力",
                where={"unit_id": unit.unit_id, "kind": unit.type},
            )
        if unit.type == "heading" and (unit.level or 1) not in version.format_bindings.heading:
            raise EngineError(ErrorCode.RENDER_BLOCKED, "有效模板缺少所需标题层级")
        if unit.type == StructureKind.TABLE.value:
            columns = unit.fields.get("columns", [])
            if not any(
                key in prototypes
                and (
                    prototypes[key].column_policy == "flexible"
                    or prototypes[key].columns == len(columns)
                )
                for key in version.format_bindings.tables
            ):
                raise EngineError(ErrorCode.RENDER_BLOCKED, "有效模板缺少匹配列数的表格原型")


__all__ = [
    "assemble_content",
    "validate_document",
    "generate_outline",
    "generate_section",
    "semantic_capabilities",
    "suggest_outline",
    "validate_render_capabilities",
]
