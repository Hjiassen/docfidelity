# 贡献指南

修改前阅读 [AGENTS.md](AGENTS.md) 的模块边界与硬规则。示例和问题复现必须使用自造数据，禁止提交真实客户模板、工作区、密钥或内网地址。

## 环境

最低 Python 3.11，当前支持 3.11–3.12；前端与容器构建使用 Node.js 22。

```bash
python -m venv .venv
# Linux / macOS: source .venv/bin/activate
# Windows PowerShell: .\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev,server,model]"
npm --prefix web ci
pre-commit install
```

仅修改内核可安装 `.[dev]` 并运行 `tests/unit`；API 和集成测试需要 `server`，真实 SDK 形状检查需要 `model`。Office 不作为自动化测试的前提。

Windows 执行检查前设置 `$env:PYTHONUTF8="1"`；Linux / macOS 可用 `export PYTHONUTF8=1`。直接从源码运行依赖检查还需 `PYTHONPATH` 包含 `src` 与仓库根，安装 editable 包可免此设置。

## 验证

```bash
ruff check .
ruff format --check .
mypy src/docfidelity server
lint-imports
python scripts/check_forbidden_words.py
python scripts/check_line_limit.py
python scripts/check_kernel_no_web.py
python scripts/export_schemas.py --check
pytest tests/unit -m unit
pytest -m "not e2e"
npm --prefix web run build
npm --prefix web test
python scripts/make_demo_templates.py --check
python scripts/update_golden.py --check
python -m build
python -m twine check dist/*
```

CI 分别检查基础安装、完整安装、前端、包与容器。基础安装测试在不安装 Web 和数据库依赖的环境下运行。`contracts`、`ooxml`、`render`、`qa` 使用严格类型检查。pre-commit 执行基础格式和架构脚本，**不代替全量测试与 CI**。

修改 HTTP 模型后重新生成并提交：

```bash
python scripts/dump_openapi.py
npm --prefix web run gen:api
```

修改契约后运行 `python scripts/export_schemas.py`。Schema 是生成物，不手工编辑。Golden 漂移应先确认行为变化，再更新基准。

## 变更要求

- 公开函数完整类型标注；标识符英文，注释与文案中文；单个实现文件不超过 800 行。
- 缺陷修复附能复现原问题的回归测试。几何、归一化和投影保持纯函数。
- 接入新文档类型优先新增档案、领域模型和规则配置；参考 `examples/profiles/` 与 `examples/domains.json`。
- 契约已有字段改名或语义变化须附迁移说明；架构变更按 [治理规则](GOVERNANCE.md) 提交 RFC / ADR。
- 阶段行为变化须递增 `contracts.job.STAGE_IMPL_VERSION`，在 [CHANGELOG](CHANGELOG.md) 说明断点失效范围。

创建分支并提交 PR；提交格式为 `<type>(<scope>): <subject>`，type 为 `feat|fix|refactor|test|docs|build|chore`。PR 说明问题、结果、验证与兼容影响，通过 CI 后由维护者审阅。GitHub 分支保护需要仓库管理员另行配置，本地钩子不能阻止直接推送。

参与社区遵守 [行为准则](CODE_OF_CONDUCT.md)。安全问题使用 [安全策略](SECURITY.md) 的私密渠道。
