# ADR-004 Web 端是引擎的前端，内核零 Web 依赖

- 状态：Accepted
- 日期：2026-09-17
- 关联：需求 4.1 决策四、2.5；设计 DD-1.1、DD-2.2、DD-2.3（3）

## 背景

本期交付形态是 Web 端，但产品定位是**引擎**：模板解析、内容装配、渲染、门禁
这四件事本身与 HTTP 无关。所以：

- 交付形态会变（后续要 CLI / MCP Server / SDK），内核不应跟着变；
- 内核的测试不应为了跑一次渲染而启动服务、准备数据库；
- 部署边界不应被框架绑死——"数据不出内网"意味着部署形态要能自由裁剪。

## 决策

`src/docfidelity/**`（内核）**不引入** Web 框架、ASGI 服务器与数据库驱动：

- 禁止模块：`fastapi` / `starlette` / `flask` / `uvicorn` / `sqlalchemy` / `alembic` /
  `sse_starlette` / `pydantic_settings` / `server`；
- 内核在无 HTTP、无数据库的环境中必须能完整运行；
- Web、作业调度、持久化归 `server` 包；`server` 只依赖 `contracts` 与 `pipeline`。

## 备选方案与取舍

| 方案 | 优点 | 代价 | 结论 |
| --- | --- | --- | --- |
| 内核内嵌 Web 依赖 | 少一层适配；开发省事 | 内核单测需起服务；形态扩展要复制逻辑；依赖面被审计放大 | 否 |
| **内核零 Web（采纳）** | 内核可独立测试与复用；交付形态可替换 | 需要 `server` 层做适配与编排；跨层数据必须走显式契约 | 采纳 |

## 后果

### 正面

- `pytest tests/unit` 在无 HTTP、无 DB 环境中全绿，本身就是可执行的证明（验收 32）；
- 后续 CLI / MCP / SDK 可复用同一内核，不需要重写业务逻辑；
- 内核安装不拉入 Web 依赖（`pyproject.toml` 分段依赖 `OS-13`）。

### 负面（明确接受的代价）

- 内核不能使用 `pydantic-settings` 这类带环境耦合的便利库；
- 一些在内核层看起来"顺手"的编排职责，必须上移到 `server`。

### 需要后续跟进

- `scripts/check_kernel_no_web.py`（AST 静态检查）+ `tests/unit/conftest.py`
  的运行时断言 + `.importlinter` 的 `server → ooxml` 禁止，三处共同守。
