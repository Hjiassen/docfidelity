#!/usr/bin/env python3
"""P0 验收 45 条的**登记表**（TASK-P0-22 / 需求第 11 章）。

为什么把这张表单独成一个模块
--------------------------
`check_line_limit.py` 的 800 行上限（NFR-09）在这里正好起了作用：45 条 × 每条
"判据 + 证据 + 判定方式"，加上判定实现，一个文件放不下。按职责切开也就有了清楚的边界
——**本模块只有数据**（哪条验收项、判据是什么、证据在哪、怎么判），判定实现在
`scripts/acceptance_check.py`。

`kind` 三种取值
------------
* ``auto``   —— ``target`` 是 `acceptance_check.AUTO_CHECKS` 里的检查名，**可以用 `;`
  分隔多个**（验收 33 的判据原文点名了 `lint-imports` 与行数上限两件事，只挂一个
  会让另一个成了"注册了却永远不执行"）；
* ``suite``  —— ``target`` 是要跑的 pytest 目标；
* ``manual`` —— ``target`` 是**该做什么**（脚本原样打出来，不假装它过了）。

`how` 与 `evidence` 分开两列是刻意的：前者是**上游原文的判据**（不许改写），后者是
**今天靠什么成立**。合成一列之后，"判据被悄悄放宽"就再也看不出来了。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Final

__all__ = [
    "ITEMS",
    "REQUIRED_FILES",
    "SUITE_COMMANDS",
    "WEB_PAGES",
    "Item",
]

REPO_ROOT: Final = Path(__file__).resolve().parent.parent

#: 开源条件成立（验收 20/31）要求存在的文件。缺一个就不是"clone 下来能跑"。
REQUIRED_FILES: Final = (
    "LICENSE",
    "NOTICE",
    "README.md",
    "CONTRIBUTING.md",
    "GOVERNANCE.md",
    "ROADMAP.md",
    "CODE_OF_CONDUCT.md",
    "SECURITY.md",
    "CODEOWNERS",
    "AGENTS.md",
    "CHANGELOG.md",
    "pyproject.toml",
    ".importlinter",
    "deploy/Dockerfile",
    "deploy/compose.yml",
    "deploy/entrypoint.sh",
    "deploy/.env.example",
    "deploy/README.md",
    "tests/integration/test_one_click.py",
)

#: `web` 的十四页（需求 7.3.2 站点地图）。页面名即交付面（验收 27/35）。
WEB_PAGES: Final = tuple(f"P{index:02d}" for index in range(1, 15))


@dataclass(frozen=True)
class Item:
    """一条验收项：编号 + 判据 + 证据 + 判定方式。"""

    number: int
    title: str
    how: str
    evidence: str
    kind: str  # "auto" | "suite" | "manual"
    #: auto: 检查名（**多个用 `;` 分隔**，见 `acceptance_check.TARGET_SEPARATOR`）；
    #: suite: pytest 目标；manual: **该做什么**。
    target: str


#: `--with-tests` 跑的两条命令。
#: 存成 **argv 列表**而不是一行字符串：`-m "unit or integration or replay"` 里那个带空格的
#: 表达式一旦被按空白切开，pytest 会开始找名叫 `or` 的目录，而报错信息是
#: "file or directory not found: or"——看起来像环境问题，其实是这里少了一层引号。
SUITE_COMMANDS: Final[tuple[tuple[str, list[str]], ...]] = (
    (
        "unit+integration+replay",
        ["-m", "unit or integration or replay", "tests/unit", "tests/integration", "tests/replay"],
    ),
    ("api", ["-m", "api", "tests/api"]),
)

#: 45 条验收项。`kind=auto` 的 `target` 指向 :data:`AUTO_CHECKS` 里的名字。
ITEMS: Final[tuple[Item, ...]] = (
    Item(
        1,
        "文档产出",
        "按档案一键产出 docx，命名符合档案声明",
        "tests/api/test_server_reference_build.py",
        "suite",
        "tests/api/test_server_reference_build.py",
    ),
    Item(
        2,
        "样式保真",
        "样式层一致 / 引用的 styleId 全部存在 / 无直设格式残留",
        "tests/unit/test_ooxml_styles.py、test_qa_rules_style.py",
        "suite",
        "tests/unit/test_ooxml_styles.py tests/unit/test_qa_rules_style.py",
    ),
    Item(
        3,
        "版式合规",
        "表头加粗缺失 0 / 数据行误加粗 0 / 底纹残留 0 / 表格越界 0",
        "tests/unit/test_qa_rules_layout.py、test_ooxml_geometry.py",
        "suite",
        "tests/unit/test_qa_rules_layout.py tests/unit/test_ooxml_geometry.py",
    ),
    Item(
        4,
        "分页合规",
        "正文分页符 0，仅封面 / 修订记录 / 目录三处允许",
        "tests/unit/test_render_pipeline.py",
        "suite",
        "tests/unit/test_render_pipeline.py tests/unit/test_ooxml_refresh.py",
    ),
    Item(
        5,
        "图片合规",
        "图片落在版心内、无裁剪、图注与图不分离",
        "tests/unit/test_ooxml_pictures.py",
        "suite",
        "tests/unit/test_ooxml_pictures.py",
    ),
    Item(
        6,
        "术语一致",
        "软件名与模块名全局一致，禁用旧称命中 0",
        "tests/unit/test_compose_glossary.py",
        "suite",
        "tests/unit/test_compose_glossary.py",
    ),
    Item(
        7,
        "内容完整",
        "模板槽位未填 0 / 占位提示残留 0",
        "tests/unit/test_qa_rules_content.py",
        "suite",
        "tests/unit/test_qa_rules_content.py",
    ),
    Item(
        8,
        "事实一致",
        "文档中的计数与清单与事实源逐项一致",
        "tests/unit/test_model_projection.py、test_compose_builder.py",
        "suite",
        "tests/unit/test_model_projection.py tests/unit/test_compose_builder.py",
    ),
    Item(
        9,
        "稳定比对",
        "归一化易变标识后两次构建文本差异行数 = 0",
        "tests/unit/test_qa_compare.py、test_ooxml_normalize.py",
        "suite",
        "tests/unit/test_qa_compare.py tests/unit/test_ooxml_normalize.py",
    ),
    Item(
        10,
        "可复现",
        "冻结相同输入、配置与运行姿态后文本与结构快照稳定；实际页数需固定排版环境另验",
        "scripts/update_golden.py --check + tests/integration/test_golden_snapshot.py",
        "auto",
        "golden-reproducible",
    ),
    Item(
        11,
        "报告完整",
        "单次构建输出统一 QA 报告（通过 / 未通过 / 豁免 / 产物清单）",
        "tests/unit/test_qa_engine.py",
        "suite",
        "tests/unit/test_qa_engine.py",
    ),
    Item(
        12,
        "追溯可查",
        "任取一个条目可查到其章节；任取一节可查到事实来源",
        "tests/unit/test_qa_engine.py（追溯矩阵）、test_model_projection.py",
        "suite",
        "tests/unit/test_qa_engine.py tests/unit/test_model_projection.py",
    ),
    Item(
        13,
        "模板库生效",
        "生成期不再解析模板；同模板连续 3 次产物一致",
        "tests/unit/test_pipeline_templates.py、test_registry_pipeline.py",
        "suite",
        "tests/unit/test_pipeline_templates.py tests/unit/test_registry_pipeline.py",
    ),
    Item(
        14,
        "模板版本可比",
        "两个版本能输出样式 / 结构 / 表原型 / 页面设置差异清单",
        "tests/unit/test_registry_version_diff.py",
        "suite",
        "tests/unit/test_registry_version_diff.py",
    ),
    Item(
        15,
        "契约自足",
        "版式决策全部可从模板包契约查到，无需回读原件",
        "scripts/export_schemas.py --check + tests/unit/test_registry_package_io.py",
        "auto",
        "schemas-reproducible",
    ),
    Item(
        16,
        "包完整性",
        "文件齐全 / JSON 可解析 / 原件可深拷贝 / 契约与实际一致",
        "scripts/make_demo_templates.py --check + tests/unit/test_registry_package_io.py",
        "auto",
        "demo-templates-reproducible",
    ),
    Item(
        17,
        "Agent 不越界",
        "工具调用中不含 OOXML 直接写操作",
        "tests/unit/test_agents_tools.py、test_agents_guardrails.py",
        "suite",
        "tests/unit/test_agents_tools.py tests/unit/test_agents_guardrails.py",
    ),
    Item(
        18,
        "LLM 可关闭",
        "关闭 LLM 后仍产出并通过全部门禁",
        "tests/api/test_server_reference_build.py、tests/unit/test_agents_routing.py",
        "suite",
        "tests/api/test_server_reference_build.py tests/unit/test_agents_routing.py",
    ),
    Item(
        19,
        "决策可审计",
        "报告含每个 Agent 的判定清单与理由，可按 run_id 追溯",
        "tests/unit/test_pipeline_decisions.py、tests/api/test_server_decisions.py",
        "suite",
        "tests/unit/test_pipeline_decisions.py tests/api/test_server_decisions.py",
    ),
    Item(
        20,
        "开源可跑通",
        "Python 3.11 / 3.12 上 clone 后用自制示例模板在无私有数据下跑通",
        "LICENSE/README/CONTRIBUTING/… 与入口点",
        "auto",
        "open-source-prereqs",
    ),
    Item(
        21,
        "新人上手成本",
        "从 clone 到产出第一份文档 ≤ 30 分钟，不需 API Key / 真实模板 / Office",
        "人工计时（真实使用者）",
        "manual",
        "按 README 的快速开始在本机实测一次并记时；需要一位没读过本仓库的人",
    ),
    Item(
        22,
        "命名无冲突",
        "项目名、PyPI 包名与导入路径一致；首次发布前验证发布权限与命名占用",
        "仓库内命名一致；外部发布权限需核验",
        "manual",
        "验证 docfidelity 的首次 PyPI 发布权限与占用状态；已拥有的 GitHub 仓库无需查空",
    ),
    Item(
        23,
        "许可证兼容",
        "LICENSE 与全部直接依赖兼容；核心链路无 GPL/AGPL",
        "LICENSE + 依赖元数据扫描",
        "auto",
        "license-compat",
    ),
    Item(
        24,
        "可离线验证",
        "CI 不调用真实模型 API；确定性模式下全量通过并产 golden",
        "scripts/update_golden.py --check + tests/replay",
        "auto",
        "golden-reproducible",
    ),
    Item(
        25,
        "扩展点可用",
        "第三方仅按文档即可实现并加载一个 Connector 或 QA 规则",
        "docs/design/adr/ADR-007 + tests/unit/test_ingest_framework.py",
        "suite",
        "tests/unit/test_ingest_framework.py tests/unit/test_ingest_connectors.py",
    ),
    Item(
        26,
        "兼容承诺可执行",
        "对外格式自带版本标识；弃用流程有一次真实实例",
        "8 个 schema 家族逐个查 format_version",
        "auto",
        "schema-families",
    ),
    Item(
        27,
        "Web 端全流程",
        "浏览器内完成上传→入库→配置→构建→审阅→下载，全程不用命令行",
        "web 十四页 + 路由（真浏览器走查见 MANUAL）",
        "auto",
        "web-pages",
    ),
    Item(
        28,
        "长任务可用性",
        "构建异步且进度可见；服务重启后作业可识别并续跑",
        "tests/unit/test_pipeline_job.py、test_pipeline_stages.py",
        "suite",
        "tests/unit/test_pipeline_job.py tests/unit/test_pipeline_stages.py",
    ),
    Item(
        29,
        "上传前置校验",
        "非法 / 超大 / 不可解析的文件在提交时即被拒并给可操作提示",
        "tests/api/test_server_uploads.py",
        "suite",
        "tests/api/test_server_uploads.py",
    ),
    Item(
        30,
        "默认离线数据边界",
        "安装完成后关闭模型，断网可完成全流程且无外部请求；授权模型出口另按40核验；密钥不出前端与日志",
        "唯一出网字段 + 零硬编码外网地址 + .env 未入库",
        "auto",
        "single-egress-field;no-hardcoded-outbound;env-not-committed",
    ),
    Item(
        31,
        "一键体验",
        "`docker compose up` 后浏览器访问即可跑通全流程",
        "deploy/* + 容器与浏览器实测范围见 docs/验收/P0-验收自检.md",
        "manual",
        "需从空数据卷执行 docker compose up --build，再在浏览器内完成构建与下载；文件存在及API用例不能证明容器体验通过",
    ),
    Item(
        32,
        "内核无 Web 依赖",
        "内核在无 Web 框架、无数据库的环境下独立完成入库→构建→门禁",
        "scripts/check_kernel_no_web.py + tests/unit",
        "auto",
        "kernel-no-web",
    ),
    Item(
        33,
        "模块边界可校验",
        "无循环依赖、无越界导入；compose 与 api 中不出现 OOXML 操作",
        "lint-imports + 行数上限",
        "auto",
        "lint-imports;line-limit",
    ),
    Item(
        34,
        "内容 IR 纯净",
        "IR 及其编排代码中不出现 styleId / w: 前缀 / twips / 绝对列宽",
        "tests/unit/test_compose_purity.py",
        "suite",
        "tests/unit/test_compose_purity.py",
    ),
    Item(
        35,
        "信息架构落地",
        "模板入库向导、构建作业、门禁裁决、产物管理、模板版本对比五类页面可用",
        "web 十四页 + 路由（真浏览器走查见 MANUAL）",
        "auto",
        "web-pages",
    ),
    Item(
        36,
        "作业粒度正确",
        "一次构建派生出等量子作业；失败隔离；可只重跑失败子作业",
        "tests/unit/test_pipeline_job.py、tests/api/test_server_jobs.py",
        "suite",
        "tests/unit/test_pipeline_job.py tests/api/test_server_jobs.py",
    ),
    Item(
        37,
        "配置模型主流程与基线",
        "当前配置模型在发行默认阶段实际触发至少一个达标决策点；拟接管点均有匹配且达标的逐点真实基线，其余走规则；不限定参数量、量化或部署位置",
        "当前配置模型已实测；默认阶段集成与缺置信度准入仍缺，见 docs/验收/P0-验收自检.md",
        "manual",
        "使用当前.env模型补齐默认阶段调用、决策、产物与逐点基线证据；当前默认基线0/10、默认阶段0调用仍未达标；辅助阶段与仅将阈值置0不能替代",
    ),
    Item(
        38,
        "结构化输出可靠",
        "采用的输出100%过Schema校验；结构化输出能力按探测启用；不支持或关闭受限解码时靠字段约束+校验+有限重试+规则兜底",
        "tests/unit/test_agents_structured.py、test_agents_sdk_shape.py",
        "suite",
        "tests/unit/test_agents_structured.py tests/unit/test_agents_sdk_shape.py",
    ),
    Item(
        39,
        "降级路径可验证",
        "断开模型后全链路退化且产出合规；缺置信度/低置信与失败项默认进裁决队列，无静默采用；替代准入须显式配置、留存依据并真实评测",
        "tests/unit/test_agents_routing.py、tests/api/test_server_decisions.py",
        "suite",
        "tests/unit/test_agents_routing.py tests/api/test_server_decisions.py",
    ),
    Item(
        40,
        "授权模型接入与出口",
        "模型仅请求显式配置且授权的OpenAI兼容端点，不限定部署位置；禁止tracing外发与隐式云端回退；系统级运行期记录核验无未授权出口",
        "源码扫描与回放测试不能证明真实模型运行期出口",
        "manual",
        "接入当前.env模型，以系统级记录核对API与worker的运行期出口及授权端点；关闭模型后确认无模型请求；日志与证据脱敏",
    ),
    Item(
        41,
        "决策点归属可查",
        "每个决策点在配置中显式声明规则/配置模型/可选模型/人工，保留历史路由字段；界面可查看实际路由与降级依据",
        "tests/unit/test_agents_catalog.py、test_agents_routing.py",
        "suite",
        "tests/unit/test_agents_catalog.py tests/unit/test_agents_routing.py",
    ),
    Item(
        42,
        "类型无关可校验",
        "引擎代码中零个文档类型名与领域概念名；render 不针对具体 kind 分支",
        "docfidelity.qa.type_agnostic（AST 检索，含 branch_literal）",
        "auto",
        "type-agnostic",
    ),
    Item(
        43,
        "同源多型",
        "同一事实包按 ≥2 份档案产出结构形态不同的两类文档，门禁全过",
        "demo-templates × examples/profiles",
        "auto",
        "demo-assets-cover-types",
    ),
    Item(
        44,
        "新增类型零代码",
        "新增第三类文档类型（非软件域）仅通过配置完成，引擎零改动",
        "引擎零禁用词 + 示例资产；完整配置扩展证据尚需核验",
        "auto",
        "type-agnostic;demo-assets-cover-types",
    ),
    Item(
        45,
        "档案可交换",
        "档案连同模板包导出后，在另一环境导入即可复现同样产出",
        "tests/api/test_server_profile_package.py；跨环境构建与归一化比对尚待验证",
        "auto",
        "profile-exchange",
    ),
)
