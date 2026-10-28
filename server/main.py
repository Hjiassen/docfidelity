"""应用工厂（DD-13.1 / DD-13.7 / DD-18.1 的部署面）。

**用工厂而不是模块级 ``app = FastAPI()``**：模块级单例让"两个互不干扰的实例"
（正常路径 / 阶段缺失路径、内存仓储 / 落库仓储）无法共存，测试只能靠清场，
而清场漏一处就变成偶发失败。

启动时做四件事，都不该让进程起不来
---------------------------------
1. **建工作区根目录**（不存在就建，包含父目录）。
2. **首启装载示例模板**（``seed_demo_templates=True`` 时才做，见 :mod:`server.seed`）。
3. **恢复事件游标**：把 ``jobs/*/events.jsonl`` 读回来重建内存索引，
   ``Last-Event-ID`` 跨重启才有效（DD-13.4 的"断线可回放"）。
4. **起 worker 线程**（可关）：把排队中的作业推进。

第 2、3 步的失败**都不阻断启动**（示例资产坏了、某个作业的事件文件坏了，都不该导致整个
服务起不来），但都会记 warning——而且那部分能力确实已经不可信了。

部署形态（DD-18.1）
------------------
同一颗进程按配置扮演两种角色：``df-api``（托管前端 + 提供接口）与 ``df-worker``
（只消费作业，见 :mod:`server.worker`）。前端静态资源由 ``df-api`` **同源托管**
（:mod:`server.static_site`），故浏览器侧不存在跨域这一步。

数据边界（DD-13.7）
------------------
本模块只做启动装配；边界的**可见性**由 ``GET /settings`` 的 ``data_boundary`` 承担，
边界的**强制**由"密钥只从环境变量读、且响应模型里没有密钥字段"承担。
"""

from __future__ import annotations

import logging
import tempfile
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Final

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from docfidelity.contracts.generation import ContentGenerator
from docfidelity.pipeline import EventLog, StageRegistry

from .api import register
from .api.deps import AppState, build_state
from .api.handlers import TRACE_ATTRIBUTE, TRACE_HEADER
from .errors import new_trace_id
from .jobs import SessionFactory, WorkerLoop
from .seed import SeedOutcome, seed_demo_templates
from .settings import ServerSettings
from .static_site import attach as attach_static_site
from .storage import Repository

__all__ = ["APP_STATE_ATTRIBUTE", "API_TITLE", "create_app"]

_LOGGER = logging.getLogger(__name__)

#: 应用状态在 ``app.state`` 上的属性名（与 ``api.deps`` 共用同一常量口径）。
APP_STATE_ATTRIBUTE: Final = "docfidelity_state"

#: 首启装载结果在 ``app.state`` 上的属性名。它是**运行期事实**（这台机器上这次装载
#: 成功没有），不属于 :class:`~server.api.deps.AppState` 的装配面，故单独放。
SEED_STATE_ATTRIBUTE: Final = "docfidelity_demo_seed"

API_TITLE: Final = "docfidelity API"
API_DESCRIPTION: Final = (
    "模板与体例驱动的通用文档撰写引擎。\n\n"
    "- 接口形状为 **Stable**（DD-13.2）：新增字段允许，删除/改名须走弃用流程。\n"
    "- 全部失败共用一份错误响应包：`code` / `message` / `where` / `remediation` / `trace_id`。\n"
    "- `remediation` 是给使用者看的可执行动作，界面**按它渲染**，不自行推断。\n"
    "- 界面不承载业务判断：作业状态、聚合进度、续跑点一律由服务端返回。"
)

_API_VERSION: Final = "0.0.1"


def _repository_readable(state: AppState) -> dict[str, Any]:
    """仓储探活：一次**只读**列表。写探测会污染数据，建连接探测看不出 schema 旧。"""
    try:
        count = len(state.repository.list_projects())
    except Exception as exc:  # 就绪探针的职责是报告，不是把故障变成 5xx 栈
        return {"ok": False, "detail": f"{type(exc).__name__}: {exc}"}
    return {"ok": True, "detail": f"projects={count}"}


def _workspace_writable(root: Path) -> dict[str, Any]:
    """工作区可写探测：真写一个临时文件再删。

    不查权限位：``os.access`` 在 Windows 上对 ACL 不可靠，而"能建目录"与"能写文件"
    也是两件事（只读挂载 + 已有目录同时成立）。真写一次是唯一没有歧义的判据。
    """
    try:
        with tempfile.NamedTemporaryFile(dir=root, prefix=".readyz-", suffix=".tmp"):
            pass
    except OSError as exc:
        return {"ok": False, "detail": f"{type(exc).__name__}: {exc}"}
    return {"ok": True, "detail": str(root)}


