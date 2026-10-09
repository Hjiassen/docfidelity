# docfidelity

模板与体例驱动的通用 DOCX 文档引擎：将可追溯的事实源按外部档案注入模板，并输出质量报告。

[![CI](https://github.com/Hjiassen/docfidelity/actions/workflows/ci.yml/badge.svg)](https://github.com/Hjiassen/docfidelity/actions/workflows/ci.yml)

当前版本 **0.0.1，Alpha**。内核、HTTP API 和浏览器工作台已实现；Docker、浏览器闭环、当前配置模型和 Word 分页已有本机实测。默认构建尚未接入模型决策，当前网关在默认置信度门槛下未通过基线；结果与发布缺口见 [验收说明](docs/验收/P0-验收自检.md)。

## 快速体验

Python 3.10–3.12；在仓库根目录执行。首次安装需要网络，安装后示例构建无需模型、数据库或 Office。

```bash
git clone https://github.com/Hjiassen/docfidelity.git
cd docfidelity
python -m venv .venv
# Linux / macOS: source .venv/bin/activate
# Windows PowerShell: .\.venv\Scripts\Activate.ps1
python -m pip install -e .
python examples/build_demo.py
```

同一批自制事实源生成三份文档：逐节设计、汇总设计与作业程序。产物位于 `var/examples/jobs/*/artifacts/`，报告位于对应作业的 `work/report.json`。使用与扩展步骤见 [使用指南](docs/guide/README.md) 和 [示例说明](examples/README.md)。

浏览器工作台另外需要 Node.js ≥ 22.12，可从源码运行：

```bash
python -m pip install -e ".[server]"
npm --prefix web ci
npm --prefix web run build
python -m uvicorn server.main:create_app --factory --host 127.0.0.1 --port 8000
```

打开 <http://127.0.0.1:8000>。默认元数据仓储在内存中，重启会清空；持久化、示例模板装载和 Docker 启动见 [部署说明](deploy/README.md)。不要把开发夹具服务器 `scripts/serve_dev.py` 的固定报告用作功能验收。

## 能力与边界

| 能力 | 当前边界 |
| --- | --- |
| 模板保真 | 比较模板包契约与产物的归一化样式结构，复用同表原型；不承诺整个 DOCX 的原始字节不变 |
| 类型无关 | 文档类型与领域模型由外部配置声明；新增类型需满足已有连接器、投影和渲染能力 |
| 可离线运行 | 默认关闭模型；安装后的规则链路可离线构建，待确认项进入裁决队列 |
| 可追溯 | 内容 IR 保留事实来源，构建包含报告、事件与阶段断点 |
| 可复现 | 冻结输入、关闭模型和 Office 时，示例 DOCX 与结构快照稳定；Office 页码依赖具体排版环境 |
| 部署 | 单团队自部署，无内建身份认证；默认仅绑定回环地址 |

启用模型需要 `python -m pip install -e ".[model]"`、显式配置兼容端点以及对应的逐点基线。模型、样本或路由配置变更后须重测；未验证的模型决策点回退规则并标记待确认。Word COM 和 PDF 目检为可选能力，许可证边界见 [NOTICE](NOTICE)。

## 架构

```text
模板原件 -> registry 入库 -> 模板包契约 ----+
事实源   -> ingest       -> 事实包 --------+-> compose -> ContentIR -> render -> DOCX -> qa
领域模型与文档类型档案 ---------------------+                                -> 报告
```

`contracts` 为依赖之根；`compose` 不操作 OOXML；模板解析发生在入库期；`server` 通过 `pipeline` 调用内核，`web` 只使用 HTTP。基础安装不含 Web、数据库或模型 SDK 依赖。

## 开发与文档

```bash
python -m pip install -e ".[dev,server,model]"
pytest -m "not e2e"
```

完整检查、严格类型与构建流程见 [贡献指南](CONTRIBUTING.md)。

- [使用指南](docs/guide/README.md)：构建、模板、档案、模型与报告。
- [需求](docs/需求/需求.md)与[详细设计](docs/设计/开发设计文档.md)：目标、约束和验收判据。
- [格式规范](docs/spec/README.md)与[架构决策](docs/design/adr/README.md)：对外契约与设计取舍。
- [路线图](ROADMAP.md)、[安全策略](SECURITY.md)、[治理](GOVERNANCE.md)、[变更记录](CHANGELOG.md)。

代码与自制示例采用 [Apache-2.0](LICENSE)。缺陷与建议请提交 [Issue](https://github.com/Hjiassen/docfidelity/issues)，仅附自造、脱敏数据。
