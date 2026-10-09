"""发行版演示项目：登记自制资产，再写入每项目隔离的事实来源与三文档配置。"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Final

from pydantic import TypeAdapter

from docfidelity.contracts.config import ProjectConfig
from docfidelity.contracts.domain import DomainModel
from docfidelity.contracts.errors import EngineError, ErrorCode
from docfidelity.contracts.fact import SourceConfig
from docfidelity.contracts.profile import DocTypeProfile
from docfidelity.pipeline import fingerprint_domain_model, fingerprint_profile, path_segment
from docfidelity.platform.fs import atomic_write_bytes, ensure_within, safe_join
from docfidelity.platform.hashing import sha256_hex

from .profiles import DEFAULT_PROFILE_VERSION
from .services import Services
from .storage import Project

__all__ = ["create_demo_project", "initialize_demo_project"]

EXAMPLES_ROOT: Final = Path(__file__).resolve().parent.parent / "examples"
DEMO_AUTHOR: Final = "docfidelity 发行版演示"
DEMO_PROJECTS_DIR: Final = "demo-projects"


@dataclass(frozen=True)
class _SourceAsset:
    config: SourceConfig
    relative_path: Path
    payload: bytes


@dataclass(frozen=True)
class _TemplateAsset:
    template_id: str
    version: str
    filename: str
    payload: bytes
    doc_type: str | None
    notes: str | None

    @property
    def ref(self) -> str:
        return f"{self.template_id}@{self.version}"


@dataclass(frozen=True)
class _DemoAssets:
    project: ProjectConfig
    domains: tuple[DomainModel, ...]
    profiles: tuple[DocTypeProfile, ...]
    sources: tuple[_SourceAsset, ...]
    templates: tuple[_TemplateAsset, ...]


def _asset_error(message: str, *, path: Path | None = None) -> EngineError:
    return EngineError(
        ErrorCode.CONFIG_INVALID,
        message,
        where={} if path is None else {"path": str(path)},
        remediation=[
            "从同一发行版恢复完整的 examples/ 与 demo-templates/ 目录；容器需重新构建镜像",
            "或创建普通项目，手动登记模板、领域模型、档案和事实来源",
        ],
    )


def _read_bytes(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError as exc:
        raise _asset_error(f"演示资产缺失或不可读：{path.name}", path=path) from exc


def _read_json(path: Path) -> Any:
    try:
        return json.loads(_read_bytes(path).decode("utf-8"))
    except (UnicodeError, ValueError) as exc:
        raise _asset_error(f"演示资产不是有效 JSON：{path.name}", path=path) from exc


def _load_assets(services: Services) -> _DemoAssets:
    """在创建项目与登记库资产前，读取并校验完整的发行资产。"""
    try:
        project = ProjectConfig.model_validate(_read_json(EXAMPLES_ROOT / "project.json"))
        domains = TypeAdapter(list[DomainModel]).validate_python(
            _read_json(EXAMPLES_ROOT / "domains.json")
        )
        bundle = _read_json(EXAMPLES_ROOT / "sources.json")
        configs = TypeAdapter(list[SourceConfig]).validate_python(bundle["sources"])
        data_root = safe_join(EXAMPLES_ROOT, bundle["source_root"])
        sources: list[_SourceAsset] = []
        for config in configs:
            if not config.root:
                raise _asset_error(f"演示来源没有文件路径：{config.source_id}")
            source = safe_join(data_root, config.root)
            sources.append(_SourceAsset(config, source.relative_to(data_root), _read_bytes(source)))
        source_ids = {source.config.source_id for source in sources}
        if len(source_ids) != len(sources) or any(
            not entry.source_ids or set(entry.source_ids) - source_ids
            for entry in project.enabled_documents()
        ):
            raise _asset_error("演示项目的事实来源引用不完整或存在重复标识")

        profiles: list[DocTypeProfile] = []
        for profile_id in dict.fromkeys(entry.profile_id for entry in project.documents):
            profile_path = safe_join(EXAMPLES_ROOT, "profiles", f"{profile_id}.json")
            profile = DocTypeProfile.model_validate(_read_json(profile_path))
            if profile.profile_id != profile_id:
                raise _asset_error("演示档案标识与项目引用不一致", path=profile_path)
            profiles.append(profile)

        required = {entry.template_ref for entry in project.documents}
        required.update(ref for profile in profiles for ref in profile.bindings.template_packages)
        templates: dict[str, _TemplateAsset] = {}
        template_root = services.settings.demo_templates_dir.resolve()
        for path in sorted(template_root.glob("*/template.json")):
            declaration = _read_json(path)
            template_id = path_segment(str(declaration["template_id"]), field="template_id")
            version = path_segment(str(declaration["version"]), field="version")
            ref = f"{template_id}@{version}"
            if ref not in required:
                continue
            if ref in templates:
                raise _asset_error(f"演示模板身份重复：{ref}", path=path)
            source = safe_join(path.parent, declaration.get("source", "template.docx"))
            templates[ref] = _TemplateAsset(
                template_id,
                version,
                source.name,
                _read_bytes(source),
                declaration.get("doc_type"),
                declaration.get("notes"),
            )
        missing = required - templates.keys()
        if missing:
            raise _asset_error(
                f"演示模板资产不完整：{'、'.join(sorted(missing))}", path=template_root
            )
        return _DemoAssets(
            project, tuple(domains), tuple(profiles), tuple(sources), tuple(templates.values())
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise _asset_error("演示资产声明不符合配置契约", path=EXAMPLES_ROOT) from exc


def _register_assets(services: Services, assets: _DemoAssets) -> None:
    """复用已登记资产；内容冲突与废弃状态须由操作者处理。"""
    existing_templates = {entry.ref: entry for entry in services.list_templates()}
    for template in assets.templates:
        existing = existing_templates.get(template.ref)
        if existing is not None:
            incoming_hash = sha256_hex(template.payload)
            if existing.source_hash != incoming_hash:
                raise EngineError(
                    ErrorCode.VERSION_CONFLICT,
                    f"库内同版本模板与发行版演示原件不同：{template.ref}",
                    where={
                        "template_ref": template.ref,
                        "existing_source_hash": existing.source_hash,
                        "incoming_source_hash": incoming_hash,
                    },
                    remediation=[
                        "保留用户模板，另用独立存储根创建演示项目",
                        "或核对发行版资产，并在模板库显式处理版本冲突后重试",
                    ],
                )
            if existing.deprecated or not existing.parsed:
                raise _asset_error(f"演示模板不可用：{template.ref}")
            continue
        imported = services.import_template(
            filename=template.filename,
            payload=template.payload,
            template_id=template.template_id,
            version=template.version,
            doc_type=template.doc_type,
            notes=template.notes,
            author=DEMO_AUTHOR,
        )
        if imported.package_dir is None:
            raise _asset_error(f"演示模板未能解析出模板包：{template.ref}")
        services.register_template(imported.import_id, operator=DEMO_AUTHOR)

    existing_domains = {entry.kind: entry for entry in services.list_domain_models()}
    for model in assets.domains:
        existing_domain = existing_domains.get(model.kind)
        if existing_domain is not None:
            if existing_domain.deprecated:
                raise _asset_error(f"演示领域模型已废弃：{model.kind}")
            if existing_domain.fingerprint == fingerprint_domain_model(model):
                continue
        services.register_domain_model(model=model, author=DEMO_AUTHOR)

    profile_entries = services.list_profiles()
    existing_profiles = {entry.ref: entry for entry in profile_entries}
    active_profiles = {entry.profile_id: entry for entry in profile_entries if not entry.deprecated}
    template_refs = [entry.ref for entry in services.list_templates(include_deprecated=False)]
    for profile in assets.profiles:
        ref = f"{profile.profile_id}@{DEFAULT_PROFILE_VERSION}"
        fingerprint = fingerprint_profile(profile).digest
        active_profile = active_profiles.get(profile.profile_id)
        # 示例配置用裸标识；必须核对构建实际会解析的最新有效版本。
        if active_profile is not None and active_profile.fingerprint != fingerprint:
            raise EngineError(
                ErrorCode.VERSION_CONFLICT,
                f"有效档案与发行版演示体例不同：{active_profile.ref}",
                where={"profile_id": profile.profile_id, "active_ref": active_profile.ref},
                remediation=[
                    "保留用户档案，另用独立存储根创建演示项目",
                    "或在档案库显式处理版本冲突后重试",
                ],
            )
        existing_profile = existing_profiles.get(ref)
        if existing_profile is not None:
            if existing_profile.deprecated:
                raise _asset_error(f"演示档案已废弃：{ref}")
            if existing_profile.fingerprint == fingerprint:
                continue
        services.register_profile(
            document=profile,
            author=DEMO_AUTHOR,
            known_template_refs=template_refs,
            domain_models=services.domain_declarations(),
        )


def _put_project_data(services: Services, project_id: str, assets: _DemoAssets) -> ProjectConfig:
    storage_root = services.settings.storage_root.resolve()
    target_root = safe_join(
        storage_root, DEMO_PROJECTS_DIR, path_segment(project_id, field="project_id"), "data"
    )
    try:
        for source in assets.sources:
            target = ensure_within(target_root, target_root / source.relative_path)
            # 部分初始化后的重试保留项目自己的修改；完成后的重试在入口直接返回配置。
            if not target.exists():
                atomic_write_bytes(target, source.payload)
            services.add_source(
                project_id,
                config=source.config.model_copy(
                    update={"root": target.relative_to(storage_root).as_posix()}
                ),
                author=DEMO_AUTHOR,
            )
    except OSError as exc:
        raise _asset_error("演示来源无法写入项目目录；检查存储卷写权限", path=target_root) from exc
    config = assets.project.model_copy(update={"project_id": project_id, "author": DEMO_AUTHOR})
    return services.put_config(project_id, config, author=DEMO_AUTHOR).config


def create_demo_project(services: Services, *, name: str) -> Project:
    """创建可构建的演示项目；资产缺失时在创建项目之前给出补救动作。"""
    assets = _load_assets(services)
    _register_assets(services, assets)
    project = services.create_project(name=name, demo=True)
    _put_project_data(services, project.project_id, assets)
    return project


def initialize_demo_project(services: Services, project_id: str) -> ProjectConfig:
    """幂等初始化：已有配置原样返回，保留用户编辑与历史配置版本。"""
    project = services.repository.get_project(project_id)
    versions = services.list_config_versions(project_id)
    if versions:
        return services.get_config(project_id)
    assets = _load_assets(services)
    _register_assets(services, assets)
    config = _put_project_data(services, project_id, assets)
    if not project.demo:
        services.repository.save_project(replace(project, demo=True, updated_at=services.now()))
    return config
