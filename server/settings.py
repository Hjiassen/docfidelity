"""服务端设置（DD-13.6 / DD-13.7 的数据边界落实）。

三条纪律
--------
1. **密钥只从环境变量 / 服务端配置文件读**，绝不进代码、也绝不出现在任何响应里
   （:meth:`ServerSettings.data_boundary` 只回 ``base_url`` 与 ``model``）。故密钥字段用
   ``SecretStr``——它连 ``repr`` 都是打码的，日志/异常里顺手带出去的可能性被消掉。
2. **``model_enabled=False`` 时全链路纯确定性**（NFR-13）：此时连出口白名单都是空的，
   ``external_calls`` 必为 ``False``。这不是"配置项"，是数据边界的可验证事实。
3. **保留策略按三类分别计日**（上传件 / 工作区 / 产物），因为它们的价值衰减速度不同：
   上传件过期即无用，产物要留到复核。合成一个天数会让其中一类总是过长或过短。

本模块**只声明配置**，既不做清理（那是 :mod:`server.retention`）、也不做校验
（那是 :mod:`server.uploads`）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Final, Literal
from urllib import request as urlrequest

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from docfidelity.contracts.config import DataBoundary, RetentionPolicy

__all__ = [
    "DEFAULT_ALLOWED_UPLOAD_SUFFIXES",
    "DEFAULT_MAX_UPLOAD_BYTES",
    "DETERMINISTIC_REASON",
    "ENV_PREFIX",
    "FORCED_DETERMINISTIC_REASON",
    "IMPORTS_DIRNAME",
    "ServerSettings",
    "TEMPLATES_DIRNAME",
    "default_model_prober",
]

#: 环境变量前缀。密钥（``DOCFIDELITY_MODEL_API_KEY``）只经此进入进程。
ENV_PREFIX: Final = "DOCFIDELITY_"

#: 模板导入暂存目录名（``<storage_root>/imports``）。
IMPORTS_DIRNAME: Final = "imports"

#: 模板库目录名（``<storage_root>/templates``）。
TEMPLATES_DIRNAME: Final = "templates"

#: 档案库目录（``profiles/<profile_id>/<version>/``）。与模板库同理：它是历史产物的
#: 引用键，**不受保留策略管辖**——按天数清理会让"上个月的文档用的哪份体例"变成
#: 无法回答的问题。
PROFILES_DIRNAME: Final = "profiles"

#: 模型未启用时给界面的固定说明（界面直接显示，不自行拼字符串）。
DETERMINISTIC_REASON: Final = "模型未启用，全链路为确定性模式"

#: 确定性模式挡下 Office 域刷新时的说明。
#:
#: 与 :data:`DETERMINISTIC_REASON` 分开是必需的：两者的**处置完全不同**——前者是
#: "没有模型，本来就不外呼"，后者是"你显式开了开关，但缺省姿态把它挡回去了"。
#: 合成一句话，用户会去查"为什么模型没生效"，而真正该做的是改姿态。
FORCED_DETERMINISTIC_REASON: Final = (
    "确定性姿态（DOCFIDELITY_DETERMINISTIC_MODE）已关闭本机 Office 参与，"
    "产物页码需在 Word 中刷新"
)

#: 探测超时（秒）。短超时是刻意的：这个端点回答"能不能连上"，不回答"快不快"。
PROBE_TIMEOUT_SECONDS: Final = 3.0

#: 上传大小上限（64 MiB）。模板包是 zip，正常在个位数 MiB；上限存在的意义是挡住
#: "拖死 worker"的畸形文件（DD-13.5），不是限制正常用法。
DEFAULT_MAX_UPLOAD_BYTES: Final = 64 * 1024 * 1024

#: 允许的上传后缀。**PDF 也算**：目检需要上传 pdf 对照件（DD-9.6）。
DEFAULT_ALLOWED_UPLOAD_SUFFIXES: Final[tuple[str, ...]] = (".docx", ".pdf", ".zip")


class ServerSettings(BaseSettings):
    """服务端运行配置。全部字段可由 ``DOCFIDELITY_*`` 环境变量覆盖。"""

    model_config = SettingsConfigDict(
        env_prefix=ENV_PREFIX,
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ------------------------------------------------------------------ 存储
    storage_root: Path = Field(
        default=Path("var"), description="工作区与产物的根目录；上传目录默认在其下"
    )
    uploads_dirname: str = Field(default="uploads", description="上传件目录（相对根）")
    database_url: str | None = Field(
        default=None,
        description=(
            "元数据库连接串（DD-16.2 的 `DF_DB_URL`）。**为空即用进程内仓储**——"
            "单机演示与测试因此零配置、且不落盘；自部署改成 "
            "`sqlite:///./data/df.db`（或 postgresql://…）即切到落库。"
            "「为空」与「配了 sqlite」是两件事：前者进程重启即丢作业，后者不会。"
        ),
    )

    # -------------------------------------------------------------- 数据边界
    deployment: Literal["local", "intranet", "public"] = Field(
        default="local", description="部署形态；仅用于界面展示数据边界（DD-13.7）"
    )
    model_enabled: bool = Field(default=False, description="模型总开关；False 即全链路纯确定性")
    model_base_url: str | None = Field(
        default=None, description="模型服务地址；**唯一的出口白名单条目**，可空"
    )
    model_name: str | None = Field(default=None, description="默认模型标识")
    model_api_key: SecretStr | None = Field(default=None, description="密钥；永不返回")

    # ------------------------------------------------------------ 确定性姿态
    deterministic_mode: bool = Field(
        default=True,
        description=(
            "纯确定性姿态（DD-16.2 的 `DF_DETERMINISTIC_MODE`，CI 默认 true）。"
            "**它不是总开关，而是缺省姿态**：只在调用方没有明确表态时生效——目前管的是"
            "「要不要让本机 Office 参与构建」。本机装没装 Word 会改变产物字节（页码刷没刷），"
            "把它留给运行环境会让同一份输入在两台机器上产出不同的产物（NFR-02）。"
            "显式配置优先：`office_enabled=true` 时本项不生效。"
        ),
    )
    office_enabled: bool | None = Field(
        default=None,
        description=(
            "本机 Word 域刷新（DD-16.2 的 `DF_OFFICE_ENABLED`）：`None` = auto（按 "
            "`deterministic_mode` 定），`true` / `false` = 显式表态。"
            "它决定产物里的目录页码是真实值还是模板占位值，故会进构建输入快照。"
        ),
    )

    # ---------------------------------------------------------------- 上传
    max_upload_bytes: int = Field(default=DEFAULT_MAX_UPLOAD_BYTES, gt=0)
    allowed_upload_suffixes: tuple[str, ...] = Field(
        default=DEFAULT_ALLOWED_UPLOAD_SUFFIXES, min_length=1
    )

    # ---------------------------------------------------------------- 保留
    retention_uploads_days: int = Field(default=30, ge=1)
    retention_workspace_days: int = Field(default=30, ge=1)
    retention_artifacts_days: int = Field(default=180, ge=1)

    # ------------------------------------------------------------ 实时与并发
    sse_heartbeat_seconds: float = Field(default=15.0, gt=0)
    sse_timeout_seconds: float = Field(default=45.0, gt=0)
    worker_concurrency: int = Field(default=2, ge=1)
    project_lock_ttl_seconds: float = Field(default=900.0, gt=0)

    # ---------------------------------------------------------------- 部署
    # DD-18.1 的三件事在这里各有一个开关。它们**都是部署选择**，故走环境变量而不是
    # 代码分支：镜像里那份 `.env` 决定形态，同一份代码在本机与容器里行为一致。
    static_dir: Path | None = Field(
        default=None,
        description=(
            "前端构建产物目录。为 `None` 时按 `web/dist` 探测：**存在就由本进程托管**，"
            "不存在就只提供 API。同一颗进程两种形态，是为了让开发期（Vite dev server 在前）"
            "与容器内（前端与 API 同源）都不必改一行代码——写死一个目录会让缺了它的部署"
            "在导入期就崩，而那时报的错与『前端还没构建』完全不像。"
        ),
    )
    start_worker: bool = Field(
        default=True,
        description=(
            "是否在本进程内起后台 worker 线程。单机一键体验用默认值即可；"
            "`docker compose` 里另起 `df-worker` 时把它关掉"
            "（`DOCFIDELITY_START_WORKER=false`）。留着也能跑（项目锁保证同项目串行），"
            "但两个消费者抢同一队列会让『worker 到底在哪台机器上』变成一个无法回答的问题。"
        ),
    )
    seed_demo_templates: bool = Field(
        default=False,
        description=(
            "首启装载 `demo-templates/`（DD-18.1，验收 31 一键体验）。**默认关**：本机开发时"
            "它会把两个示例模板悄悄塞进模板库，而『库里凭空多了两个包』没有任何界面动作与之"
            "对应。容器镜像由 `deploy/Dockerfile` 显式打开。"
        ),
    )
    demo_templates_dir: Path = Field(
        default=Path("demo-templates"),
        description="示例模板包目录（`seed_demo_templates` 的来源）。",
    )

    # ---------------------------------------------------------------- 派生
    @property
    def static_root(self) -> Path | None:
        """实际托管的前端目录；不存在即 ``None``（= 本进程只提供 API）。

        **不在这里建目录**：托管一个空目录会让 ``GET /`` 回 404 而不是回一句
        "前端还没构建"，而后者才是可执行的线索。
        """
        candidate = Path("web") / "dist" if self.static_dir is None else Path(self.static_dir)
        return candidate if candidate.is_dir() else None

    @property
    def uploads_root(self) -> Path:
        """上传目录（与工作区**同根**，故 DD-13.5 的"跨卷先落临时再原子移动"不触发）。"""
        return self.storage_root / self.uploads_dirname

    @property
    def imports_root(self) -> Path:
        """模板导入的**暂存**根目录（``imports/<import_id>/``）。

        与工作区同根、且**不由保留策略管辖**：一份没登记完的导入还带着"稍后重试解析"
        的承诺，把它按天数清掉会让那条承诺失效。暂存目录的清理是独立的一轮
        （见 ``server/templates.py`` 的模块文档）。
        """
        return self.storage_root / IMPORTS_DIRNAME

    @property
    def templates_root(self) -> Path:
        """模板库根目录（``templates/<template_id>/<version>/``）。

        **不受保留策略管辖**：模板版本是历史产物的引用键，按天数清理会让"上个月的
        文档是怎么生成的"变成无法回答的问题。
        """
        return self.storage_root / TEMPLATES_DIRNAME

    @property
    def profiles_root(self) -> Path:
        """档案库根目录（``profiles/<profile_id>/<version>/``）。

        **不受保留策略管辖**：档案版本是历史产物的引用键（``DocumentEntry.profile_id``
        指向它），清理它等于让"这份文档是按哪份体例产出的"永久失去答案。
        """
        return self.storage_root / PROFILES_DIRNAME

    def retention_policy(self) -> RetentionPolicy:
        return RetentionPolicy(
            uploads_days=self.retention_uploads_days,
            workspace_days=self.retention_workspace_days,
            artifacts_days=self.retention_artifacts_days,
        )

    # ------------------------------------------------------------ 确定性派生
    def allow_office(self) -> bool:
        """本机 Office 是否参与构建（决定目录页码刷不刷）。

        三级：显式表态 > 确定性姿态 > 探测。**显式优先**是刻意的——`office_enabled`
        是"我就是要这台机器的 Word"，而 `deterministic_mode` 是"没特别说就别依赖
        本机软件"。反过来（确定性压过显式）会让 `DOCFIDELITY_OFFICE_ENABLED=true`
        变成一句不起作用的配置，而那正是"配了但没生效"的典型来源。
        """
        if self.office_enabled is not None:
            return self.office_enabled
        return not self.deterministic_mode

    def office_reason(self) -> str | None:
        """``allow_office()`` 为假时的成因；``None`` = 没有关掉它。

        "关掉的"与"探测不到的"必须分开：前者去查配置，后者去查部署环境。冻结输入
        把本值一路带到渲染报告里，报告才不会把两者说成同一句话。
        """
        if self.allow_office():
            return None
        if self.office_enabled is False:
            return "配置显式关闭了本机 Office 参与（DOCFIDELITY_OFFICE_ENABLED=false）"
        return FORCED_DETERMINISTIC_REASON

    def data_boundary(self) -> DataBoundary:
        """数据边界只读视图（DD-13.7）。

        ``external_calls`` 由 ``model_enabled`` 与 ``model_base_url`` **共同**推出，
        而不是另立一个开关：另立开关就允许"声称无外部请求、实际有"这种自相矛盾的状态。
        """
        return DataBoundary(
            deployment=self.deployment,
            model_enabled=self.model_enabled,
            model_base_url=self.model_base_url,
            external_calls=bool(self.model_enabled and self.model_base_url),
            storage_path=str(self.storage_root),
        )


def default_model_prober(base_url: str) -> tuple[bool, str]:
    """轻量连通性探测：发一次 GET，只看"能不能建立连接"，不解析响应体。

    刻意不用 HTTP 客户端库：``server`` 的 extras 里没有 HTTP 客户端（DD 只列了
    FastAPI / SQLAlchemy / SSE 那一组），而这里需要的只是"连得上吗"。
    凭空引入一个请求库，只为发这一个探测，不值当。

    返回 ``(可达, 说明)``。失败不抛异常——这个端点的职责是**报告**连通性，
    而不是把连不上变成一次 5xx。
    """
    try:
        with urlrequest.urlopen(base_url, timeout=PROBE_TIMEOUT_SECONDS) as response:
            return True, f"HTTP {response.status}"
    except Exception as exc:  # 探测失败的具体原因不影响结论
        return False, f"{type(exc).__name__}: {exc}"
