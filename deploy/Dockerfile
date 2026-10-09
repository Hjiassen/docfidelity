# syntax=docker/dockerfile:1
#
# docfidelity 的容器镜像（DD-18.1）。
#
# 一个镜像两种角色：`df-api`（托管前端 + 提供接口）与 `df-worker`（只消费作业）。
# 两者只差启动命令，故**刻意不造两个镜像**——两个镜像会带来"worker 与 api 的阶段体
# 不是同一个版本"这类只在生产才暴露的问题，而它们的差别本来就只有一行 argv。
#
# Docker 实测与无 Docker 时的等价验证入口见 deploy/README.md。
#
# 数据边界（DD-10.1 / DD-13.7）
# ---------------------------
# 镜像里**不含任何真实模板与事实源**。装进去的只有两份自制示例模板（demo-templates/）
# 与两份示例输入（examples/data/）；真实部署应当把它们删掉或替换。

# ============================================================== 阶段 1：前端构建
FROM node:22-slim AS web
WORKDIR /build/web
# 先只拷依赖清单：源码改一行不该让 `npm ci` 这一层整个重跑。
COPY web/package.json web/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY web/ ./
# `npm run build` 含 `tsc --noEmit`：类型错误应当在**构建镜像**时就失败，
# 而不是等到运行时在浏览器里白屏。
RUN npm run build

# ============================================================== 阶段 2：运行时
FROM python:3.12-slim AS runtime

# Python 基线是 >=3.11,<3.13（pyproject.toml）。取 3.12：上限内最新，也是 CI 跑的那个。
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# `alembic.ini` 与迁移脚本**必须**一起进镜像：`migration_config()` 从
# `server/storage/engine.py` 往上两级（= /app）去找它，并据此解析迁移目录。
# 漏掉它的症状是"容器里跑迁移报找不到 alembic.ini"，而迁移在本地开发时从来不出错。
COPY pyproject.toml README.md LICENSE alembic.ini ./
COPY src/ ./src/
COPY server/ ./server/
# 装的是本发行版 + server extras（FastAPI / uvicorn / SQLAlchemy / Alembic / SSE）。
# **不含** `[office]`：内核的 Word 域刷新在 Linux 容器里本来就用不上，装了只是一份
# 用不到的依赖。缺它时域刷新会走既有降级路径（置 `w:updateFields`），不是"功能缺一块"。
RUN pip install --no-cache-dir ".[server]"

# 示例资产与前端产物。`web/dist` 由**同一进程**托管（server.static_site），
# 故容器里只有一个对外端口，也就不存在跨域这一步。
COPY demo-templates/ ./demo-templates/
COPY examples/ ./examples/
COPY --from=web /build/web/dist ./web/dist
COPY deploy/entrypoint.sh /usr/local/bin/docfidelity-entrypoint
RUN chmod 0755 /usr/local/bin/docfidelity-entrypoint

# 以非 root 运行。数据卷要**在这里就 chown**：若留给运行时由 root 建卷，第一次写工作区
# 就会 EACCES，而那个错出现在构建的第三个阶段，离"卷是 root 建的"很远。
RUN groupadd --system --gid 10001 df \
    && useradd --system --uid 10001 --gid df --home-dir /app df \
    && mkdir -p /data \
    && chown -R df:df /app /data

USER df

# 部署形态（DD-18.1）。逐条都有理由：
#   PYTHONPATH=/app            → `server/` 是**仓库根**的包，不随内核 wheel 分发
#                                （见 pyproject 的 `[tool.hatch.build.targets.wheel]`）。
#   STORAGE_ROOT=/data         → 数据边界落在这个卷上（compose 把 ./data 挂进来）。
#   DATABASE_URL=sqlite:////data/df.db
#                              → 元数据落库，重启后作业/产物登记仍在。**四个斜杠**：
#                                三个是 scheme 分隔，第四个是绝对路径。入口脚本会在
#                                启动前 `alembic upgrade head`（DD-13.6 的"不自动建表"）。
#   START_WORKER=false         → 默认按"另有 df-worker"的拓扑起；单容器想省掉那个服务，
#                                把它改成 true 即可（两种拓扑都成立）。
#   SEED_DEMO_TEMPLATES=true   → 首启把 demo-templates/ 装进模板库（验收 31 一键体验）。
#   DEPLOYMENT=intranet        → 数据边界面板显示"内网部署"，而不是用"local"糊过去。
#   DETERMINISTIC_MODE=true    → 容器里没有 Word，域刷新必然降级；显式钉住这个姿态，
#                                产物字节就不再取决于"跑在哪台机器上"（NFR-02）。
ENV PYTHONPATH=/app \
    DOCFIDELITY_STORAGE_ROOT=/data \
    DOCFIDELITY_DATABASE_URL=sqlite:////data/df.db \
    DOCFIDELITY_START_WORKER=false \
    DOCFIDELITY_SEED_DEMO_TEMPLATES=true \
    DOCFIDELITY_DEPLOYMENT=intranet \
    DOCFIDELITY_DETERMINISTIC_MODE=true

VOLUME ["/data"]
EXPOSE 8000

# 探针用 python 而不是 curl：slim 镜像里没有 curl，为了一个探针装一个 HTTP 客户端
# 不值当；而"装了 curl 却忘了 ca-certificates"是另一类偶发失败。
# 探针打的是 `/healthz`（进程活着吗），不是 `/readyz`：容器编排对失败容器的处置是
# **重启**，而"库连不上"重启一百次也不会好——那是要摘流量，不是要重启。
HEALTHCHECK --interval=15s --timeout=3s --start-period=30s --retries=3 \
    CMD ["python", "-c", "import sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2).status == 200 else 1)"]

ENTRYPOINT ["docfidelity-entrypoint"]
CMD ["uvicorn", "server.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
