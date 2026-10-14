"""角色规格与工具校验；共享决策目录来自契约根。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from docfidelity.contracts.base import FormatModel
from docfidelity.contracts.decision_catalog import *  # noqa: F403
from docfidelity.contracts.decision_catalog import (
    AGENT_SPECS_VERSION,
    DECISION_POINT_IDS,
    MAX_TOOLS_PER_AGENT,
)
from docfidelity.contracts.decision_catalog import __all__ as _catalog_exports
from docfidelity.contracts.decision_outputs import AGENT_OUTPUT_TYPES, AgentName
from docfidelity.contracts.errors import EngineError, ErrorCode

from .tools import TOOLS

__all__ = [*_catalog_exports, "AgentSpec", "HANDOFF_CHAIN", "validate_agent_spec"]


# ------------------------------------------------------------------ 五 Agent
@dataclass(frozen=True)
class AgentSpec:
    """一个 Agent 的声明（DD-12.1 的表格行）。

    ``decision_points`` 与 ``tools`` 分开记，是 DD-12.1 与需求 5.4 的直接对照：
    前者的"谁做哪类决策"、后者的"谁碰哪些能力"。合并成一个列表会让
    "这个 Agent 有 5 个工具"与"这个 Agent 管 5 件事"长得一样。
    """

    name: AgentName
    title: str
    single_duty: str
    tools: tuple[str, ...]
    decision_points: tuple[str, ...]
    handoff: AgentName | None
    rework_target: AgentName | None = None
    version: int = AGENT_SPECS_VERSION

    def output_type(self) -> type[FormatModel]:
        return AGENT_OUTPUT_TYPES[self.name]

    def as_record(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "title": self.title,
            "single_duty": self.single_duty,
            "output_type": self.output_type().__name__,
            "tools": list(self.tools),
            "decision_points": list(self.decision_points),
            "handoff": self.handoff,
            "rework_target": self.rework_target,
            "version": self.version,
        }


#: 显式 handoff 链（需求 5.4：handoff 是显式的阶段流转，不用提示词暗示）。
#: Inspector 是唯一有回边的节点：不合格返工回到 Composer。
HANDOFF_CHAIN: tuple[AgentName, ...] = (
    "curator",
    "architect",
    "composer",
    "renderer",
    "inspector",
)


def validate_agent_spec(spec: AgentSpec) -> None:
    """规格自检。任何一条不成立都抛 :class:`~docfidelity.contracts.errors.EngineError`。

    这里的每一条都对应一处**上游的硬约束**，不是审美：
    工具数 >7 违反 NFR-25；工具名不在白名单里等于给了模型一个调不到的能力；
    决策点不在目录里等于绕过分级表新开了一个判断点（需求第 13 章风险项原话）。
    """
    where = {"agent": spec.name}
    if len(spec.tools) > MAX_TOOLS_PER_AGENT:
        raise EngineError(
            ErrorCode.CONFIG_INVALID,
            f"Agent {spec.name} 声明了 {len(spec.tools)} 个工具，超过 {MAX_TOOLS_PER_AGENT} 个上限"
            "（NFR-25：单 Agent 工具数 ≤ 7）",
            where=where,
            remediation=["优先加能力而不是加工具，或把该 Agent 拆成两个单一职责的 Agent"],
        )
    unknown_tools = [name for name in spec.tools if name not in TOOLS]
    if unknown_tools:
        raise EngineError(
            ErrorCode.CONFIG_INVALID,
            f"Agent {spec.name} 引用了未登记的工具：{unknown_tools}",
            where={**where, "tools": unknown_tools},
            remediation=["在 agents/tools/library.py 登记该能力，或改用已登记的工具"],
        )
    unknown_points = [point for point in spec.decision_points if point not in DECISION_POINT_IDS]
    if unknown_points:
        raise EngineError(
            ErrorCode.CONFIG_INVALID,
            f"Agent {spec.name} 引用了未登记的决策点：{unknown_points}",
            where={**where, "decision_points": unknown_points},
            remediation=["决策点必须先归类并登记到 DECISION_POINTS"],
        )
    if spec.name not in HANDOFF_CHAIN:
        raise EngineError(
            ErrorCode.CONFIG_INVALID, f"Agent {spec.name} 不在 handoff 链上", where=where
        )
    _check_direction(spec)


def _check_direction(spec: AgentSpec) -> None:
    """handoff 只能指向链上的**下一个** Agent（Inspector 的回边单独用 ``rework_target``）。

    允许任意跳转会让"阶段流转"退化成一张随意的有向图：编排层无法回答
    "这一步之后该谁"，而 handoff 的全部价值就在于那个答案是**唯一**的。
    """
    index = HANDOFF_CHAIN.index(spec.name)
    expected = HANDOFF_CHAIN[index + 1] if index + 1 < len(HANDOFF_CHAIN) else None
    if spec.handoff != expected:
        raise EngineError(
            ErrorCode.CONFIG_INVALID,
            f"Agent {spec.name} 的 handoff 指向 {spec.handoff}，链上应为 {expected}",
            where={"agent": spec.name, "handoff": spec.handoff},
        )
    if spec.rework_target is not None and spec.rework_target not in HANDOFF_CHAIN:
        raise EngineError(
            ErrorCode.CONFIG_INVALID,
            f"Agent {spec.name} 的返工去向 {spec.rework_target} 不存在",
            where={"agent": spec.name},
        )
