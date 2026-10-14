# AGENTS.md

本文件面向**在本仓库工作的 AI 编码助手**，以及需要快速理解约束边界的人类协作者。
它不是教程，是**纪律清单**——违反其中任何一条，代码在 CI 上会直接失败。

上游依据：`docs/需求/需求.md`（需求 · 架构 · 信息架构）、`docs/设计/开发设计文档.md`（详细设计）。

---

## 1. 不可违反的硬规则

按「最容易踩」排序。每条都标注了自动校验方式。

| # | 规则 | 校验 |
| --- | --- | --- |
| 1 | **生成期不得重新解析模板、不得与模板对比**，只查模板包契约 | 单测断言调用栈无 `TemplateParser`；连续 3 次构建产物一致 |
| 2 | `compose` 与 `server/api` **禁止 import `ooxml`** | `lint-imports` 禁止依赖契约 |
| 3 | 引擎代码**不得出现文档类型名与领域概念名**（含 `== "endpoint"` 这类分支） | `scripts/check_forbidden_words.py` 命中必须为 0 |
| 4 | 内核（`src/docfidelity/**`）**不得引入 Web / DB 依赖**，也不得 `import random` | `scripts/check_kernel_no_web.py` |
| 5 | 跨文档克隆样式必须**同时**处理 `w:pStyle` 与 `w:tblStyle` | `ooxml.styles` 单测 |
| 6 | 新表格的行模板必须取**同表自己的 `tr`**，不跨表克隆 | `render.tables` 单测 |
| 7 | 图片越界的唯一判据是 `wp:extent` 的 `cx/cy`，**禁止用页数反推** | `qa` 规则 FR-M5-07 |
| 8 | 图片、表格**只缩不放** | 几何纯函数单测 |
| 9 | 一切比对前先**归一化易变标识**（`_Toc` 书签 ID、部件关系 ID） | `ooxml.normalize`；禁止字节比对 |
| 10 | 模板冗余裁剪必须定位到「标题 + 段落 + 表格」**完整块** | `registry.parser` 单测 |
| 11 | 模型不可用必须**可运行** —— 规则默认值 + 标记「待确认」进裁决队列，**构建不得失败** | `DF_MODEL_ENABLED=false` 跑通全链路 |
| 12 | 界面上每一个「能改的东西」都必须对应配置文件里的一行；**界面不做业务判断** | 评审 |
| 13 | 单文件 **≤ 800 行** | `scripts/check_line_limit.py` |
| 14 | 内核不抛裸 `Exception`，统一 `EngineError` 且带 `code` | 评审 + 规则 |

## 2. 模块边界（允许依赖 → 禁止依赖）

```
builder（组合根）
  ↑
agents（适配器，仅消费契约和工具面）
  ↑
pipeline
  ↑
compose | registry | ingest | render | qa
  ↑
ooxml | model
  ↑
contracts            platform（与 contracts 平级，不被内核业务模块反向依赖）
```

| 模块 | 允许依赖 | 明确禁止 |
| --- | --- | --- |
| `contracts` | `pydantic` | 其余一切 |
| `platform` | 标准库 + `pydantic` | 内核业务模块 |
| `ooxml` | `contracts` `platform` | `compose` `registry` `pipeline` `server` `web` |
| `model` | `contracts` | **`ooxml`** `render` `qa` `registry` |
| `ingest` | `contracts` `model` `platform` | `ooxml` `registry` `render` `qa` |
| `registry` | `contracts` `ooxml` `platform` | `compose` `render` `qa` |
| `compose` | `contracts` `model` `ingest` | **`ooxml`** `registry` `render` |
| `render` | `contracts` `ooxml` `platform` | `compose` `server` |
| `qa` | `contracts` `ooxml`（只读） | `server` `web` |
| `pipeline` | 上述内核模块 + `platform`，通过契约接收模型适配器 | **`agents` `server` `web`** |
| `agents` | `contracts` `pipeline` 工具面 | `web` **`ooxml`** |
| `server` | `contracts` `pipeline` | **`ooxml`** `registry` 内部实现 |
| `web` | 仅 HTTP | 一切内核模块 |

## 3. 六条关键决策（ADR）

1. **编排与渲染解耦** —— 内容以 `ContentIR` 表达；`compose` 只产出 IR，`render` 只消费 IR。
2. **模板解析前置** —— 解析全部落在 `registry.ingest`；生成期只走 `ContractQuery`。
3. **模型只产出结构化决策或内容草稿** —— 不输出布局或 OOXML；工具层不注册任何 OOXML 直写工具。正文撰写使用独立内容生成协议，不能塞进旧候选选择协议。
4. **核心零 Web 依赖** —— 内核可在无 HTTP、无 DB 环境下全绿。
5. **契约模块为依赖之根** —— `contracts` 只依赖 `pydantic`，被所有人依赖。
6. **引擎与文档类型无关** —— 文档类型与领域概念均为外部数据，接入新类型只写配置。

V2 以用户选择生成的有效模板为格式源，以新材料为业务源。原件只在导入时读取；渲染消费有效材料、FormatBindings 与冻结空原型。未识别项默认不选，页眉/封面普通旧文字也须清理，固定素材必须显式确认。V1 领域档案入口临时保留至 M5，不是 V2 的必经步骤。

## 4. 编码规范

- 标识符英文；注释与文案中文。
- 公开函数 100% 类型标注；`contracts` / `ooxml` / `render` / `qa` 走 `mypy --strict`。
- 几何计算、投影求值、归一化必须是**无副作用纯函数**。
- 内核错误一律转 `EngineError`；`fatal=False` 的失败**不得**中断阶段，只进裁决队列。
- 日志为结构化 JSON，**密钥类字段永不出现在日志中**。
- 提交信息格式：`<type>(<scope>): <subject>`，type ∈ `feat|fix|refactor|test|docs|build|chore`。

## 5. 命名定稿（已定）

**工程名 = `docfidelity`**（2026-09-21 定稿，对应上游 `OS-18`）。三处可用性已实测：
PyPI 包名 / GitHub 用户名（或组织名）均 HTTP 404 可用，`docfidelity.com` / `.cn` / `.org` 无 DNS 记录可注册。

仓库自始即用 `docfidelity`（`pyproject` 的 `name`、`src/docfidelity/`、全部导入路径、文档、CI），
故本次定稿**零路径改动**——后续引入新依赖 / 新模块沿用该名即可，不再讨论改名。

## 6. 验证入口

提交前至少跑一遍：

```bash
lint-imports
python scripts/check_forbidden_words.py
python scripts/check_line_limit.py
python scripts/check_kernel_no_web.py
pytest tests/unit -m unit
```
