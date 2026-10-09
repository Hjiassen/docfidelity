"""Inspector · 质量审查（DD-12.1 第 5 行 / 需求 5.4）。

职责：门禁校验、体例评审、漂移检测，输出返工清单。
输出契约：:class:`~docfidelity.agents.outputs.QaVerdict` —— 结构化判定，
``is_binding`` 恒为 ``False``。
handoff：→ Composer（不合格返工）——链上唯一有回边的节点。

职责边界（DD-12.1 的"骨架要点"）：**输出结构化判定，不作为阻断依据**。
阻断的唯一出口是 ``qa.can_finalize``；把模型判定接进阻断链，等于用一次低置信的
采样决定"这份文档能不能交出去"（需求 5.3 原则：能写成规则的绝不问 LLM）。

它名下的三个决策点分别是 ``rule``（门禁判定）、``small_model``（体例裁决建议）、
``optional_large_model``（文档评审）——**同一个 Agent 的决策点分属三档**，
这正是"决策点分级"必须逐点声明而不能按 Agent 一刀切的理由。
"""

from __future__ import annotations

from .catalog import AgentSpec

__all__ = ["SPEC"]

SPEC = AgentSpec(
    name="inspector",
    title="质量审查",
    single_duty="输出结构化判定与返工清单，不作为阻断依据。",
    tools=("qa_run", "style_audit", "drift_check"),
    decision_points=("gate.judge", "convention.advisory", "review.document"),
    handoff=None,
    rework_target="composer",
)