def create_app(
    *,
    settings: ServerSettings | None = None,
    repository: Repository | None = None,
    stages: StageRegistry | None = None,
    root: Path | str | None = None,
    events: EventLog | None = None,
    decide_session: SessionFactory | None = None,
    now: Callable[[], datetime] | None = None,
    start_worker: bool | None = None,
    content_generator: ContentGenerator | None = None,
) -> FastAPI:
    """构造应用。

    Parameters
    ----------
    stages:
        阶段实现注册表。为 ``None`` 时按 ``docfidelity.build_stages`` 入口点发现；
        发现不到就是空注册表——应用照常启动，``/health`` 会报出缺哪些阶段，
        ``POST /jobs`` 会以 ``E_PRECHECK_FAILED`` 明确拒绝（见 ``api.deps`` 的说明）。
    decide_session:
        决策会话工厂（``(作业, 路由配置, 事件日志) -> DecisionSession``）。为 ``None``
        时按配置推（``api.deps.session_factory_from``）：``model_enabled`` +
        ``model_base_url`` + ``model_name`` 三样齐备就挂真模型，否则确定性会话
        ——全链路走规则默认值、产出完整、token 为 0，这是"关闭模型"该有的样子，
        不是"缺一块"。传一个工厂是**覆盖**，部署方用它换适配层 / 加基线 / 挂落库镜像。
    start_worker:
        是否起后台 worker 线程。为 ``None`` 时取 ``settings.start_worker``（部署配置），
        测试一般显式传 ``False`` 并自己调 ``run_until_idle()``，免得断言与后台线程赛跑。
    """
    resolved = settings or ServerSettings()
    state: AppState = build_state(
        settings=resolved,
        repository=repository,
        stages=stages,
        root=root,
        events=events,
        decide_session=decide_session,
        now=now,
        content_generator=content_generator,
    )
    worker = (
        WorkerLoop(state.runner, additional_runner=state.document_runs.run_once)
        if _should_start_worker(resolved, start_worker)
        else None
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if resolved.seed_demo_templates:
            seed = seed_demo_templates(state.services, root=resolved.demo_templates_dir)
            setattr(app.state, SEED_STATE_ATTRIBUTE, seed)
            for failure in seed.errors:
                _LOGGER.warning("示例模板装载未全部成功：%s", failure)
        try:
            restored = state.runner.restore_events()
            if restored:
                _LOGGER.info("已恢复 %d 个作业的事件游标", restored)
        except Exception:  # 事件文件坏了不该让服务起不来
            _LOGGER.warning("恢复事件游标失败；相关作业的重连补推将不可用", exc_info=True)
        missing = state.runner.registry.missing()
        if missing:
            # 记为 warning 而不是 error：这不是本服务的问题，是"还没接构建阶段"，
            # 而且 /health 会把它暴露给部署者。
            _LOGGER.warning("流水线阶段未注册，构建将无法发起：%s", sorted(missing))
        if worker is not None:
            worker.start()
        try:
            yield
        finally:
            if worker is not None:
                worker.stop()

    app = FastAPI(
        title=API_TITLE,
        version=_API_VERSION,
        description=API_DESCRIPTION,
        lifespan=lifespan,
        openapi_tags=[
            {"name": "projects", "description": "项目与配置"},
            {"name": "uploads", "description": "上传收件（四道闸门）"},
            {"name": "templates", "description": "模板入库与模板库（P05 / P04）"},
            {
                "name": "domain-models",
                "description": "领域模型库（档案 for_each 引用的类型词汇表）",
            },
            {"name": "profiles", "description": "文档类型档案（登记 / 详情 / 导出 / 废弃）"},
            {"name": "sources", "description": "事实源声明、试接入与条目浏览"},
            {"name": "jobs", "description": "作业、事件流与产物"},
            {"name": "decisions", "description": "人工裁决与待办聚合（P10 / P12）"},
            {"name": "system", "description": "设置、数据边界与保留清理"},
        ],
    )
    setattr(app.state, APP_STATE_ATTRIBUTE, state)
    app.state.worker = worker

    @app.middleware("http")
    async def _trace(request: Request, call_next: Any) -> Any:
        """为每个请求打一个 trace id，并回写到响应头。

        回写是必须的：使用者看到错误时唯一能提供的定位线索就是这个 id ——
        没有它，"日志里有这条吗"这个问题就只能靠时间戳猜。
        """
        trace_id = new_trace_id()
        setattr(request.state, TRACE_ATTRIBUTE, trace_id)
        response = await call_next(request)
        response.headers[TRACE_HEADER] = trace_id
        return response

    @app.get("/health", tags=["system"], summary="健康检查（含流水线就绪状态）")
    def health(request: Request) -> dict[str, Any]:
        registry = state.runner.registry
        return {
            "status": "ok",
            "storage_root": str(state.root),
            "repository": type(state.repository).__name__,
            "stages": {
                "registered": list(registry.registered()),
                "missing": list(registry.missing()),
            },
            # 就绪与否由服务端给结论，界面不自己算（NFR-22）。
            "build_ready": not registry.missing(),
            "model_enabled": state.settings.model_enabled,
            "worker_running": bool(worker is not None and worker.running),
            # 「本进程有没有在消费作业」与「这个部署有没有消费者」是两件事：后者由
            # df-worker 那个独立服务承担（DD-18.1），本进程关掉 worker 不代表没人跑作业。
            "worker_mode": "in-process" if worker is not None else "external",
            # 首启装载的结果。**没配就不出现**：一个恒为 null 的字段会被读成"装载失败了"。
            "demo_seed": _seed_view(getattr(request.app.state, SEED_STATE_ATTRIBUTE, None)),
        }

    @app.get("/healthz", tags=["system"], summary="存活探针（进程还在跑吗）")
    def healthz() -> dict[str, str]:
        """进程级探针：**不碰磁盘、不碰库**。

        与 ``/readyz`` 分开是必需的：两者失败对应的处置完全相反。存活探针失败 =
        重启进程；就绪探针失败 = 从负载均衡里摘掉、等依赖恢复。合成一个的后果是
        数据库抖一下所有实例被重启一遍。
        """
        return {"status": "alive"}

    @app.get("/readyz", tags=["system"], summary="就绪探针（仓储 + 工作区 + 阶段）")
    def readyz() -> JSONResponse:
        """就绪 = **能接受一次构建请求**。

        三项都是"缺了就不能干活"：库读不了（作业登记不上）、工作区写不了（产物落不下）、
        阶段没注册（``POST /jobs`` 必然被拒）。

        **模型不在其中**：它缺了会退回确定性会话，产出依然完整（NFR-13 的硬规则 9）。
        把可选能力写进就绪判定，会让"没接模型"这个正常部署被编排器判成不健康。
        示例装载同理——它是演示资产，不是服务能力。
        """
        registry = state.runner.registry
        missing = sorted(registry.missing())
        checks: dict[str, dict[str, Any]] = {
            "repository": _repository_readable(state),
            "workspace": _workspace_writable(state.root),
            "stages": {"ok": not missing, "detail": f"missing={missing}"},
        }
        ready = all(item["ok"] for item in checks.values())
        return JSONResponse(
            status_code=200 if ready else 503,
            content={
                "status": "ready" if ready else "not-ready",
                "checks": checks,
                "model_enabled": state.settings.model_enabled,
                "worker_mode": "in-process" if worker is not None else "external",
            },
        )

    register(app)
    # **必须最后挂**：``Mount("/")`` 匹配一切，先挂它后面的接口就永远轮不上
    # （见 ``server.static_site.attach``）。
    static_root = resolved.static_root
    if static_root is not None:
        attach_static_site(app, static_root)
        _LOGGER.info("已托管前端静态资源：%s", static_root)
    else:
        _LOGGER.info(
            "未发现前端构建产物（%s），本进程只提供接口", resolved.static_dir or "web/dist"
        )
    return app


def _should_start_worker(settings: ServerSettings, explicit: bool | None) -> bool:
    """本进程要不要起 worker 线程：显式参数 > 部署配置。

    显式参数优先是刻意的——测试与嵌入方**必须**能无视部署配置把后台线程关掉，
    否则一份写着 ``DOCFIDELITY_START_WORKER=true`` 的 ``.env`` 会让"关掉线程再断言"
    变成不可能，而那种失败看起来像随机。
    """
    return settings.start_worker if explicit is None else explicit


def _seed_view(seed: SeedOutcome | None) -> dict[str, Any] | None:
    """首启装载结果的可读视图；没做装载时为 ``None``（见 ``/health`` 的注释）。"""
    if seed is None:
        return None
    return {
        "skipped": seed.skipped,
        "seeded": [item.ref for item in seed.seeded],
        "errors": list(seed.errors),
    }
