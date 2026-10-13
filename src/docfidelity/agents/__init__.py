"""决策层：五 Agent、工具面、路由与护栏（DD-12 / TASK-P0-16）。

本层在内核中的位置
------------------
``pipeline`` 之下、``qa`` 之上的独立层（见 ``.importlinter`` 的 ``kernel-layers``）。
它**只做决策**：可以 import `contracts` / `platform` 与工具面指向的各层公开入口，
但**不得触碰 `ooxml`**——那条禁令由契约 ``agents-has-no-ooxml`` 守着，是决策三
（"Agent 只做决策不碰 OOXML"）的可执行形式。

本层不 import `pipeline`：调用方向是"编排调 Agent"，不是"Agent 调编排"。
把方向搞反会得到循环依赖，而循环依赖的表现通常是"某次启动顺序变了才报 ImportError"。

五个模块
--------
============================  ==================================================
`agents.catalog`              声明目录：决策点分级表 + 五 Agent 规格
`agents.routing`              路由表、``dispatch`` 与留痕
`agents.tools`                工具白名单与调用闸门（含高危审批、路径作用域）
`agents.guardrails`           输入护栏 / 工具输入护栏 / 输出护栏
`agents.sdk_adapter`          薄适配层（全仓唯一接触 openai-agents 的地方）
============================  ==================================================

另有 `agents.baseline`（TASK-P0-17）：小模型基线评测——`routing` 的
``baseline`` 参数需要一个**来源**，那个来源就是它（样本集 → 逐决策点通过率 → 报告）。
它不是第六个 Agent，故不占上表。

声明校验在**导入期**执行（见文件末尾）：DD-12.3 要求"配置中每个决策点必须显式声明，
未声明即启动失败"。加载期是这类错误唯一可接受的发现时机——等模型调到那一条才发现，
看到的只是一次"工具调用失败"，而那时已经浪费了一轮构建。
"""

from __future__ import annotations

from docfidelity.contracts.errors import EngineError, ErrorCode

# 五个 Agent 的声明模块先导入：`AGENT_SPECS` 由它们聚合而成。
# 它们各自只依赖 `catalog` / `instructions` / `outputs`，故不构成导入环。
from . import architect as _architect
from . import composer as _composer
from . import curator as _curator
from . import inspector as _inspector
from . import renderer as _renderer
from .baseline import (
    BASELINE_FILENAME,
    CASE_SOURCE_VERSION,
    BaselineCase,
    BaselineReport,
    BaselineStatus,
    CaseOutcome,
    cases_from_payload,
    cases_to_payload,
    default_cases,
    endpoint_digest,
    load_status,
    run_baseline,
    save_report,
    uncovered_points,
)
from .catalog import (
    AGENT_SPECS_VERSION,
    DECISION_POINT_IDS,
    DECISION_POINTS,
    HANDOFF_CHAIN,
    MAX_TOOLS_PER_AGENT,
    AgentSpec,
    DecisionPointDef,
    decision_point,
    validate_agent_spec,
    validate_decision_catalog,
)
from .guardrails import (
    INSTRUCTION_RULES,
    InstructionGuardResult,
    OutputGuardResult,
    check_instruction,
    check_tool_input,
    guard_output,
    require_instruction,
)
from .instructions import (
    AGENT_INSTRUCTIONS,
    COMMON_RULES,
    INSTRUCTIONS_VERSION,
    agent_instructions,
    instructions_digest,
)
from .outputs import AGENT_NAMES, AGENT_OUTPUT_TYPES, AgentName
from .recorder import DecisionRecorder, MemoryRecorder
from .routing import (
    DEFAULT_MAX_RETRIES,
    AdapterOutcome,
    DecisionContext,
    DecisionOutcome,
    DecisionRequest,
    ModelAdapter,
    ResolvedRoute,
    RoutingTable,
    build_routing,
    dispatch,
)
from .sdk_adapter import (
    GrammarCapability,
    NullAdapter,
    OpenAIAgentsAdapter,
    ScriptedAdapter,
    probe_grammar,
)
from .tools import TOOLS, assert_whitelist_sane, descriptions, exposed_names, invoke
from .tools.spec import ToolApprovalPolicy

