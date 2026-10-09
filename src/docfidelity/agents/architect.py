"""Architect · 文档架构师（DD-12.1 第 2 行 / 需求 5.4）。

职责：读结构骨架与事实源，规划章节内容清单与落位。
输出契约：:class:`~docfidelity.agents.outputs.PlacementPlan` —— 在给定候选章节中
选择落位。
handoff：→ Composer。

职责边界（DD-12.1 的"骨架要点"）：**不得创造章节**。模型请求的新章节会原样记进
``created_chapters`` 作为违规痕迹，但**不会**被落地——体例来自模板与档案，
不来自模型的即兴发挥。

两个决策点都归它，且都是"小模型"档：语义判重（路径不同、功能相同）与章节落位
都需要语义理解，但输出都被限制成封闭选择 + 理由（需求 5.2.3）。
"""

from __future__ import annotations

from .catalog import AgentSpec

__all__ = ["SPEC"]

SPEC = AgentSpec(
    name="architect",
    title="文档架构师",
    single_duty="在给定候选章节中选择落位、判语义重复；不得创造章节。",
    tools=("outline_read", "source_query", "trace_map", "slot_plan"),
    decision_points=("dedup.semantic_duplicate", "placement.section_slot"),
    handoff="composer",
)
