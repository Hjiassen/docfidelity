"""新版材料的有界语义契约，来源引用采用 material_id/block_id。"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, StringConstraints, model_validator
from typing_extensions import Self

from .base import FormatModel
from .imports import StructureKind

Identifier = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")]
SourceRef = Annotated[
    str,
    StringConstraints(
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}/[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$"
    ),
]
Digest = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
Text = Annotated[str, StringConstraints(max_length=100_000)]
Pending = Annotated[str, StringConstraints(min_length=1, max_length=2000)]
AssetRef = Annotated[
    str, StringConstraints(min_length=1, max_length=256, pattern=r"^[A-Za-z0-9][A-Za-z0-9_./-]*$")
]
BlockKind = Literal["heading", "para", "bullets", "numbered", "table", "figure"]
MAX_MATERIAL_CHARS = 2_000_000


class MaterialBlock(FormatModel):
    """新材料中的一个语义块；locator 定位原始输入。"""

    block_id: Identifier
    kind: BlockKind
    text: Text = ""
    level: int | None = Field(default=None, ge=1, le=9)
    items: list[Text] = Field(default_factory=list, max_length=2000)
    columns: list[Text] = Field(default_factory=list, max_length=100)
    rows: list[list[Text]] = Field(default_factory=list, max_length=2000)
    image_ref: AssetRef | None = None
    locator: str = Field(min_length=1, max_length=512)

    @model_validator(mode="after")
    def validate_content(self) -> Self:
        """拒绝行列错位、空块和不安全资产引用。"""
        if self.kind not in {"para", "heading", "figure"} and self.text:
            raise ValueError("此块类型不支持 text")
        if self.kind not in {"bullets", "numbered"} and self.items:
            raise ValueError("仅列表可以包含 items")
        if self.kind != StructureKind.TABLE.value and (self.columns or self.rows):
            raise ValueError("仅表格可以包含 columns/rows")
        if self.kind != "figure" and self.image_ref is not None:
            raise ValueError("仅图片可以包含 image_ref")
        if self.kind == "heading" and (self.level is None or not self.text.strip()):
            raise ValueError("标题必须含文字和层级")
        if self.kind != "heading" and self.level is not None:
            raise ValueError("仅标题可以包含层级")
        if self.kind == "para" and not self.text.strip():
            raise ValueError("段落不能为空")
        if self.kind in {"bullets", "numbered"} and not self.items:
            raise ValueError("列表不能为空")
        if self.kind == StructureKind.TABLE.value and (
            not self.columns or any(len(row) != len(self.columns) for row in self.rows)
        ):
            raise ValueError("表格必须有列且行列一致")
        if self.kind == "figure" and self.image_ref is None:
            raise ValueError("图片必须引用资产")
        if self.image_ref and any(part in {"", ".", ".."} for part in self.image_ref.split("/")):
            raise ValueError("资产引用必须为安全逻辑名")
        return self


class SourceMaterial(FormatModel):
    """原始输入摘要与完整的有序材料块；不支持对象进入待确认。"""

    material_id: Identifier
    name: str = Field(min_length=1, max_length=512)
    media_type: Literal["text", "markdown", "docx"]
    digest: Digest
    blocks: list[MaterialBlock] = Field(min_length=1, max_length=2000)
    pending: list[Pending] = Field(default_factory=list, max_length=2000)

    @model_validator(mode="after")
    def validate_blocks(self) -> Self:
        """同材料块 ID 唯一，总字符数有界。"""
        if len({block.block_id for block in self.blocks}) != len(self.blocks):
            raise ValueError("材料块 ID 不得重复")
        if len(self.model_dump_json()) > MAX_MATERIAL_CHARS:
            raise ValueError("材料规范化内容超出上限")
        return self
