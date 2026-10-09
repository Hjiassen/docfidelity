"""Curator · 模板馆员（DD-12.1 第 1 行 / 需求 5.4）。

职责：模板入库解析、契约生成、指纹与版本比对。
输出契约：:class:`~docfidelity.agents.outputs.TemplateConfirmResult` —— 在候选清单内
确认样式别名与版块定位。
handoff：→ Architect。

职责边界（DD-12.1 的"骨架要点"）：**只能确认，不得新增候选**。候选集由确定性代码
算出（契约里的样式与版块），模型只做选择——这是"候选注入代替自由生成"在第一个
Agent 上的落地（需求 5.2.2）。
"""

from __future__ import annotations

from .catalog import AgentSpec

__all__ = ["SPEC"]

SPEC = AgentSpec(
    name="curator",
    title="模板馆员",
    single_duty="从候选清单中确认样式别名与版块定位；不得新增候选。",
    tools=(
        "template_parse",
        "contract_build",
        "block_index",
        "fingerprint_diff",
        "registry_put",
    ),
    decision_points=("template.parse",),
    handoff="architect",
)
