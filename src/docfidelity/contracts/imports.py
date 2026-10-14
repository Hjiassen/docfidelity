"""有效模板导入与冻结契约。所有定位在导入期确定。"""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import Field

from .base import FormatModel

CandidateKind = Literal["cover", "toc", "body", "header", "footer"]
PrototypeKind = Literal["heading", "body", "list", "table", "image"]


class StructureKind(str, Enum):
    PARAGRAPH = "paragraph"
    TABLE = "table"
    IMAGE = "image"
    OBJECT = "object"
    CONTAINER = "container"


class StructureNode(FormatModel):
    node_id: str
    part: str
    xpath: str
    block_index: int
    kind: StructureKind
    text_preview: str = ""
    heading_level: int | None = None
    supported: bool = True
    top_level: bool = False


class SelectionRange(FormatModel):
    kind: CandidateKind
    start_node_id: str
    end_node_id: str


class TemplateCandidate(FormatModel):
    candidate_id: str
    kind: CandidateKind
    status: Literal["detected", "unrecognized", "unsupported"]
    ranges: list[SelectionRange] = Field(default_factory=list)
    reason: str


class ImportDraft(FormatModel):
    import_id: str
    source_sha256: str
    source_size: int
    nodes: list[StructureNode]
    candidates: list[TemplateCandidate]


class VariableMapping(FormatModel):
    key: str
    node_id: str
    required: bool = True


class TemplateSelection(FormatModel):
    import_id: str
    source_sha256: str
    ranges: list[SelectionRange] = Field(default_factory=list)
    variables: list[VariableMapping] = Field(default_factory=list)
    fixed_assets: list[str] = Field(default_factory=list)
    confirmed_absent: list[CandidateKind] = Field(default_factory=list)


class FrozenVariable(FormatModel):
    key: str
    part: str
    marker: str
    required: bool


class FrozenPrototype(FormatModel):
    prototype_id: str
    kind: PrototypeKind
    path: str
    source_node_id: str | None = None
    heading_level: int | None = None
    row_path: str | None = None
    columns: int | None = None


class FormatBindings(FormatModel):
    heading: dict[int, str] = Field(default_factory=dict)
    body: str | None = None
    list_paragraph: str | None = None
    tables: list[str] = Field(default_factory=list)
    image: str | None = None
    insertion_part: str = "word/document.xml"
    insertion_marker: str = "df:insert:content"


class CleanupReport(FormatModel):
    removed_parts: list[str]
    retained_parts: list[str]
    cleared_text_containers: int
    excluded_objects: int
    fixed_assets: list[str]


class TemplateVersion(FormatModel):
    template_id: str
    version: int = Field(ge=1)
    name: str
    source_sha256: str
    selection: TemplateSelection
    variables: list[FrozenVariable]
    prototypes: list[FrozenPrototype]
    format_bindings: FormatBindings
    cleanup_report: CleanupReport
    file_hashes: dict[str, str]
    content_hash: str
