"""三层护栏（DD-12.5 / 需求 5.6）。

===============================  ============================================
输入护栏                         指令进入前：编造事实 / 改模板原件 / 绕门禁
工具输入护栏                     高危工具调用前：写盘覆盖、路径越界、作用域外
输出护栏                         **写盘前**：契约违规、占位残留、术语禁用、图越界
===============================  ============================================

三层的处置方式刻意不同
----------------------
* 输入护栏是**关键词筛查**，不是语义分类器。诚实地说：它拦得住明显越权的指令，
  拦不住绕着说的。真正的防线不在提示词也不在这张词表，而在**决策三**——
  引擎从不给 Agent 任何 OOXML 直写工具，于是"改模板原件"这类要求即使被说出来也无从执行。
  把筛查当成主防线，会让人误以为提示词可以承担安全责任。
* 工具输入护栏**只有一处实现**（`agents.tools.guard_call`），本模块只转发。
* 输出护栏**不做判定**，只读取渲染层已有的结论（``RenderResult.guardrail_blockers``）——
  写盘前该不该落盘是渲染层护栏的判断，它已经在 ``render.pipeline`` 里实现过一次
  （DD-10.7）。这里再判一遍的后果是"两处结论不一致，而产物已经写出去了"。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from pydantic import Field

from docfidelity.contracts.base import FormatModel
from docfidelity.contracts.errors import EngineError, ErrorCode

from .tools import guard_call
from .tools.spec import ToolApprovalPolicy, ToolFailure, ToolSpec

__all__ = [
    "INSTRUCTION_RULES",
    "InstructionGuardResult",
    "InstructionIntent",
    "OutputGuardResult",
    "ToolInputGuardResult",
    "check_instruction",
    "check_tool_input",
    "guard_output",
    "require_instruction",
]

#: 输入护栏要拦的三类意图（DD-12.5 的"拦截内容"一列）。
InstructionIntent = Literal["fabricate_facts", "edit_template", "bypass_gate"]


@dataclass(frozen=True)
class InstructionRule:
    """一条意图规则。``cues`` 命中任一即判定为该意图。"""

    intent: InstructionIntent
    cues: tuple[str, ...]
    reason: str
    remediation: tuple[str, ...]


#: 关键词表。**刻意保持短而显式**：词表越长越容易误伤（"忽略"这种词在正常诉求里也会出现），
#: 而一条总在误报的护栏会被关掉——那时它连"拦得住明显越权"这点价值也没有了。
INSTRUCTION_RULES: tuple[InstructionRule, ...] = (
    InstructionRule(
        intent="fabricate_facts",
        cues=("编造", "虚构", "伪造", "不用事实", "不需要事实源", "忽略事实源"),
        reason="要求生成事实源无法支撑的内容，违反「产出必须可追溯」（FR-M6-07 / NFR-15）",
        remediation=("把该内容登记为事实源后再生成", "或在内容中显式标注为待确认"),
    ),
    InstructionRule(
        intent="edit_template",
        cues=("修改模板原件", "改模板原件", "直接改模板", "覆盖模板", "改样式层"),
        reason="模板样式层零改动是本引擎的底线（FR-M4-01 / 验收 34）；模板只能重新入库",
        remediation=("调整模板原件后重新入库生成新版本", "或改用项目级配置修正定位与映射"),
    ),
    InstructionRule(
        intent="bypass_gate",
        cues=("绕过门禁", "跳过门禁", "跳过检查", "忽略门禁", "不要检查", "不做门禁"),
        reason="门禁结论是唯一裁决出口（qa.can_finalize），不可被指令绕过",
        remediation=("对具体发现提交裁决（接受 / 改配置 / 豁免并写明理由）",),
    ),
)


class InstructionGuardResult(FormatModel):
    """输入护栏结论。"""

    allowed: bool
    intent: InstructionIntent | None = None
    matched: str | None = Field(default=None, description="命中的关键词")
    reason: str = ""
    remediation: list[str] = Field(default_factory=list)


class ToolInputGuardResult(FormatModel):
    """工具输入护栏结论（与工具调用闸门同源，不另做判定）。"""

    allowed: bool
    failure: ToolFailure | None = None


class OutputGuardResult(FormatModel):
    """输出护栏结论（读取渲染层既有判定，不重算）。"""

    allowed: bool
    blockers: list[str] = Field(default_factory=list)
    reason: str = ""


def check_instruction(text: str) -> InstructionGuardResult:
    """输入护栏：指令进入前筛查三类越权意图。"""
    lowered = text.lower()
    for rule in INSTRUCTION_RULES:
        for cue in rule.cues:
            if cue in lowered:
                return InstructionGuardResult(
                    allowed=False,
                    intent=rule.intent,
                    matched=cue,
                    reason=rule.reason,
                    remediation=list(rule.remediation),
                )
    return InstructionGuardResult(allowed=True)


def require_instruction(text: str) -> None:
    """指令不通过即抛 ``E_GUARDRAIL_REJECTED``，附可操作清单。

    单独给一个"抛异常"的入口，是为了让编排层无法"顺手忽略"结论：
    只看返回值的调用方总有一天会忘掉那个 ``if``。
    """
    verdict = check_instruction(text)
    if verdict.allowed:
        return
    raise EngineError(
        ErrorCode.GUARDRAIL_REJECTED,
        f"指令被输入护栏拒绝：{verdict.reason}",
        where={"intent": verdict.intent or "", "matched": verdict.matched or ""},
        remediation=verdict.remediation,
    )


def check_tool_input(
    spec: ToolSpec,
    args: FormatModel,
    policy: ToolApprovalPolicy | None = None,
) -> ToolInputGuardResult:
    """工具输入护栏：转发到 :func:`docfidelity.agents.tools.guard_call`。"""
    failure = guard_call(spec, args, policy or ToolApprovalPolicy())
    return ToolInputGuardResult(allowed=failure is None, failure=failure)


def guard_output(result: Any) -> OutputGuardResult:
    """输出护栏：读渲染层的写盘护栏结论，**在写盘前**判定是否放行。

    入参是 ``render.RenderResult``（鸭子类型，只读一个属性）——不静态 import
    ``render`` 是为了让"护栏结论的唯一实现"这件事一眼可见：本函数不计算任何东西。

    读数失败**不静默放行**：拿不到护栏结论却继续，等于把"没检查"伪装成"检查通过"。
    """
    blockers = getattr(result, "guardrail_blockers", None)
    if blockers is None:
        raise EngineError(
            ErrorCode.INTERNAL,
            "输出护栏拿不到写盘护栏结论（入参不是渲染结果，或该结果缺少 guardrail_blockers）",
            where={"type": type(result).__name__},
            remediation=["传入 render.RenderResult；护栏判定只由 render.pipeline 产出"],
        )
    codes = [str(getattr(item, "rule_id", item)) for item in blockers]
    if codes:
        return OutputGuardResult(
            allowed=False,
            blockers=codes,
            reason="渲染层已判定产物不可交付（样式引用悬挂 / 占位残留），不得写盘",
        )
    return OutputGuardResult(allowed=True, reason="渲染层护栏未命中 blocker")
