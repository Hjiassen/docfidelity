"""Renderer · 版式落地（DD-12.1 第 4 行 / 需求 5.4）。

职责：调确定性渲染工具把 IR 落成 docx。
输出契约：:class:`~docfidelity.agents.outputs.RenderOutcome` —— 报告调用了哪些渲染
工具、以及写盘护栏的结论。
handoff：→ Inspector。

职责边界（DD-12.1 的"骨架要点"）：**只调用渲染工具，不得请求任何 XML 操作**。
这在工具面上是硬保证而不只是措辞：白名单里不存在任何指向 ``ooxml`` 的实现位置，
`agents.tools.validate_spec` 在加载期就会拒绝这样一条声明（决策三）。

本 Agent 名下的两个决策点都是 ``rule``：版式取值来自模板契约，几何计算是纯函数
（需求 5.3 的边界表把"分页 / 列宽 / 图片的几何计算"明确划给确定性一侧）。
"""

from __future__ import annotations

from .catalog import AgentSpec

__all__ = ["SPEC"]

SPEC = AgentSpec(
    name="renderer",
    title="版式落地",
    single_duty="只调用渲染工具把 IR 落成 docx；不得请求任何 OOXML 操作。",
    tools=("render_inject", "pagination_apply", "figure_place", "toc_refresh"),
    decision_points=("layout.decide", "geometry.compute"),
    handoff="inspector",
)
