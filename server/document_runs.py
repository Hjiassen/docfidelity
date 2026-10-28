"""冻结输入的可恢复生成队列；模型调用不持有编辑事务锁。"""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from docfidelity.contracts.documents import DocumentDraft
from docfidelity.contracts.errors import EngineError, EngineErrorInfo, ErrorCode
from docfidelity.contracts.generation import (
    CONTENT_PROMPT_VERSION,
    ContentGenerator,
    GenerationAttempt,
    GenerationRun,
    RunEvent,
    WritingRequest,
    WritingResult,
)
from docfidelity.pipeline.content_provider import create_content_generator
from docfidelity.pipeline.writing import (
    assemble_content,
    generate_outline,
    generate_section,
    validate_render_capabilities,
)
from docfidelity.platform.fs import atomic_write_bytes

from .document_store import Records, transaction_lock
from .documents import Documents, sections_for_outline
from .ids import new_id
from .jobs.events import WorkspaceExecutionLocks

PROMPT_VERSION = CONTENT_PROMPT_VERSION
RunKind = Literal["outline", "content", "render"]


class RunRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run: GenerationRun
    events: list[RunEvent] = Field(default_factory=list)
    artifacts: list[dict[str, Any]] = Field(default_factory=list)
    template_digest: str
    asset_digests: dict[str, str] = Field(default_factory=dict)
    idempotency_key: str | None = None
    pending: list[str] = Field(default_factory=list)
    result_prepared: bool = False
    section_id: str | None = None


class CountedGenerator:
    def __init__(self, generator: ContentGenerator) -> None:
        self.generator = generator
        self.calls = 0

    def generate(self, request: WritingRequest) -> WritingResult:
        self.calls += 1
        return self.generator.generate(request)


