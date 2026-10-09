"""构建输入快照的冻结（FR-M7-15 / DD-13.2 的 ``build_payload``）。

为什么单独成一个模块
--------------------
它是一块**自成一体的职责**：只回答"这次构建的输入是什么"，与"作业怎么排队、阶段怎么
推进、失败怎么隔离"没有关系。挤在 ``worker`` 里除了让那个文件越过行数上限（NFR-09），
还把两种改动混在一起——改调度策略的人会路过一段"档案怎么解析"的代码。

三个"不"（每一条都对应一个踩过的坑）
------------------------------------
① **不只给标识**。"编排只把上游快照原样递下去"仍然成立（快照里没有一样东西是阶段跑出来
   的），但只给 ``profile_id`` / ``source_ids`` 这几个**标识**，五个阶段体就会各自回仓储
   取一次件，于是"这次构建用的是哪一版档案"有了五个答案。冻成声明之后它只有一个：
   取件发生在发起时刻。
② **不抛异常**。它在阶段循环里被调用，而那一层没有 ``except``——异常会一路冒到调度循环
   的宽 ``except``（"已隔离"），结果是**作业永远停在 running**，界面上看不出区别。
   故解析不出的项冻成 ``None`` 并在事件流里留一条 ``log``；真正的失败由**阶段体**如实
   报出（那才是 ``run_stage`` 会接住并转成"失败阶段"的位置）。
③ **不自己拼资产路径**。模板包目录、原件位置、来源 ``root`` 的解析基点，拼法都是各库
   自己的事。尤其 ``source_root`` 必须与 ``server.sources`` 的"试接入"同一个口径
   （存储根），否则界面上"试接入通了"、构建却说找不到文件。
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict

from docfidelity.contracts.base import FormatModel
from docfidelity.contracts.config import DocumentEntry, ProjectConfig, RoutingConfig
from docfidelity.contracts.errors import EngineError, ErrorCode
from docfidelity.contracts.job import Job
from docfidelity.contracts.profile import DocTypeProfile
from docfidelity.pipeline import EventLog, Workspace

from ..domain_models import domain_declarations
from ..profiles import read_profile_document, resolve_profile
from ..settings import ServerSettings
from ..storage import Repository
from ..templates import resolve_template, template_source_path

__all__ = ["freeze_payload", "load_payload", "persist_payload"]

_PAYLOAD_PATH = "inputs/build_payload.json"


class _ModelPosture(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    enabled: bool
    base_url: str | None
    name: str | None


def load_payload(job: Job, workspace: Workspace) -> Mapping[str, Any] | None:
    """读取提交快照；缺失供历史作业兼容，损坏时拒绝复用。"""
    path = workspace.resolve(_PAYLOAD_PATH)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("快照必须是对象")
        if payload.get("job_id") != job.job_id or payload.get("inputs_hash") != job.inputs_hash:
            raise ValueError("快照与作业标识或输入摘要不一致")
        if payload.get("routing") is not None:
            RoutingConfig.model_validate(payload["routing"])
        _ModelPosture.model_validate(payload.get("model"))
    except (OSError, ValueError) as error:
        raise EngineError(
            ErrorCode.CONFIG_INVALID,
            "提交输入快照损坏，不能用最新配置替代历史输入",
            where={"job_id": job.job_id, "path": str(path)},
            remediation=["恢复该作业的输入快照，或提交一次新的构建"],
        ) from error
    return payload


def persist_payload(payload: Mapping[str, Any], workspace: Workspace) -> None:
    """原子保存输入声明；重试只清断点，不覆盖这份快照。"""
    workspace.put_json(_PAYLOAD_PATH, dict(payload))


@dataclass(frozen=True)
class _Context:
    """冻结时要用到的四样东西——省掉每个内部函数都带一串关键字参数。"""

    job: Job
    repository: Repository
    settings: ServerSettings
    events: EventLog


def freeze_payload(
    job: Job,
    *,
    repository: Repository,
    settings: ServerSettings,
    events: EventLog,
    config: ProjectConfig | None = None,
) -> Mapping[str, Any]:
    """冻结这次构建要用的**输入**：引用、声明本体，以及资产的路径。

    ``config`` 由提交方传入，与计算 ``inputs_hash`` 的配置一致；后续修改配置
    只影响新提交的作业。历史作业没有快照时，兼容读取一次当前配置。
    """
    context = _Context(job=job, repository=repository, settings=settings, events=events)
    config = config if config is not None else repository.latest_config(job.project_id)
    entry = config.document(job.doc_key) if config is not None and job.doc_key else None

    profile = _freeze_profile(context, entry)
    template = resolve_template(repository, entry.template_ref) if entry else None
    if entry is not None and entry.template_ref and template is None:
        _note_unresolved(context, "template_ref", entry.template_ref)
    docx = template_source_path(template) if template is not None else None
    return {
        "job_id": job.job_id,
        "parent_job_id": job.parent_job_id,
        "project_id": job.project_id,
        "doc_key": job.doc_key,
        "doc_type": job.doc_type,
        # —— 引用：原样保留。界面、错误载荷与前置检查都按这些键说话 ——
        "template_ref": entry.template_ref if entry is not None else None,
        "profile_id": entry.profile_id if entry is not None else None,
        "source_ids": list(entry.source_ids) if entry is not None else [],
        "overrides_ref": entry.overrides_ref if entry is not None else None,
        "config_version": config.version if config is not None else None,
        "inputs_hash": job.inputs_hash,
        "retention": settings.retention_policy().model_dump(mode="json"),
        "routing": _dump(config.routing) if config is not None else None,
        # 只冻结非秘密模型姿态；独立 worker 的密钥仍来自其服务端设置。
        "model": {
            "enabled": settings.model_enabled,
            "base_url": settings.model_base_url,
            "name": settings.model_name,
        },
        # —— 冻结的声明：阶段体直接消费，不再回仓储取件 ——
        "profile": None if profile is None else profile.model_dump(mode="json"),
        "domain_models": [
            model.model_dump(mode="json") for model in domain_declarations(repository)
        ],
        "sources": _freeze_sources(context, entry),
        "terminology": _dump(config.terminology) if config is not None else {},
        "pagination": _dump(config.pagination) if config is not None else {},
        "rule_sets": [_dump(ref) for ref in (config.rule_sets if config is not None else [])],
        # —— 资产位置：阶段体不自己拼路径（拼法是各库自己的事）——
        # 未解析的模板**没有**契约，故把包目录留空让阶段体如实报缺
        # （``RegisteredTemplate.parsed`` 为假 = "只登记了原件、解析没成功"）。
        "template_package_dir": (
            template.package_dir if template is not None and template.parsed else None
        ),
        "template_docx": None if docx is None else str(docx),
        # 来源 ``root`` 的解析基点 = **存储根**，与 ``server.sources`` 的"试接入"
        # 同一个口径（``SourcesMixin._source_root``）。不共用它会得到一个很具体的
        # 坏结果：界面上"试接入通了"，构建却说找不到文件。
        "source_root": str(settings.storage_root),
        # —— Office 姿态：**必须冻进来**，不能让阶段体自己探测 ——
        # 本机装没装 Word 会改变产物字节（目录页码刷没刷），留给阶段体探测就等于
        # "产物是本机环境的函数"，同一份输入在两台机器上产出不同的产物（NFR-02）。
        # 姿态随时可改（`PUT /settings`），而冻结发生在发起时刻——与其余输入同理：
        # 这次构建用的是哪一版档案/哪个姿态，只有一个答案。
        "office_enabled": settings.allow_office(),
        "office_reason": settings.office_reason(),
    }


def _freeze_profile(context: _Context, entry: DocumentEntry | None) -> DocTypeProfile | None:
    """把配置里的档案引用解析成一份**本体**；解析不出返回 ``None``（见 :func:`freeze_payload`）。

    解析用 ``server.profiles.resolve_profile``——"裸 ``profile_id`` 落到哪一版"
    这条规则只有那一处实现（构建期与登记期的口径必须一致，否则会出现"报告说 A 版、
    产物按 B 版做"）。
    """
    if entry is None or not entry.profile_id:
        return None
    found = resolve_profile(context.repository, entry.profile_id)
    if found is None:
        _note_unresolved(context, "profile_id", entry.profile_id)
        return None
    try:
        return read_profile_document(found)
    except EngineError as error:
        # 本体文件丢失（存储根被人手改过）。**不吞**：原因原样进事件流，
        # 作业随后由阶段体以"输入里没有档案本体"失败——两条信息合起来才够定位。
        _note_unresolved(context, "profile_id", found.ref, detail=error.message)
        return None


def _freeze_sources(context: _Context, entry: DocumentEntry | None) -> list[dict[str, Any]]:
    """把 ``source_ids`` 解析成来源声明。解析不出的一条记进事件流并**略过**。

    略过而不是"整份快照作废"：一条来源被移走时，其余来源仍能被真实消费，
    而"少了哪一条"由事件流说清楚。硬要在这里失败，反而会让
    "配置引用了未登记的来源"与"来源接入失败"这两件事在界面上变成同一个样子。
    """
    if entry is None:
        return []
    frozen: list[dict[str, Any]] = []
    for source_id in entry.source_ids:
        record = context.repository.find_source(context.job.project_id, source_id)
        if record is None:
            _note_unresolved(context, "source_ids", source_id)
            continue
        frozen.append(_dump(record.config))
    return frozen


def _note_unresolved(context: _Context, field: str, ref: str, *, detail: str = "") -> None:
    """把"这项引用解析不出"写进事件流。

    留痕而不是静默跳过：冻结**不能抛**（见 :func:`freeze_payload`），若既不抛也不留痕，
    "配置引用了一个不存在的资产"就会表现为"文档里少了一章"。
    """
    context.events.emit(
        context.job.job_id,
        "log",
        {
            "level": "warning",
            "message": f"构建输入解析不出：{field}={ref!r}",
            "field": field,
            "ref": ref,
            "detail": detail,
        },
    )


def _dump(model: FormatModel) -> dict[str, Any]:
    """契约模型 → 可放进 ``payload`` 的 JSON 形状。

    ``mode="json"`` 是必须的：``payload`` 会被 :func:`~docfidelity.platform.hashing.digest_payload`
    与自己拼的 JSON 两种方式消费，带 ``datetime`` / ``Path`` 的 ``model_dump()``
    在其中一种里会炸，且报错位置离原因很远。
    """
    return model.model_dump(mode="json")
