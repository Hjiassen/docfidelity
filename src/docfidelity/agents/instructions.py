"""五 Agent 的 instructions 骨架（DD-12.1 / 需求 5.2.5）。

提示词在这里是**被版本管理的代码**，不是散落在调用处的拼串：
NFR-17 要求"instructions 与工具描述纳入版本管理，变更必须附回归结果"，
:func:`instructions_digest` 就是这个要求的可执行形式——提示词或工具描述改一个字，
摘要就变，回归用例随之失败，逼着改动附上新的评测结果。

风格约束（需求 5.2.5）
----------------------
短句、扁平结构、单一目标。避免长而嵌套的要求，减少决策歧义；
能靠 schema 约束的（输出形状）就不靠提示词叮嘱，
能靠"候选注入"避免的（自由生成）就不靠提示词禁止。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

from docfidelity.platform.hashing import digest_payload

from .outputs import AGENT_NAMES, AgentName

__all__ = [
    "AGENT_INSTRUCTIONS",
    "COMMON_RULES",
    "INSTRUCTIONS_VERSION",
    "agent_instructions",
    "instructions_digest",
]

#: instructions 版本。**变更即需附回归结果**（NFR-17）——与 `STAGE_IMPL_VERSION` 同理，
#: 它让"提示词改动"在 CHANGELOG 与回归记录里有处可查，而不是隐形地改掉模型行为。
INSTRUCTIONS_VERSION: Final = 1

#: 全体 Agent 共守的四条。它们对应 5.2.1 的三类失败模式（结构化输出不稳 / 工具选择易错 /
#: 多跳推理弱）：第 1 条收窄决策面，第 2 条收紧工具选择，第 3 条用候选代替自由生成，
#: 第 4 条让输出形状由 schema 而不是由自觉来保证。
COMMON_RULES: Final[tuple[str, ...]] = (
    "只回答当前这一个问题，不要顺带完成别的事。",
    "只能调用给定的工具；没有对应工具时直接说明做不到，不要臆造动作或参数。",
    "取值必须来自给定的候选清单；候选里没有的，原样放进未决字段交人工，不要自己造一个。",
    "只输出结构化结果，不要输出解释性文字。",
)

#: 逐 Agent 的职责与禁令。禁令逐条对应 DD-12.1 的"骨架要点"与需求 5.4 的职责边界：
#: 每一条都堵住一类"模型越权到确定性代码"的动作（需求 5.3 的边界）。
AGENT_INSTRUCTIONS: dict[AgentName, str] = {
    "curator": (
        "你是模板馆员。职责：从给定的候选清单中确认样式别名与版块定位。"
        "只能确认，不得新增候选；候选里找不到对应项的，放进 unconfirmed。"
        "不要读写任何 XML，不要解释模板内部结构。"
    ),
    "architect": (
        "你是文档架构师。职责：把给定的内容单元放进给定的候选章节，并给出理由。"
        "不得创造章节；候选章节里放不下的单元放进 unplaced。"
        "只做落位选择，不要生成正文文字。"
    ),
    "composer": (
        "你是内容撰写者。职责：依据给定事实写出章节内容，并套用给定术语。"
        "事实之外的内容必须逐条标注；有事实支撑的部分不得改写事实取值。"
        "不要决定版式，不要提及样式、编号或页面设置。"
    ),
    "renderer": (
        "你是版式落地者。职责：调用给定的渲染工具把内容落成文档。"
        "不得请求任何 XML 或样式操作；工具报告被护栏拦下时，如实报告未落盘，不要继续尝试绕过。"
    ),
    "inspector": (
        "你是质量审查者。职责：调用给定的检查工具，输出结构化判定与返工建议。"
        "你的判定只是建议，不是阻断依据；不要修改任何内容或产物。"
    ),
}


def agent_instructions(name: AgentName) -> str:
    """拼装某 Agent 的完整 instructions：职责段 + 通用四条。

    职责段后附固定通用规则，保持需求 5.2.5 的短句、扁平与单一目标。
    """
    body = AGENT_INSTRUCTIONS[name]
    rules = "".join(f"{index}. {rule}" for index, rule in enumerate(COMMON_RULES, start=1))
    return f"{body}\n\n通用规则：\n{rules}"


def instructions_digest(tool_summaries: Mapping[str, str] | None = None) -> str:
    """提示词 + 工具描述的确定性摘要（NFR-17 的版本抓手）。

    ``tool_summaries`` 由调用方传入（工具面在 `agents.tools`，本模块不反向依赖它）：
    工具描述是模型看到的另一半输入，只版本化 prompts 而放过描述等于漏掉一半。
    摘要用 `platform.hashing.digest_payload`（键序无关的确定性序列化），
    故"两个 Agent 的名单顺序变了、内容没变"不会误报改动。
    """
    payload = {
        "version": INSTRUCTIONS_VERSION,
        "agents": {name: agent_instructions(name) for name in AGENT_NAMES},
        "tools": dict(tool_summaries or {}),
    }
    return digest_payload(payload)
