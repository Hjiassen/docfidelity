"""独立正文生成协议与持久化运行审计，模型仅消费新材料语义。"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Protocol

from pydantic import Field, model_validator
from typing_extensions import Self

from .base import FormatModel
from .documents import DocumentDraft, OutlineSection, SectionDraft
from .errors import EngineErrorInfo
from .materials import BlockKind, Digest, Identifier, Pending, SourceMaterial


class WritingRequest(FormatModel):
    """请求体只包含当前材料和语义能力，不含模板原件或布局。"""

    task: Literal["outline", "section"]
    name: str = Field(min_length=1, max_length=512)
    instructions: str = Field(max_length=20_000)
    materials: list[SourceMaterial] = Field(min_length=1, max_length=100)
    outline: list[OutlineSection] = Field(default_factory=list, max_length=500)
    section_id: Identifier | None = None
    capabilities: list[BlockKind] = Field(default_factory=list, max_length=6)

    @model_validator(mode="after")
    def validate_request(self) -> Self:
        """请求整体有界，逐章请求必须指向已提供的大纲。"""
        if len({m.material_id for m in self.materials}) != len(self.materials):
            raise ValueError("材料 ID 不得重复")
        if self.task == "section" and self.section_id not in {s.section_id for s in self.outline}:
            raise ValueError("逐章请求必须指定当前大纲中的章节")
        if len(self.model_dump_json()) > 4_000_000:
            raise ValueError("生成请求超出上限，不得静默截断")
        return self


class WritingResult(FormatModel):
    """生成候选；消费者必须检查来源与数字支持后才能采用。"""

    outline: list[OutlineSection] = Field(default_factory=list, max_length=500)
    section: SectionDraft | None = None
    pending: list[Pending] = Field(default_factory=list, max_length=2000)
    model: str | None = Field(default=None, max_length=512)
    tokens_in: int = Field(default=0, ge=0)
    tokens_out: int = Field(default=0, ge=0)
    error: str | None = Field(default=None, max_length=2000)


class ContentGenerator(Protocol):
    """与候选选择协议独立的内容生成接口。"""

    def generate(self, request: WritingRequest) -> WritingResult:
        """返回待验证的语义内容或安全失败说明。"""
        ...


class GenerationAttempt(FormatModel):
    """一次调用的安全审计；不持久化密钥、端点或响应正文。"""

    section_id: Identifier | None = None
    attempt: int = Field(default=1, ge=1, le=10)
    model: str | None = Field(default=None, max_length=512)
    tokens_in: int = Field(default=0, ge=0)
    tokens_out: int = Field(default=0, ge=0)
    adopted: bool = False
    fallback_used: bool = False
    error: str | None = Field(default=None, max_length=2000)


class GenerationRun(FormatModel):
    """输入快照不变；晚到结果作为候选保存，不覆盖用户新修订。"""

    run_id: Identifier
    document_id: Identifier
    kind: Literal["outline", "content", "render"]
    state: Literal["queued", "running", "succeeded", "failed", "canceled"] = "queued"
    input_digest: Digest
    document_revision: int = Field(ge=1)
    document_snapshot: DocumentDraft
    materials_snapshot: list[SourceMaterial] = Field(min_length=1, max_length=100)
    template_id: Identifier
    template_version: int = Field(ge=1)
    model: str | None = Field(default=None, max_length=512)
    model_identity: str | None = Field(default=None, max_length=512)
    model_enabled: bool = False
    prompt_version: str = Field(min_length=1, max_length=128)
    attempts: list[GenerationAttempt] = Field(default_factory=list, max_length=5000)
    event_cursor: int = Field(default=0, ge=0)
    result_revision: int | None = Field(default=None, ge=1)
    result_document: DocumentDraft | None = None
    artifact_ids: list[Identifier] = Field(default_factory=list, max_length=1000)
    section_checkpoints: list[SectionDraft] = Field(default_factory=list, max_length=500)
    error: EngineErrorInfo | None = None
    created_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None

    @model_validator(mode="after")
    def validate_snapshot(self) -> Self:
        """运行的显式版本与输入快照必须一致。"""
        document = self.document_snapshot
        if (
            document.document_id,
            document.revision,
            document.template_id,
            document.template_version,
        ) != (self.document_id, self.document_revision, self.template_id, self.template_version):
            raise ValueError("运行与文档快照版本不一致")
        ids = [material.material_id for material in self.materials_snapshot]
        if len(set(ids)) != len(ids) or set(ids) != set(document.material_ids):
            raise ValueError("运行材料快照不一致")
        return self


class RunEvent(FormatModel):
    """按运行单调递增、持久化后才发布的回放事件。"""

    event_id: int = Field(ge=1)
    run_id: Identifier
    type: Literal["run", "section", "artifact", "log"]
    payload: dict[str, Any] = Field(default_factory=dict, max_length=100)
    created_at: datetime | None = None
