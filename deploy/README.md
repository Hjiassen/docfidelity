# 私有部署

默认面向单团队本地使用，无内建身份认证。绑定回环地址；向其他机器开放前须配置带身份认证与 TLS 的前置代理。模型默认关闭，安装后的规则构建可离线运行；拉取镜像和依赖需要网络。

## Docker

在仓库根执行：

```bash
docker compose -f deploy/compose.yml up --build
```

打开 <http://127.0.0.1:8000>。API 托管前端，worker 使用同一镜像；入口先迁移数据库，worker 等 API 健康后启动。镜像以 UID/GID `10001` 运行，首启从自制资产装载两份模板。

脚本中需要后台启动并等待就绪时，分别启动两个服务：

```bash
docker compose -f deploy/compose.yml build
docker compose -f deploy/compose.yml up --detach --no-build --wait --wait-timeout 120 df-api
docker compose -f deploy/compose.yml up --detach --no-build --no-deps df-worker
python scripts/smoke_deployment.py --url http://127.0.0.1:8000
```

仅 API 有 HTTP 健康探针；部分 Compose 版本对整栈 `--wait` 会因 worker 无探针而失败。上面的检查会创建项目并实际构建、下载三份文档，用于验证独立 worker 的作业消费能力，应在临时部署中运行。

数据存于 Compose 命名卷 `df-data`（通常实际名称为 `docfidelity_df-data`），包括 SQLite、工作区、上传件、模板、档案与产物。命名卷初始化时继承镜像 `/data` 权限，避免 Linux 自动创建的 bind 目录归 root 而无法写入。

已有 `deploy/data/` 的部署升级前须备份；它不会自动迁移到新命名卷。需要继续使用 bind mount 时自行覆盖 volumes，并在宿主机保证目录可被 UID/GID `10001` 访问。

```bash
docker compose -f deploy/compose.yml down
# 会永久删除命名卷中的全部运行数据，仅在明确清空时执行：
docker compose -f deploy/compose.yml down --volumes
```

SQLite 在线备份应使用一致性备份工具；整卷备份应先停 API 与 worker，再备份卷及恢复验证。重启与删除卷的语义不同。

## 从源码运行

Python 3.11–3.12、Node.js ≥ 22.12。在仓库根构建前端后运行真实阶段提供者：

```bash
python -m pip install -e ".[server]"
npm --prefix web ci
npm --prefix web run build
python -m uvicorn server.main:create_app --factory --host 127.0.0.1 --port 8000
```

默认为内存元数据仓储，重启清空。持久化部署在 shell 或 `.env` 设置下表变量，并在启动前运行 `python -m server.migrate`。

| 变量 | 建议值 / 含义 |
| --- | --- |
| `DOCFIDELITY_STORAGE_ROOT` | `.devdata`；运行数据根目录 |
| `DOCFIDELITY_DATABASE_URL` | `sqlite:///./.devdata/df.db`；为空时使用内存仓储 |
| `DOCFIDELITY_MODEL_ENABLED` | `false`；默认关闭模型 |
| `DOCFIDELITY_DETERMINISTIC_MODE` | `true`；避免缺省探测本机 Word |
| `DOCFIDELITY_START_WORKER` | `true`；源码单进程模式内嵌 worker；独立 worker 时设 `false` |
| `DOCFIDELITY_SEED_DEMO_TEMPLATES` | `true`；库为空时装载自制模板 |

独立 worker 用 `python -m server.worker`，两进程必须使用同一存储根、数据库与发行版。`server` 当前从源码树或容器运行，核心 wheel 不包含服务端包。PostgreSQL 还需自行安装相应驱动并验证连接。

事件共享 `jobs/<job_id>/events.jsonl`，API 与 worker 在作业级文件锁内分配序号和读写。支持同机或同宿主容器的本地文件系统；远程共享文件系统必须另验锁与同步语义。不要在服务运行时手工改写事件文件。

执行期间另持有作业级系统锁，进程退出时自动释放。重启后的 worker 为尚未发布收尾的完成或取消作业补登记已有文件，不重新执行阶段。取消请求生效后，当前阶段仍可完成；收尾期间重试与续跑返回 409，应等待完成后操作。

## 运维与验收

`/healthz` 为存活探针，`/readyz` 为就绪探针，`/health` 提供阶段与装载状态。配置密钥只在服务端使用；日志与备份须有独立访问控制。

首启装载示例模板；浏览器点击“一键创建演示项目”会从发行资产登记领域模型、三份档案和项目隔离的来源文件，可继续构建与下载。同名资产存在但内容不兼容时拒绝覆盖。此次首次发布前调整了示例封面和目录分节；已有开发卷中的旧同版本演示资产会冲突，应保留旧卷，使用独立存储根验证新版，或由维护者显式处理模板与档案版本，不能自动覆盖用户资产。

升级前已产生重复序号或损坏行的事件日志会明确报错，并保留原文件供审查。新实现不自动重编号旧历史，以免破坏已有 `Last-Event-ID`；严格追溯应保留旧作业并重新提交构建。

CI 会构建镜像并检查空数据卷启动，再通过 `scripts/smoke_deployment.py` 验证独立 worker 的三文档演示构建、报告、连续事件与 DOCX 下载。该脚本会创建演示项目，应在临时部署中执行，运行结果以对应 CI 为准。2026-10-08 已在 Windows Docker Desktop / Linux 容器实测镜像构建、空卷首启、独立 worker、重启持久化及容器浏览器演示构建下载。完整结果、当前配置模型的限制与实际 Word 页数见 [验收说明](../docs/验收/P0-验收自检.md)。
