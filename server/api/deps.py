"""依赖装配与注入（DD-13.1）。

**应用状态挂在 ``app.state``，不用模块级全局变量。** 理由不是风格：测试要能同时
存在两个互不干扰的 app 实例（一个跑正常路径、一个跑"阶段缺失"路径），
全局单例会逼着测试写清场逻辑，而清场漏一处就变成偶发失败。

阶段实现的装配有两条路（DD-13.1「界面需要的每一步能力都必须由 pipeline 暴露为
用例函数」的落地口径）：

1. **显式传** ``stages=StageRegistry(...)``——测试与嵌入方用这条；
2. **入口点发现**：``docfidelity.build_stages`` 组下每个入口点是一个
   ``register(registry) -> None`` 可调用对象。这颗内核的构建阶段体由谁提供是
   **部署选择**，不是本仓库的选择，所以走入口点而不是硬编码 import。

两条都没有 → 注册表为空。**这不让应用启动失败**：``/health`` 会把它报出来，
``POST /jobs`` 会以 ``E_PRECHECK_FAILED`` 明确拒绝（见 ``services._precheck_stages``）。
把"能启动但构建会失败"做成"启动就崩"，只会让部署者先去怀疑服务本身。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from importlib.metadata import entry_points
from pathlib import Path
from typing import Final

from fastapi import Request

from docfidelity.contracts.config import RoutingConfig
from docfidelity.contracts.job import Job
from docfidelity.pipeline import (
    BASELINE_FILENAME,
    DecisionSession,
    EventLog,
    StageRegistry,
    Workspace,
    build_session,
    create_model_adapter,
    model_session_factory,
)

from ..jobs import JobRunner, SessionFactory, WorkspaceEventLog
from ..jobs.payload import load_payload
from ..services import Services
from ..settings import ServerSettings
from ..storage import Repository, build_repository

__all__ = [
    "STAGE_PROVIDER_GROUP",
    "AppState",
    "build_state",
    "services_of",
    "session_factory_from",
    "settings_of",
    "state_of",
]

#: 阶段体提供者的入口点组名（与 ``docfidelity.qa_rules`` 同一套发现机制）。
STAGE_PROVIDER_GROUP: Final = "docfidelity.build_stages"

_STATE_ATTRIBUTE: Final = "docfidelity_state"


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


def load_stage_registry() -> StageRegistry:
    """从入口点组装阶段实现；没有任何提供者时返回空注册表（见模块文档）。"""
    registry = StageRegistry()
    for entry in entry_points(group=STAGE_PROVIDER_GROUP):
        provider = entry.load()
        provider(registry)
    return registry


def session_factory_from(settings: ServerSettings) -> SessionFactory | None:
    """按配置决定决策侧挂什么。``None`` = 确定性会话（``worker`` 的缺省路径）。

    **这是 ``settings.model_*`` 与模型之间唯一的连接**。没有它的话，``.env`` 配得再对
    也没有任何代码会去调模型——"启动时报就绪、构建时一次模型都没问"的形态不报错、
    不失败，只是每一项决策都静默走规则默认值，从界面与报告上完全看不出来。

    判定取"三样齐备"，理由各不相同：

    * ``model_enabled`` —— 总开关（NFR-13）。
    * ``model_base_url`` —— 与 :meth:`ServerSettings.data_boundary` 的 ``external_calls``
      **同一口径**：那里由"总开关 + 端点"共同推出"会不会外呼"，这里由同样两样推出
      "挂不挂适配层"。用同一个口径，才不可能出现"声称无外部请求、实际挂了模型"。
    * ``model_name`` —— 适配层多要的一样。缺它的后果不是"用默认模型"，而是
      **SDK 拿自己的默认模型去配你给的端点**（名字对不上，多数网关直接 4xx）；
      与其让这种配置半好不坏地跑起来，不如退回确定性会话。

    密钥只在**这一层**从 ``SecretStr`` 取出来（``get_secret_value()``），
    再交给 `model_session_factory`；``describe()`` / 日志 / 响应里永不出现它（NFR-21）。

    基线评测报告按**存储根下的固定文件名**给出（``<storage_root>/baseline.json``），
    由 `model_session_factory` 在**每个作业开跑时现读**：评测是运维动作，跑完就该对
    下一次构建生效。文件名只在 ``agents.baseline.BASELINE_FILENAME`` 定义一次
    （经 ``pipeline`` 的决策接缝转出），两处各写字面量会静默失联——报告换名之后
    服务端仍去找旧名字，症状是"评测跑了，界面却说没跑过"。
    """
    if not (settings.model_enabled and settings.model_base_url and settings.model_name):
        return None
    secret = settings.model_api_key
    return model_session_factory(
        model=settings.model_name,
        base_url=settings.model_base_url,
        api_key=None if secret is None else secret.get_secret_value(),
        baseline_path=settings.storage_root / BASELINE_FILENAME,
        adapter=create_model_adapter(
            model=settings.model_name,
            base_url=settings.model_base_url,
            api_key=None if secret is None else secret.get_secret_value(),
        ),
    )


def _submitted_session_factory(settings: ServerSettings, root: Path) -> SessionFactory:
    """按提交姿态选会话；客户端按模型配置复用，API 与独立 worker 口径一致。"""
    cached: dict[tuple[bool, str | None, str | None], SessionFactory | None] = {}

    def factory(job: Job, routing: RoutingConfig | None, events: EventLog) -> DecisionSession:
        payload = load_payload(job, Workspace(root, job.job_id))
        model = payload.get("model") if payload is not None else None
        posture = settings
        if isinstance(model, dict):
            posture = settings.model_copy(
                update={
                    "model_enabled": model["enabled"],
                    "model_base_url": model["base_url"],
                    "model_name": model["name"],
                }
            )
        key = (posture.model_enabled, posture.model_base_url, posture.model_name)
        if key not in cached:
            cached[key] = session_factory_from(posture)
        selected = cached[key]
        if selected is None:
            return build_session(routing, job_id=job.job_id, events=events, model_enabled=False)
        return selected(job, routing, events)

    return factory


@dataclass(frozen=True)
class AppState:
    """应用运行期状态。"""

    settings: ServerSettings
    repository: Repository
    runner: JobRunner
    services: Services

    @property
    def root(self) -> Path:
        return self.runner.root


def build_state(
    *,
    settings: ServerSettings | None = None,
    repository: Repository | None = None,
    stages: StageRegistry | None = None,
    root: Path | str | None = None,
    events: EventLog | None = None,
    decide_session: SessionFactory | None = None,
    now: Callable[[], datetime] | None = None,
) -> AppState:
    """组装状态。所有参数可注入，故测试不必碰真实磁盘或真实时间。

    ``decide_session`` 是**决策侧的组合根**（与 ``stages`` 之于渲染侧对称）：挂哪个
    适配层、用哪份基线评测、要不要加落库镜像，都是部署决策。**不传即按
    :func:`session_factory_from` 从配置推**：配置齐备就挂真模型，否则确定性会话——
    全链路走规则默认值、产出完整、token 为 0，这正是 ``DF_MODEL_ENABLED=false``
    该有的样子（验收 39），而不是"功能缺一块"。

    想强制确定性会话就改配置（``model_enabled=false``），**不要**靠传
    ``decide_session=lambda ...: rule_only_session(...)``：那样会得到"配置说接了模型、
    实际全走规则"的部署，而 ``/health`` 的数据边界仍报 ``external_calls=true``。
    """
    resolved_settings = settings or ServerSettings()
    # 仓储的选路在 ``storage.build_repository`` 一处收口：组装点只把"部署怎么配的"
    # 传下去，不需要认识 SQLAlchemy，也不需要知道"没配连接串意味着内存版"。
    resolved_repository = (
        repository if repository is not None else build_repository(resolved_settings.database_url)
    )
    registry = stages if stages is not None else load_stage_registry()
    resolved_root = resolved_settings.storage_root if root is None else Path(root)
    resolved_root.mkdir(parents=True, exist_ok=True)

    runner = JobRunner(
        repository=resolved_repository,
        registry=registry,
        settings=resolved_settings,
        root=resolved_root,
        events=events if events is not None else WorkspaceEventLog(resolved_root),
        decide_session=(
            decide_session
            if decide_session is not None
            else _submitted_session_factory(resolved_settings, resolved_root)
        ),
        now=now,
    )
    services = Services(
        repository=resolved_repository,
        runner=runner,
        settings=resolved_settings,
        now=now or _utcnow,
    )
    return AppState(
        settings=resolved_settings,
        repository=resolved_repository,
        runner=runner,
        services=services,
    )


def state_of(request: Request) -> AppState:
    """从 ``app.state`` 取应用状态；未装配说明应用工厂没被使用。"""
    state = getattr(request.app.state, _STATE_ATTRIBUTE, None)
    if not isinstance(state, AppState):  # pragma: no cover - 工厂必然已装配
        raise RuntimeError("应用状态缺失：请通过 server.main.create_app 构造应用")
    return state


def services_of(request: Request) -> Services:
    """FastAPI 依赖：用例面。"""
    return state_of(request).services


def settings_of(request: Request) -> ServerSettings:
    """FastAPI 依赖：设置。"""
    return state_of(request).settings