__all__ = [
    "AGENT_INSTRUCTIONS",
    "AGENT_NAMES",
    "AGENT_OUTPUT_TYPES",
    "AGENT_SPECS",
    "AGENT_SPECS_VERSION",
    "BASELINE_FILENAME",
    "CASE_SOURCE_VERSION",
    "COMMON_RULES",
    "DECISION_POINTS",
    "DECISION_POINT_IDS",
    "DEFAULT_MAX_RETRIES",
    "HANDOFF_CHAIN",
    "INSTRUCTION_RULES",
    "INSTRUCTIONS_VERSION",
    "MAX_TOOLS_PER_AGENT",
    "TOOLS",
    "AdapterOutcome",
    "AgentName",
    "AgentSpec",
    "BaselineCase",
    "BaselineReport",
    "BaselineStatus",
    "CaseOutcome",
    "DecisionContext",
    "DecisionOutcome",
    "DecisionPointDef",
    "DecisionRecorder",
    "DecisionRequest",
    "GrammarCapability",
    "InstructionGuardResult",
    "MemoryRecorder",
    "ModelAdapter",
    "NullAdapter",
    "OpenAIAgentsAdapter",
    "OutputGuardResult",
    "ResolvedRoute",
    "RoutingTable",
    "ScriptedAdapter",
    "ToolApprovalPolicy",
    "agent_instructions",
    "agent_spec",
    "assert_whitelist_sane",
    "build_routing",
    "cases_from_payload",
    "cases_to_payload",
    "check_instruction",
    "check_tool_input",
    "decision_point",
    "default_cases",
    "endpoint_digest",
    "descriptions",
    "dispatch",
    "exposed_names",
    "guard_output",
    "instructions_digest",
    "invoke",
    "load_status",
    "probe_grammar",
    "require_instruction",
    "run_baseline",
    "save_report",
    "uncovered_points",
]

#: 五 Agent 规格，按 handoff 链的顺序排列。
#:
#: 顺序即链序，故 ``AGENT_SPECS[i].handoff == AGENT_SPECS[i + 1].name`` 是一条可断言
#: 的不变量（`validate_agent_spec` 已在加载期逐条验证）。用**元组**而不是字典，
#: 是因为"这一步之后该谁"只能有一个答案，而字典顺序容易被当成无关紧要的细节。
AGENT_SPECS: tuple[AgentSpec, ...] = (
    _curator.SPEC,
    _architect.SPEC,
    _composer.SPEC,
    _renderer.SPEC,
    _inspector.SPEC,
)

_SPECS_BY_NAME: dict[str, AgentSpec] = {spec.name: spec for spec in AGENT_SPECS}


def agent_spec(name: str) -> AgentSpec:
    """按名取 Agent 规格；未登记即抛（编排层写错名字必须当场可见）。"""
    spec = _SPECS_BY_NAME.get(name)
    if spec is None:
        raise EngineError(
            ErrorCode.INTERNAL,
            f"未登记的 Agent：{name}",
            where={"agent": name},
            remediation=["本轮只定义五个 Agent（见 agents/catalog.py 的 HANDOFF_CHAIN）"],
        )
    return spec


def _run_declaration_checks() -> None:
    """加载期声明校验（DD-12.3 的"未声明即启动失败"）。

    三件事：决策点目录自洽、五 Agent 规格自洽、工具白名单自洽。
    它们全都是**声明错误**，因此在导入期就炸是正确处置——这些错误一旦漏到运行期，
    表现是"模型调了个调不到的工具"，而没有人会想到去查声明。
    """
    validate_decision_catalog()
    for spec in AGENT_SPECS:
        validate_agent_spec(spec)
    assert_whitelist_sane()


_run_declaration_checks()
