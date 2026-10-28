"""可编辑文档修订与章节草稿契约。"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator
from typing_extensions import Self

from .base import FormatModel
from .materials import AssetRef, Identifier, MaterialBlock, Pending, SourceRef, Text


class OutlineSection(FormatModel):
    """用户可确认、重排的大纲项。"""

    section_id: Identifier
    title: str = Field(min_length=1, max_length=512)
    level: int = Field(default=1, ge=1, le=9)
    source_refs: list[SourceRef] = Field(default_factory=list, max_length=2000)


class DraftBlock(FormatModel):
    """不携带布局或 OOXML 的正文块。"""

    kind: Literal["para", "bullets", "numbered", "table", "figure"]
    text: Text = ""
    items: list[Text] = Field(default_factory=list, max_length=2000)
    columns: list[Text] = Field(default_factory=list, max_length=100)
    rows: list[list[Text]] = Field(default_factory=list, max_length=2000)
    image_ref: AssetRef | None = None
    source_refs: list[SourceRef] = Field(default_factory=list, max_length=2000)

    @model_validator(mode="after")
    def validate_content(self) -> Self:
        """沿用材料块的同一套形状约束。"""
        MaterialBlock(block_id="draft", locator="draft", **self.model_dump(exclude={"source_refs"}))
        return self


class SectionDraft(FormatModel):
    """无来源的人工作品必须显式声明 edited。"""

    section_id: Identifier
    blocks: list[DraftBlock] = Field(max_length=2000)
    edited: bool = False
    pending: list[Pending] = Field(default_factory=list, max_length=2000)

    @model_validator(mode="after")
    def validate_sources(self) -> Self:
        """无来源内容不能伪装成自动采用的材料。"""
        if not self.edited and any(not block.source_refs for block in self.blocks):
            raise ValueError("无来源正文必须显式标记 edited")
        return self


class DocumentDraft(FormatModel):
    """冻结到具体材料和模板版本的文档修订。"""

    document_id: Identifier
    revision: int = Field(ge=1)
    name: str = Field(min_length=1, max_length=512)
    template_id: Identifier
    template_version: int = Field(ge=1)
    material_ids: list[Identifier] = Field(min_length=1, max_length=100)
    mode: Literal["format_only", "organize"]
    instructions: str = Field(default="", max_length=20_000)
    outline: list[OutlineSection] = Field(default_factory=list, max_length=500)
    outline_confirmed: bool = False
    sections: list[SectionDraft] = Field(default_factory=list, max_length=500)
    variables: dict[str, Text] = Field(default_factory=dict, max_length=100)
    pending: list[Pending] = Field(default_factory=list, max_length=2000)

    @model_validator(mode="after")
    def validate_structure(self) -> Self:
        """拒绝重复 ID、游离章节和跨材料引用。"""
        outline_ids = {section.section_id for section in self.outline}
        if len(set(self.material_ids)) != len(self.material_ids):
            raise ValueError("材料 ID 不得重复")
        if len(outline_ids) != len(self.outline) or len(
            {s.section_id for s in self.sections}
        ) != len(self.sections):
            raise ValueError("章节 ID 不得重复")
        if any(section.section_id not in outline_ids for section in self.sections):
            raise ValueError("正文必须属于当前大纲")
        references = [ref for section in self.outline for ref in section.source_refs]
        references.extend(
            ref
            for section in self.sections
            for block in section.blocks
            for ref in block.source_refs
        )
        if any(ref.split("/")[0] not in self.material_ids for ref in references):
            raise ValueError("来源必须属于当前材料")
        if self.outline_confirmed and not self.outline:
            raise ValueError("空大纲不能确认")
        if len(self.model_dump_json()) > 4_000_000:
            raise ValueError("文档草稿超出上限")
        return self
