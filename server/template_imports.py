"""V2 导入持久化：current 是唯一提交真相，模板索引只是缓存。"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import tempfile
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from docfidelity.contracts.errors import EngineError, ErrorCode
from docfidelity.contracts.imports import ImportDraft, TemplateSelection, TemplateVersion
from docfidelity.contracts.ir import ContentIR
from docfidelity.pipeline import template_imports as engine
from docfidelity.platform.fs import atomic_write_bytes, atomic_write_text, safe_join

from .ids import new_id
from .settings import ServerSettings
from .template_import_io import import_lock, logical_path, validate_docx_zip, write_image_assets
from .uploads import probe_upload, validate_upload

_LOGGER = logging.getLogger(__name__)


class ImportSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")
    import_id: str
    template_id: str
    revision: int = Field(ge=1)
    filename: str
    draft: ImportDraft
    selection: TemplateSelection
    versions: list[int] = Field(default_factory=list)


class TemplateDetail(BaseModel):
    template_id: str
    name: str
    current_version: int
    versions: list[int]
    content_hash: str


def _segment(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", value):
        raise EngineError(ErrorCode.PATH_ESCAPE, "标识不是安全路径段")
    return value


def match_revision(value: str | None, current: int) -> None:
    """接受裸数字或强 ETag；不允许弱标签、通配符及多标签绕过并发检查。"""
    normalized = value.strip() if value is not None else ""
    if re.fullmatch(r'"[1-9][0-9]*"', normalized):
        normalized = normalized[1:-1]
    if not re.fullmatch(r"[1-9][0-9]*", normalized) or normalized != str(current):
        raise EngineError(
            ErrorCode.VERSION_CONFLICT,
            "导入修订已变化，请读取最新选择后保存",
            where={"current_revision": current},
            remediation=["重新读取导入草稿后重试"],
        )


class TemplateImports:
    """磁盘存储与用例装配；不使用旧模板仓储或原件对外路径。"""

    def __init__(self, settings: ServerSettings) -> None:
        self.settings = settings
        self.root = settings.storage_root / "v2"
        for name in ("imports", "templates", "renders", "locks"):
            (self.root / name).mkdir(parents=True, exist_ok=True)

    def _directory(self, import_id: str) -> Path:
        return safe_join(self.root, "imports", _segment(import_id))

    def _lock(self, import_id: str) -> Path:
        return safe_join(self.root, "locks", f"{_segment(import_id)}.lock")

    def _read(self, import_id: str) -> ImportSnapshot:
        directory = self._directory(import_id)
        try:
            pointer = json.loads((directory / "current.json").read_text(encoding="utf-8"))
            revision = pointer["revision"]
            if type(revision) is not int or revision < 1:
                raise ValueError("修订指针不合法")
            result = ImportSnapshot.model_validate_json(
                (directory / "revisions" / f"{revision}.json").read_bytes()
            )
            if result.revision != revision or result.import_id != import_id:
                raise ValueError("快照与指针不一致")
            _segment(result.template_id)
            return result
        except FileNotFoundError as exc:
            raise EngineError(ErrorCode.NOT_FOUND, "导入草稿不存在或尚未提交") from exc
        except (OSError, ValueError, KeyError, TypeError, ValidationError) as exc:
            raise EngineError(ErrorCode.PACKAGE_INCOMPLETE, "导入提交快照无法读取") from exc

    def _commit(self, snapshot: ImportSnapshot) -> None:
        directory = self._directory(snapshot.import_id)
        # 未提交修订可能遗留；分配新修订时跳过它们，不覆盖审计快照。
        target = directory / "revisions" / f"{snapshot.revision}.json"
        if target.exists():
            raise EngineError(ErrorCode.VERSION_CONFLICT, "导入修订快照已存在")
        atomic_write_text(target, snapshot.model_dump_json(indent=2))
        atomic_write_text(directory / "current.json", json.dumps({"revision": snapshot.revision}))

    def _next_revision(self, snapshot: ImportSnapshot) -> int:
        revisions = self._directory(snapshot.import_id) / "revisions"
        occupied = [int(p.stem) for p in revisions.glob("*.json") if p.stem.isdigit()]
        return max([snapshot.revision, *occupied]) + 1

    def accept(self, filename: str, payload: bytes) -> ImportSnapshot:
        receipt = validate_upload(probe_upload(filename, payload), self.settings)
        if receipt.suffix != ".docx":
            raise EngineError(ErrorCode.UPLOAD_TYPE, "格式来源仅支持 DOCX")
        members = validate_docx_zip(payload)
        validate_upload(probe_upload(filename, payload, members=members), self.settings)
        import_id, template_id = new_id("i_"), new_id("t_")
        directory = self._directory(import_id)
        directory.mkdir()
        try:
            source = atomic_write_bytes(directory / "source.docx", payload)
            draft = engine.inspect_template(source, import_id=import_id)
            snapshot = ImportSnapshot(
                import_id=import_id,
                template_id=template_id,
                revision=1,
                filename=filename,
                draft=draft,
                selection=engine.default_selection(draft),
            )
            with import_lock(self._lock(import_id)):
                self._commit(snapshot)
            return snapshot
        except BaseException:
            shutil.rmtree(directory, ignore_errors=True)
            raise

    def get(self, import_id: str) -> ImportSnapshot:
        with import_lock(self._lock(import_id)):
            return self._read(import_id)

    def save(
        self, import_id: str, selection: TemplateSelection, if_match: str | None
    ) -> ImportSnapshot:
        with import_lock(self._lock(import_id)):
            previous = self._read(import_id)
            match_revision(if_match, previous.revision)
            engine.validate_selection(previous.draft, selection)
            updated = previous.model_copy(
                update={"revision": self._next_revision(previous), "selection": selection}
            )
            self._commit(updated)
            return updated

    def _bundle(self, template_id: str, version: int) -> Path:
        if version < 1:
            raise EngineError(ErrorCode.NOT_FOUND, "模板版本不存在")
        return safe_join(self.root, "templates", _segment(template_id), "versions", str(version))

    def _cache(self, snapshot: ImportSnapshot, version: TemplateVersion) -> None:
        detail = TemplateDetail(
            template_id=version.template_id,
            name=version.name,
            current_version=version.version,
            versions=snapshot.versions,
            content_hash=version.content_hash,
        )
        atomic_write_text(
            self.root / "templates" / version.template_id / "template.json",
            detail.model_dump_json(indent=2),
        )

    def _repair_cache(self, snapshot: ImportSnapshot, version: TemplateVersion) -> None:
        try:
            self._cache(snapshot, version)
        except (OSError, EngineError):
            # current 已提交，缓存失败不能把成功保存伪装成失败。
            _LOGGER.warning("V2 模板索引缓存暂未更新", extra={"template_id": snapshot.template_id})

    def finalize(self, import_id: str, if_match: str | None, name: str) -> TemplateVersion:
        if not name.strip() or len(name) > 200:
            raise EngineError(ErrorCode.SCHEMA_INVALID, "模板名称不能为空且不得超过 200 字符")
        with import_lock(self._lock(import_id)):
            previous = self._read(import_id)
            match_revision(if_match, previous.revision)
            versions_dir = self._bundle(previous.template_id, 1).parent
            versions_dir.mkdir(parents=True, exist_ok=True)
            occupied = [int(p.name) for p in versions_dir.iterdir() if p.name.isdigit()]
            number = max([0, *previous.versions, *occupied]) + 1
            staging = Path(tempfile.mkdtemp(prefix=".freeze-", dir=versions_dir.parent))
            temporary_bundle = staging / "bundle"
            try:
                version = engine.freeze_template(
                    self._directory(import_id) / "source.docx",
                    previous.selection,
                    temporary_bundle,
                    template_id=previous.template_id,
                    version=number,
                    name=name.strip(),
                )
                engine.load_template_version(temporary_bundle)
                destination = self._bundle(previous.template_id, number)
                if destination.exists():
                    raise EngineError(ErrorCode.VERSION_CONFLICT, "模板版本目录已存在")
                # 同卷目录重命名先发布完整包，current 提交前外部读取仍不可见。
                os.rename(temporary_bundle, destination)
                committed = previous.model_copy(
                    update={
                        "revision": self._next_revision(previous),
                        "versions": [*previous.versions, number],
                    }
                )
                self._commit(committed)
                self._repair_cache(committed, version)
                return version
            finally:
                shutil.rmtree(staging, ignore_errors=True)

    def _committed(self) -> list[ImportSnapshot]:
        snapshots: list[ImportSnapshot] = []
        for directory in sorted((self.root / "imports").iterdir()):
            if directory.is_dir() and re.fullmatch(r"[A-Za-z0-9_-]{1,100}", directory.name):
                try:
                    snapshot = self.get(directory.name)
                    if snapshot.versions:
                        snapshots.append(snapshot)
                except EngineError:
                    continue
        return snapshots

    def _template(self, template_id: str) -> ImportSnapshot:
        _segment(template_id)
        for snapshot in self._committed():
            if snapshot.template_id == template_id:
                return snapshot
        raise EngineError(ErrorCode.NOT_FOUND, "模板不存在或尚未提交")

    def list_templates(self) -> list[TemplateDetail]:
        return [self._detail(snapshot) for snapshot in self._committed()]

    def _detail(self, snapshot: ImportSnapshot) -> TemplateDetail:
        number = max(snapshot.versions)
        version = engine.load_template_version(self._bundle(snapshot.template_id, number))
        self._repair_cache(snapshot, version)
        return TemplateDetail(
            template_id=version.template_id,
            name=version.name,
            current_version=number,
            versions=snapshot.versions,
            content_hash=version.content_hash,
        )

    def template(self, template_id: str) -> TemplateDetail:
        return self._detail(self._template(template_id))

    def version(self, template_id: str, number: int) -> TemplateVersion:
        snapshot = self._template(template_id)
        if number not in snapshot.versions:
            raise EngineError(ErrorCode.NOT_FOUND, "模板版本不存在或尚未提交")
        version = engine.load_template_version(self._bundle(template_id, number))
        if version.template_id != template_id or version.version != number:
            raise EngineError(ErrorCode.PACKAGE_INCOMPLETE, "模板版本身份不一致")
        return version

    def render(
        self,
        template_id: str,
        number: int,
        content: ContentIR,
        variables: dict[str, str],
        assets: dict[str, str],
    ) -> Path:
        self.version(template_id, number)
        # 引用检查与图片逻辑名检查先于创建暂存目录。
        if content.template_ref != f"{template_id}@{number}":
            raise EngineError(ErrorCode.SCHEMA_INVALID, "内容引用与请求模板版本不一致")
        for unit in content.walk():
            if unit.type in {"figure", "screenshot"}:
                reference = unit.fields.get("image_ref")
                if not isinstance(reference, str):
                    raise EngineError(ErrorCode.SCHEMA_INVALID, "图片缺少逻辑引用")
                logical_path(reference)
                if reference not in assets:
                    raise EngineError(ErrorCode.SCHEMA_INVALID, "图片逻辑引用未提供素材")
        directory = Path(tempfile.mkdtemp(prefix="render-", dir=self.root / "renders"))
        try:
            write_image_assets(directory / "assets", assets)
            return engine.render_effective(
                self._bundle(template_id, number), content, variables, directory / "document.docx"
            )
        except BaseException:
            shutil.rmtree(directory, ignore_errors=True)
            raise