class DocumentRuns:
    def __init__(self, documents: Documents, generator: ContentGenerator | None = None) -> None:
        self.documents = documents
        self.settings = documents.settings
        self.records = Records(self.settings.storage_root / "v2", "runs")
        self.execution = WorkspaceExecutionLocks(
            self.settings.storage_root / "v2" / "run-execution"
        )
        self.generator = generator

    def _identity(self) -> str:
        secret = self.settings.model_api_key
        config = {
            "enabled": self.settings.model_enabled,
            "model": self.settings.model_name,
            "url": self.settings.model_base_url,
            "key": secret.get_secret_value() if secret else None,
        }
        return sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()

    def _read(self, run_id: str) -> RunRecord:
        try:
            record = RunRecord.model_validate(self.records.read(run_id))
            if (
                record.run.run_id != run_id
                or record.run.event_cursor != len(record.events)
                or any(
                    event.run_id != run_id or event.event_id != index
                    for index, event in enumerate(record.events, 1)
                )
            ):
                raise ValueError("运行身份或事件游标不一致")
            return record
        except (ValidationError, ValueError) as exc:
            raise EngineError(ErrorCode.PACKAGE_INCOMPLETE, "运行快照损坏") from exc

    def _save(
        self,
        record: RunRecord,
        event_type: Literal["run", "section", "artifact", "log"],
        payload: dict[str, Any],
    ) -> None:
        event = RunEvent(
            event_id=len(record.events) + 1,
            run_id=record.run.run_id,
            type=event_type,
            payload=payload,
            created_at=datetime.now(timezone.utc),
        )
        record.events.append(event)
        record.run.event_cursor = event.event_id
        validated = RunRecord.model_validate(record.model_dump())
        self.records.commit(record.run.run_id, validated.model_dump(mode="json"))

    def get(self, run_id: str) -> GenerationRun:
        with transaction_lock(self.documents.lock_path):
            return self._read(run_id).run

    def events(self, run_id: str, cursor: int) -> list[RunEvent]:
        with transaction_lock(self.documents.lock_path):
            return [e for e in self._read(run_id).events if e.event_id > cursor]

    def submit(
        self,
        document_id: str,
        kind: RunKind,
        if_match: str | None = None,
        idempotency_key: str | None = None,
        section_id: str | None = None,
    ) -> GenerationRun:
        from .template_imports import match_revision

        with transaction_lock(self.documents.lock_path):
            if section_id is not None and kind != "content":
                raise EngineError(ErrorCode.SCHEMA_INVALID, "只有正文运行可选择章节")
            key = (
                sha256(idempotency_key.encode()).hexdigest()
                if idempotency_key is not None
                else None
            )
            if key:
                for run_id in self.records.identifiers():
                    prior = self._read(run_id)
                    if prior.idempotency_key == key and prior.run.document_id == document_id:
                        if prior.run.kind != kind or prior.section_id != section_id:
                            raise EngineError(
                                ErrorCode.VERSION_CONFLICT, "幂等键已用于其他运行类型"
                            )
                        if if_match is not None:
                            match_revision(if_match, prior.run.document_revision)
                        elif self.documents.get(document_id).revision not in {
                            prior.run.document_revision,
                            prior.run.result_revision,
                        }:
                            raise EngineError(
                                ErrorCode.VERSION_CONFLICT, "文档已变化，请使用新的幂等键"
                            )
                        return prior.run
            document = self.documents.get(document_id)
            if section_id is not None and section_id not in {
                s.section_id for s in document.outline
            }:
                raise EngineError(ErrorCode.SCHEMA_INVALID, "所选章节不在当前大纲中")
            if if_match is not None:
                match_revision(if_match, document.revision)
            if kind != "outline" and not document.outline_confirmed:
                raise EngineError(ErrorCode.STATE_INVALID, "请先确认大纲")
            self.documents.validate(document)
            materials = [self.documents.materials.get(key) for key in document.material_ids]
            assets = self.documents.materials.assets(materials)
            template = self.documents.templates.version(
                document.template_id, document.template_version
            )
            identity = self._identity()
            asset_digests = {name: sha256(data).hexdigest() for name, data in assets.items()}
            inputs = {
                "document": document.model_dump(mode="json"),
                "materials": [m.model_dump(mode="json") for m in materials],
                "template_digest": template.content_hash,
                "assets": asset_digests,
                "model_identity": identity,
                "prompt_version": PROMPT_VERSION,
                "kind": kind,
                "section_id": section_id,
            }
            run = GenerationRun(
                run_id=new_id("r_"),
                document_id=document_id,
                kind=kind,
                input_digest=sha256(
                    json.dumps(inputs, sort_keys=True, ensure_ascii=False).encode()
                ).hexdigest(),
                document_revision=document.revision,
                document_snapshot=document,
                materials_snapshot=materials,
                template_id=document.template_id,
                template_version=document.template_version,
                model=self.settings.model_name,
                model_identity=identity,
                model_enabled=self.settings.model_enabled,
                prompt_version=PROMPT_VERSION,
                created_at=datetime.now(timezone.utc),
            )
            record = RunRecord(
                run=run,
                template_digest=template.content_hash,
                asset_digests=asset_digests,
                idempotency_key=key,
                section_id=section_id,
            )
            self._save(record, "run", {"state": "queued"})
            return run

    def cancel(self, run_id: str) -> GenerationRun:
        with transaction_lock(self.documents.lock_path):
            record = self._read(run_id)
            if record.run.state in {"queued", "running"}:
                record.run.state = "canceled"
                record.run.finished_at = datetime.now(timezone.utc)
                self._save(record, "run", {"state": "canceled"})
            return record.run

    def artifacts(self, run_id: str) -> list[dict[str, Any]]:
        with transaction_lock(self.documents.lock_path):
            record = self._read(run_id)
            return record.artifacts if record.run.state == "succeeded" else []

    def download(self, run_id: str, artifact_id: str) -> tuple[Path, dict[str, Any]]:
        artifact = next(
            (a for a in self.artifacts(run_id) if a["artifact_id"] == artifact_id), None
        )
        if artifact is None:
            raise EngineError(ErrorCode.NOT_FOUND, "运行产物不存在")
        path = self.records.directory(run_id) / "artifacts" / artifact_id
        try:
            if path.is_symlink() or sha256(path.read_bytes()).hexdigest() != artifact["digest"]:
                raise ValueError("摘要不一致")
        except (OSError, ValueError) as exc:
            raise EngineError(ErrorCode.PACKAGE_INCOMPLETE, "运行产物损坏") from exc
        return path, artifact

    def _generator(self, run: GenerationRun) -> CountedGenerator | None:
        if (
            not run.model_enabled
            or run.model_identity != self._identity()
            or run.prompt_version != PROMPT_VERSION
            or run.document_snapshot.mode == "format_only"
        ):
            return None
        provider = self.generator
        if provider is None and self.settings.model_name and self.settings.model_base_url:
            provider = create_content_generator(
                model=self.settings.model_name,
                base_url=self.settings.model_base_url,
                api_key=self.settings.model_api_key.get_secret_value()
                if self.settings.model_api_key
                else None,
            )
        return CountedGenerator(provider) if provider is not None else None

    def _attempt(
        self, result: WritingResult, called: bool, section_id: str | None = None
    ) -> GenerationAttempt:
        return GenerationAttempt(
            section_id=section_id,
            model=result.model,
            tokens_in=result.tokens_in if called else 0,
            tokens_out=result.tokens_out if called else 0,
            adopted=called and result.error is None,
            fallback_used=result.error is not None,
            error=result.error,
        )

    def _candidate(self, document: DocumentDraft, **values: Any) -> DocumentDraft:
        return DocumentDraft.model_validate(document.model_dump() | values)

    def _finish(
        self, run_id: str, candidate: DocumentDraft, artifact: dict[str, Any] | None = None
    ) -> GenerationRun:
        with transaction_lock(self.documents.lock_path):
            record = self._read(run_id)
            run = record.run
            if run.state == "canceled":
                return run
            current = self.documents.get(run.document_id)
            # 候选先写运行日志，恢复时可辨认已发布修订，避免再次采用。
            if not record.result_prepared:
                candidate = self._candidate(
                    candidate, revision=self.documents.records.next_revision(run.document_id)
                )
                run.result_document = candidate
                record.result_prepared = True
                self._save(record, "log", {"result_prepared": True})
            else:
                if run.result_document is None:
                    raise EngineError(ErrorCode.PACKAGE_INCOMPLETE, "已准备运行缺少结果候选")
                candidate = run.result_document
            if run.kind != "render" and current.revision == run.document_revision:
                target = (
                    self.documents.records.directory(run.document_id)
                    / "revisions"
                    / f"{candidate.revision}.json"
                )
                if target.exists():
                    # 上次崩溃可能留下未发布修订，保留审计文件并重新分配编号。
                    candidate = self._candidate(
                        candidate, revision=self.documents.records.next_revision(run.document_id)
                    )
                    run.result_document = candidate
                    self._save(record, "log", {"result_prepared": True})
                self.documents.commit(candidate)
                run.result_revision = candidate.revision
            elif current == candidate:
                run.result_revision = candidate.revision
            if artifact is not None:
                record.artifacts = [artifact]
                run.artifact_ids = [artifact["artifact_id"]]
            run.state = "succeeded"
            run.finished_at = datetime.now(timezone.utc)
            self._save(
                record,
                "run",
                {
                    "state": "succeeded",
                    "result_revision": run.result_revision,
                    "artifact_ids": run.artifact_ids,
                },
            )
            return run

    def _execute(self, run_id: str) -> GenerationRun:
        with transaction_lock(self.documents.lock_path):
            record = self._read(run_id)
            if record.run.state not in {"queued", "running"}:
                return record.run
            record.run.state = "running"
            record.run.started_at = record.run.started_at or datetime.now(timezone.utc)
            self._save(record, "run", {"state": "running"})
        run = record.run
        template = self.documents.templates.version(run.template_id, run.template_version)
        assets = self.documents.materials.assets(run.materials_snapshot)
        if (
            template.content_hash != record.template_digest
            or {k: sha256(v).hexdigest() for k, v in assets.items()} != record.asset_digests
        ):
            raise EngineError(ErrorCode.PACKAGE_INCOMPLETE, "冻结模板或素材摘要不一致")
        if run.result_document is not None and run.kind != "render":
            return self._finish(run_id, run.result_document)
        document = run.document_snapshot
        provider = self._generator(run)
        config_pending = (
            ["待确认：模型配置已变化，本运行按规则回退。"]
            if run.model_identity != self._identity()
            else []
        )
        if run.prompt_version != PROMPT_VERSION:
            config_pending.append("待确认：提示版本已变化，本运行按规则回退。")
        if run.kind == "outline":
            result = generate_outline(document, run.materials_snapshot, provider)
            sections, outline_pending = sections_for_outline(document, result.outline)
            candidate = self._candidate(
                document,
                outline=[s.model_dump() for s in result.outline],
                outline_confirmed=False,
                sections=[section.model_dump() for section in sections],
                pending=list(
                    dict.fromkeys(
                        [*document.pending, *result.pending, *config_pending, *outline_pending]
                    )
                ),
            )
            with transaction_lock(self.documents.lock_path):
                record = self._read(run_id)
                if record.run.state == "canceled":
                    return record.run
                attempt = self._attempt(result, bool(provider and provider.calls))
                if provider and provider.calls:
                    record.run.attempts.append(attempt)
                record.run.result_document = candidate
                self._save(
                    record,
                    "section",
                    {
                        "outline_checkpoint": True,
                        "adopted": attempt.adopted,
                        "called": bool(provider and provider.calls),
                    },
                )
            return self._finish(run_id, candidate)
        if run.kind == "content":
            drafts = {s.section_id: s for s in document.sections}
            drafts.update({s.section_id: s for s in run.section_checkpoints})
            pending = [*document.pending, *record.pending, *config_pending]
            for outline in document.outline:
                if record.section_id is not None and outline.section_id != record.section_id:
                    continue
                if outline.section_id in {s.section_id for s in run.section_checkpoints}:
                    continue
                working = self._candidate(
                    document,
                    sections=[
                        drafts[s.section_id].model_dump()
                        for s in document.outline
                        if s.section_id in drafts
                    ],
                )
                before = provider.calls if provider else 0
                result = generate_section(
                    working, run.materials_snapshot, outline.section_id, provider
                )
                if result.section is None:
                    raise EngineError(ErrorCode.SCHEMA_INVALID, "章节结果缺失")
                pending.extend(result.pending)
                with transaction_lock(self.documents.lock_path):
                    record = self._read(run_id)
                    if record.run.state == "canceled":
                        return record.run
                    record.run.section_checkpoints.append(result.section)
                    called = bool(provider and provider.calls > before)
                    attempt = self._attempt(result, called, outline.section_id)
                    if called:
                        record.run.attempts.append(attempt)
                    # pending 与章节、审计、事件作为同一个不可变事务保存。
                    record.pending = list(dict.fromkeys(pending))
                    self._save(
                        record,
                        "section",
                        {
                            "section_id": outline.section_id,
                            "adopted": attempt.adopted,
                            "fallback_used": attempt.fallback_used,
                            "called": called,
                        },
                    )
                drafts[outline.section_id] = result.section
            candidate = self._candidate(
                document,
                sections=[
                    drafts[s.section_id].model_dump()
                    for s in document.outline
                    if s.section_id in drafts
                ],
                pending=list(dict.fromkeys(pending)),
            )
            return self._finish(run_id, candidate)
        content = assemble_content(document, run.materials_snapshot)
        validate_render_capabilities(content, template)
        import base64

        rendered = self.documents.templates.render(
            run.template_id,
            run.template_version,
            content,
            document.variables,
            {k: base64.b64encode(v).decode("ascii") for k, v in assets.items()},
        )
        try:
            payload = rendered.read_bytes()
            artifact_id = "a_" + sha256(payload).hexdigest()[:24]
            with transaction_lock(self.documents.lock_path):
                if self._read(run_id).run.state == "canceled":
                    return self._read(run_id).run
                atomic_write_bytes(
                    self.records.directory(run_id) / "artifacts" / artifact_id, payload
                )
                return self._finish(
                    run_id,
                    document,
                    {
                        "artifact_id": artifact_id,
                        "name": "document.docx",
                        "media_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                        "size_bytes": len(payload),
                        "digest": sha256(payload).hexdigest(),
                    },
                )
        finally:
            shutil.rmtree(rendered.parent, ignore_errors=True)

    def run_once(self) -> GenerationRun | None:
        for run_id in self.records.identifiers():
            if self.get(run_id).state not in {"queued", "running"}:
                continue
            with self.execution.acquire(run_id) as acquired:
                if not acquired:
                    continue
                try:
                    return self._execute(run_id)
                except Exception as exc:
                    error = (
                        exc
                        if isinstance(exc, EngineError)
                        else EngineError(ErrorCode.INTERNAL, "运行失败，请检查服务端记录")
                    )
                    with transaction_lock(self.documents.lock_path):
                        record = self._read(run_id)
                        if record.run.state != "canceled":
                            record.run.state = "failed"
                            record.run.error = EngineErrorInfo.from_error(error)
                            record.run.finished_at = datetime.now(timezone.utc)
                            self._save(record, "run", {"state": "failed", "code": error.code})
                        return record.run
        return None

    def run_until_idle(self) -> list[GenerationRun]:
        outcomes: list[GenerationRun] = []
        while (run := self.run_once()) is not None:
            outcomes.append(run)
        return outcomes
