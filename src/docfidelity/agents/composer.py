"""Composer · 内容撰写（DD-12.1 第 3 行 / 需求 5.4）。

职责：生成章节内容 IR（含文案），套用术语变量。
输出契约：:class:`~docfidelity.agents.outputs.ContentIROutput` —— 内容 IR
加**事实外内容的显式标注**。
handoff：→ Renderer。

职责边界（DD-12.1 的"骨架要点"）：**只依据输入事实生成描述；事实外内容必须标注**。
标注不是礼貌性字段：没有它，"这段是模型写的" 就只存在于模型自己的记忆里，
而报告与裁决队列都需要一个可检索的句柄（NFR-15 决策可解释）。

``copy.generate`` 是"可选大模型"档——关闭后文案退化为模板化描述，
链路不受影响（需求 5.2.3）。故本 Agent 是唯一一个**没有模型也照常工作**的角色。
"""

from __future__ import annotations

from .catalog import AgentSpec

__all__ = ["SPEC"]

SPEC = AgentSpec(
    name="composer",
    title="内容撰写",
    single_duty="只依据输入事实生成描述与文案；事实外内容必须标注。",
    tools=("content_ir_build", "glossary_apply", "unit_section", "table_section"),
    decision_points=("copy.generate",),
    handoff="renderer",
)
