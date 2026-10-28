"""文档不可变修订；所有变更先完整校验再提交。"""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from docfidelity.contracts.documents import DocumentDraft
from docfidelity.contracts.errors import EngineError, ErrorCode
from docfidelity.pipeline.writing import validate_document

from .document_store import Records, transaction_lock
from .ids import new_id
from .materials import Materials
from .settings import ServerSettings
from .template_imports import TemplateImports, match_revision


class Documents:
    def __init__(
        self, settings: ServerSettings, materials: Materials, templates: TemplateImports
    ) -> None:
        self.settings, self.materials, self.templates = settings, materials, templates
        self.records = Records(settings.storage_root / "v2", "documents")
        self.lock_path = settings.storage_root / "v2" / "locks" / "documents.lock"

    def validate(self, document: DocumentDraft) -> None:
        self.templates.version(document.template_id, document.template_version)
        materials = [self.materials.get(key) for key in document.material_ids]
        validate_document(document, materials)
        allowed_assets = {b.image_ref for m in materials for b in m.blocks if b.image_ref}
        for section in document.sections:
            for block in section.blocks:
                if block.image_ref and block.image_ref not in allowed_assets:
                    raise EngineError(ErrorCode.SCHEMA_INVALID, "图片不属于当前材料")

    def get(self, document_id: str, revision: int | None = None) -> DocumentDraft:
        with transaction_lock(self.lock_path):
            try:
                result = DocumentDraft.model_validate(self.records.read(document_id, revision))
                if (
                    result.document_id != document_id
                    or revision is not None
                    and result.revision != revision
                ):
                    raise ValueError("文档身份不一致")
                return result
            except (ValidationError, ValueError) as exc:
                raise EngineError(ErrorCode.PACKAGE_INCOMPLETE, "文档快照损坏") from exc

    def list(self) -> list[DocumentDraft]:
        with transaction_lock(self.lock_path):
            return [self.get(key) for key in self.records.identifiers()]

    def commit(self, document: DocumentDraft) -> DocumentDraft:
        self.validate(document)
        self.records.commit(
            document.document_id, document.model_dump(mode="json"), revision=document.revision
        )
        return document

    def create(self, values: dict[str, Any]) -> DocumentDraft:
        with transaction_lock(self.lock_path):
            try:
                document = DocumentDraft.model_validate(
                    {**values, "document_id": new_id("d_"), "revision": 1}
                )
            except ValidationError as exc:
                raise EngineError(ErrorCode.SCHEMA_INVALID, "文档配置非法") from exc
            return self.commit(document)

    def patch(
        self, document_id: str, values: dict[str, Any], if_match: str | None
    ) -> DocumentDraft:
        with transaction_lock(self.lock_path):
            previous = self.get(document_id)
            match_revision(if_match, previous.revision)
            try:
                if any(value is None for value in values.values()):
                    raise ValueError("文档字段不允许 null")
                if "outline" in values and values["outline"] != [
                    s.model_dump() for s in previous.outline
                ]:
                    old = {s.section_id: s for s in previous.outline}
                    values.setdefault("outline_confirmed", False)
                    if "sections" not in values:
                        retained = {
                            item["section_id"]
                            for item in values["outline"]
                            if item["section_id"] in old
                            and item["source_refs"] == old[item["section_id"]].source_refs
                        }
                        values["sections"] = [
                            s.model_dump() for s in previous.sections if s.section_id in retained
                        ]
                document = DocumentDraft.model_validate(
                    previous.model_dump()
                    | values
                    | {"revision": self.records.next_revision(document_id)}
                )
                prior_sections = {s.section_id: s for s in previous.sections}
                if "sections" in values and any(
                    not s.edited and prior_sections.get(s.section_id) != s
                    for s in document.sections
                ):
                    raise ValueError("人工修改正文必须声明 edited")
            except (ValidationError, ValueError) as exc:
                raise EngineError(
                    ErrorCode.SCHEMA_INVALID, "文档修改非法，正文修改须标记 edited"
                ) from exc
            return self.commit(document)
