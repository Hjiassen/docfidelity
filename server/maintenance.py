"""系统设置与保留清理（``Services`` 的维护职责）。

为什么是 mixin 而不是一组自由函数：这里每条用例都要读仓储、设置、运行器（工作区在哪），
改成自由函数就得把宿主的字段在调用点再传一遍，于是"改一处设置"变成"改所有调用点"。
mixin 让它们**就是**用例面的一部分——调用方写的仍是 ``services.cleanup(...)``，
只是实现换个文件放（NFR-09 的单文件行数上限）。

依赖的宿主字段在 ``TYPE_CHECKING`` 里声明：它们是 ``Services`` 的 dataclass 字段，
此处只为了让 mypy 有据可依。**不能**声明成真属性——mixin 不是 dataclass，写成真属性
既不会生效，又会与宿主的同名注解冲突。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from shutil import rmtree
from typing import TYPE_CHECKING, Any, Final

from docfidelity.contracts.errors import EngineError, ErrorCode
from docfidelity.contracts.job import Artifact
from docfidelity.pipeline import BASELINE_FILENAME, load_baseline_status, probe_grammar
from docfidelity.platform.fs import ensure_within, iter_files

from .jobs import JobRunner
from .retention import (
    RetainedObject,
    RetentionKind,
    RetentionPlan,
    expired_objects,
    plan_cleanup,
)
from .settings import DETERMINISTIC_REASON, ServerSettings
from .storage import Repository

__all__ = [
    "MUTABLE_SETTINGS_FIELDS",
    "SECRET_SETTINGS_FIELDS",
    "MaintenanceMixin",
]

#: ``PUT /settings`` 允许改的字段。**不含** ``storage_root`` / ``uploads_dirname``：
#: 运行期挪存储根会让已有工作区瞬间"消失"（它还在旧路径），这不是配置项而是数据迁移。
MUTABLE_SETTINGS_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "deployment",
        "model_enabled",
        "model_base_url",
        "model_name",
        "deterministic_mode",
        "office_enabled",
        "max_upload_bytes",
        "allowed_upload_suffixes",
        "retention_uploads_days",
        "retention_workspace_days",
        "retention_artifacts_days",
        "sse_heartbeat_seconds",
        "sse_timeout_seconds",
        "worker_concurrency",
        "project_lock_ttl_seconds",
    }
)

#: 密钥类字段，任何情况下都不经 API 读写（DD-13.7）。
SECRET_SETTINGS_FIELDS: Final[frozenset[str]] = frozenset({"model_api_key"})


class MaintenanceMixin:
    """设置读写、模型探测、保留清理。宿主 = :class:`server.services.Services`。"""

    if TYPE_CHECKING:
        repository: Repository
        runner: JobRunner
        settings: ServerSettings
        now: Callable[[], datetime]
        model_prober: Callable[[str], tuple[bool, str]]

    # ============================================================ 设置与维护
    def settings_view(self) -> dict[str, Any]:
        """``GET /settings`` 的响应。**永不包含密钥**（DD-13.7）。"""
        policy = self.settings.retention_policy()
        return {
            "data_boundary": self.settings.data_boundary().model_dump(mode="json"),
            "retention": policy.model_dump(mode="json"),
            "model": {
                "enabled": self.settings.model_enabled,
                "name": self.settings.model_name,
                "base_url": self.settings.model_base_url,
            },
            "uploads": {
                "max_bytes": self.settings.max_upload_bytes,
                "allowed_suffixes": list(self.settings.allowed_upload_suffixes),
            },
            "workers": {"concurrency": self.settings.worker_concurrency},
            # 确定性姿态的**生效值**（DD-16.3）。报生效值而不是开关值：用户配了
            # `model_enabled=true` 却发现没外呼时，要看的是"哪个姿态压住了它"，
            # 而不是再念一遍他刚才写的那个值。
            "deterministic": {
                "mode": self.settings.deterministic_mode,
                "office_enabled": self.settings.allow_office(),
                "office_reason": self.settings.office_reason(),
            },
            "mutable_fields": sorted(MUTABLE_SETTINGS_FIELDS),
        }

    def update_settings(self, patch: Mapping[str, Any]) -> ServerSettings:
        """就地更新设置（DD-13.2 的 ``PUT /settings``）。

        就地改而不是换成新对象：``runner`` 持有同一个实例的引用，换对象会让"改完设置
        但 worker 还用旧值"这种 bug 以"要重启两次才生效"的形式表现出来。
        """
        rejected = sorted(
            key
            for key in patch
            if key in SECRET_SETTINGS_FIELDS or key not in MUTABLE_SETTINGS_FIELDS
        )
        if rejected:
            raise EngineError(
                ErrorCode.CONFIG_INVALID,
                f"不允许通过接口修改这些设置：{rejected}",
                where={"rejected": rejected, "mutable": sorted(MUTABLE_SETTINGS_FIELDS)},
                remediation=[
                    "密钥仅从环境变量读取（DOCFIDELITY_MODEL_API_KEY）",
                    "存储根不可在运行期变更，请改配置文件后重启",
                ],
            )
        # 先按契约校验整份新配置，再逐字段写回——只改一半就报错会留下半新半旧的设置。
        candidate = ServerSettings.model_validate({**self.settings.model_dump(), **dict(patch)})
        for key in MUTABLE_SETTINGS_FIELDS:
            setattr(self.settings, key, getattr(candidate, key))
        return self.settings

    def probe_model(self) -> dict[str, Any]:
        """模型连通性（只读探测）。

        **未启用时不探测**并明确回 ``reachable=None``：把"没开"显示成"连不上"，
        会让人去查网络——而真正该做的是打开开关或什么都不做（NFR-13 的确定性模式）。
        """
        if not self.settings.model_enabled or not self.settings.model_base_url:
            return {
                "enabled": False,
                "base_url": self.settings.model_base_url,
                "reachable": None,
                "detail": DETERMINISTIC_REASON,
            }
        reachable, detail = self.model_prober(self.settings.model_base_url)
        return {
            "enabled": True,
            "base_url": self.settings.model_base_url,
            "reachable": reachable,
            "detail": detail,
        }

    def model_capabilities(self) -> dict[str, Any]:
        """模型侧的**能力现状**：受限解码 + 基线评测（DD-12.4 末段 / NFR-23）。

        与 :meth:`probe_model` 的分工是刻意的：那个回答"**连得上吗**"（一次出网探测），
        这个回答"**接上之后会怎么跑**"（本机有没有受限解码后端、基线数据在不在）。
        合成一张卡会让"连不上"与"没有受限解码"看起来是同一件事，而处置完全不同
        （查网络 / 装一个可选依赖或接受降级）。

        两件事实**不管模型开关**都照实报：它们描述的是**这台机器与这份存储**，
        而不是"这次构建会不会调模型"。开关状态由 ``enabled`` 单独给出，界面据此
        决定是否置灰（7.1.1 的"界面不自行判断能不能操作"）。

        ``baseline`` 走 ``load_baseline_status``，**那一个函数**同时给出"有没有、
        读不读得动、内容摘要"三态——构建接线（``session_factory_from`` 交给
        ``model_session_factory`` 的 ``baseline_path``）读的也是它。两处各判一次，
        会出现"设置页说没跑过评测、路由却按基线接管了"这种自相矛盾的显示。
        """
        grammar = probe_grammar()
        status = load_baseline_status(self.settings.storage_root / BASELINE_FILENAME)
        return {
            "enabled": bool(self.settings.model_enabled),
            "grammar": grammar.model_dump(mode="json"),
            "baseline": status.model_dump(mode="json"),
        }

    # ============================================================ 保留与清理
    def retained_objects(self) -> list[RetainedObject]:
        """当前受保留策略管辖的全部对象（清理候选列表的来源）。"""
        items: list[RetainedObject] = []
        for record in self.repository.list_uploads():
            items.append(
                RetainedObject(
                    object_id=record.upload_id,
                    kind="upload",
                    path=str(ensure_within(self.settings.storage_root, record.stored_path)),
                    size_bytes=record.receipt.size_bytes,
                    created_at=record.created_at,
                )
            )
        jobs_root = self.runner.root / "jobs"
        if jobs_root.is_dir():
            for directory in sorted(path for path in jobs_root.iterdir() if path.is_dir()):
                stat = directory.stat()
                items.append(
                    RetainedObject(
                        object_id=f"ws:{directory.name}",
                        kind="workspace",
                        path=str(directory),
                        size_bytes=sum(path.stat().st_size for path in iter_files(directory)),
                        created_at=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc),
                    )
                )
        for artifact in self._all_artifacts():
            items.append(
                RetainedObject(
                    object_id=artifact.artifact_id,
                    kind="artifact",
                    path=str(
                        self.runner.workspace(artifact.job_id).resolve(artifact.relative_path)
                    ),
                    size_bytes=artifact.size_bytes,
                    created_at=artifact.created_at,
                )
            )
        return items

    def _all_artifacts(self) -> list[Artifact]:
        found: list[Artifact] = []
        for job in self.repository.list_jobs():
            found.extend(self.repository.list_artifacts(job.job_id))
        return found

    def expired_now(self) -> list[RetainedObject]:
        """按当前策略已过期的对象（``GET /maintenance/expired`` 的响应）。"""
        return expired_objects(
            self.retained_objects(),
            policy=self.settings.retention_policy(),
            now=self.now(),
        )

    def cleanup(self, *, requested: Sequence[str]) -> RetentionPlan:
        """执行清理：先出计划，再逐项回收，最后摘掉登记。"""
        objects = self.retained_objects()
        plan = plan_cleanup(
            objects,
            requested=requested,
            policy=self.settings.retention_policy(),
            now=self.now(),
        )
        by_id = {item.object_id: item for item in objects}
        for item in plan.objects():
            path = ensure_within(self.settings.storage_root, by_id[item.object_id].path)
            self._reclaim(item.kind, item.object_id, path)
        return plan

    def _reclaim(self, kind: RetentionKind, object_id: str, path: Path) -> None:
        if kind == "upload":
            path.unlink(missing_ok=True)
            self.repository.remove_upload(object_id)
        elif kind == "artifact":
            path.unlink(missing_ok=True)
            self.repository.remove_artifact(object_id)
        elif path.is_dir():
            rmtree(path, ignore_errors=True)
