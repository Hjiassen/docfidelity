"""门禁：规则、判定、豁免、报告与追溯边（DD-10 / DD-3.2（6））。

一条纪律必须写进模型里：**「门禁未执行」与「门禁全通过」必须可区分**。
前者的表现是 ``Report`` 不存在（接口报 ``E_REPORT_MISSING``），后者是
``Report.verdict == "pass"``。任何把二者混淆的设计（例如用空数组表示"全通过"）
都会让"忘了跑门禁"伪装成"质量合格"。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any, ClassVar, Final, Literal

from pydantic import Field, model_validator

from .base import FormatModel

RuleCategory = Literal["style", "layout", "terminology", "integrity", "trace", "spotcheck", "meta"]

RuleSeverity = Literal["blocker", "major", "minor"]

Verdict = Literal["pass", "warning", "blocked"]
"""门禁结论。``blocked`` 表示存在未豁免的 blocker。"""

#: 结论的人读文案（``Report.summary`` 用）。放在契约层是因为**它由结论决定**：
#: 两处各写一份映射，会出现"结论是 warning、摘要写着阻断"这种自相矛盾的报告。
VERDICT_TEXT: Final[dict[str, str]] = {
    "pass": "通过",
    "warning": "有告警",
    "blocked": "阻断",
}

FindingStatus = Literal["open", "waived", "fixed"]

WaiverScope = Literal["instance", "project", "global"]


def verdict_of(findings: Sequence[Finding]) -> Verdict:
    """结论算法（DD-10.3）。**全项目唯一实现**。

    只统计**未豁免**的判定；``blocked`` 的门槛是"存在未豁免的 blocker"，与 major /
    minor 的条数无关——把它们混在一起会让"有 20 条 minor"与"有 1 条 blocker"得出同一结论。

    为什么在契约层而不是 ``qa``
    --------------------------
    重算结论的场合不止门禁一处：人工裁决把一条 blocker 豁免掉之后，服务端必须给出
    **新的**结论，而 ``server`` 不得 import ``qa``（决策四：内核零 Web 依赖的对称约束）。
    两处各写一份的症状很具体——"报告页说已解除阻断、作业卡片还写着阻断"。
    它在契约层是自洽的：算法只读 :class:`Finding` 的两个契约字段
    （``severity`` / ``status``），不碰 OOXML、不碰规则语义。
    """

    open_items = [item for item in findings if item.status != "waived"]
    if any(item.severity == "blocker" for item in open_items):
        return "blocked"
    return "warning" if open_items else "pass"


def summarize(
    findings: Sequence[Finding],
    *,
    verdict: Verdict,
    skipped: Sequence[str] = (),
) -> str:
    """人读摘要（FR-M5-15 的 ``Report.summary``）。

    ``skipped`` 必须进来：DD-10.3 要求"缺能力跳过必须进 summary"。一条规则因为
    依赖能力缺失而没跑，与"跑过且通过"是完全不同的两件事，而后者正是沉默带来的假象。
    """
    open_items = [item for item in findings if item.status != "waived"]
    counts = {"blocker": 0, "major": 0, "minor": 0}
    for item in open_items:
        counts[item.severity] = counts.get(item.severity, 0) + 1
    parts = [
        f"结论：{VERDICT_TEXT[verdict]}",
        f"未豁免 blocker {counts['blocker']} / major {counts['major']} / minor {counts['minor']}",
        f"已豁免 {len(findings) - len(open_items)}",
    ]
    if skipped:
        parts.append(f"跳过 {len(skipped)} 条（缺依赖能力）：{', '.join(sorted(skipped))}")
    return "；".join(parts)


class Location(FormatModel):
    """问题的定位。定位必须**足够具体**，以便界面直接跳到对应位置。

    内容类问题给 ``unit_id`` + ``chapter_path``；样式类问题给 ``part`` + ``xpath``；
    目检类问题补 ``page_hint``。
    """

    unit_id: str | None = Field(default=None, description="定位到内容单元")
    chapter_path: list[str] = Field(
        default_factory=list, description="章节路径，如 ['概述','范围']"
    )
    part: str | None = Field(default=None, description="OOXML 部件名（样式 / 版式类命中才有）")
    xpath: str | None = Field(default=None, description="精确到节点")
    page_hint: int | None = Field(default=None, ge=1, description="目检图页码（若可用）")


class Rule(FormatModel):
    """一条门禁规则。

    规则以 YAML 声明、以 Python 断言函数实现（DD-10.1）。``params`` 承载阈值类参数，
    使同一实现可被多套规则集以不同阈值复用。
    """

    format_family: ClassVar[str] = "qa"

    rule_id: str = Field(description="规则编号，如 FR-M5-07")
    category: RuleCategory
    severity: RuleSeverity
    title: str
    description: str | None = None
    enabled: bool = True
    params: dict[str, Any] = Field(default_factory=dict)
    requires: list[str] = Field(
        default_factory=list, description="依赖的引擎能力标识；缺失时该规则应被跳过而非报错"
    )
    convention_ref: str | None = Field(
        default=None, description="关联档案 conventions.id，用于说明规则来自哪条体例约定"
    )


class Finding(FormatModel):
    """一条判定结果。

    ``finding_id`` 为什么是一个字段
    ------------------------------
    界面要能**指认**一条判定才能对它提交裁决（``POST /findings/{fid}/decisions``），
    而在那之前 ``Finding`` 只有内容、没有身份——调用方唯一能用的坐标是"报告里第几条"，
    那等于让前端自己算一个标识（DD-14.3 禁止的"前端推断"），而且一旦有人重排
    ``findings``，"第 3 条"就悄悄指向了另一条规则。

    标识由 ``qa`` 在产出报告时赋值（``<报告标识>@<序号>``），故它**不参与**任何判定：
    是用户级可选项，缺失时接口层按"这条判定不可寻址"处理，而不是回退到下标。
    """

    format_family: ClassVar[str] = "qa"

    rule_id: str
    severity: RuleSeverity
    message: str = Field(description="面向使用者的中文说明")
    location: Location = Field(default_factory=Location)
    evidence: str | None = Field(default=None, description="命中的具体内容片段，供人工复核")
    suggestion: str | None = Field(default=None, description="可执行建议")
    status: FindingStatus = "open"
    waiver_id: str | None = None
    finding_id: str | None = Field(
        default=None, description="可寻址标识（``<报告标识>@<序号>``）；由 qa 产出报告时赋值"
    )


class Waiver(FormatModel):
    """豁免。豁免必须有理由与操作人，并可设置过期时间。"""

    format_family: ClassVar[str] = "qa"

    waiver_id: str
    rule_id: str
    reason: str
    operator: str
    created_at: datetime
    expires_at: datetime | None = Field(default=None, description="过期后豁免自动失效")
    scope: WaiverScope = "instance"
    target: str | None = Field(default=None, description="instance 范围时的具体对象标识")

    def is_active(self, *, now: datetime) -> bool:
        """是否仍然生效（未过期）。"""
        return self.expires_at is None or self.expires_at > now


AdjudicationAction = Literal["accept", "configure", "waive"]
"""人工裁决的**规范动作**（FR-M7-06 / NFR-15）。

只有这三种，不再多：每多一种就多一条"绕过门禁"的路径。三者的语义边界必须清楚——
``accept`` 是"我认可这条判定"，``configure`` 是"我去改配置，改完重来"，
``waive`` 是"我以责任人身份让它不阻断本次交付"。前两者**都不改变门禁结论**，
只有 ``waive`` 会——所以只有 ``waive`` 被强制要求理由。
"""


class Adjudication(FormatModel):
    """一次人工裁决的留痕（FR-M7-06 / NFR-15 / 需求 4.6.7）。

    为什么与 :class:`Waiver` 分开
    -----------------------------
    豁免只是三种规范动作之一，而且它与``接受``在**数据上**完全不同：豁免有有效期、
    有作用域、会改变 ``verdict``；接受与改配置只是留痕。把接受也塞进 ``waivers``，
    会让"这份报告被豁免了几条"这个数字变成"这份报告被人看过几条"，而报告汇总卡
    上那个数字正是定稿判据。

    ``subject_id`` 而不是 ``finding_id``
    -----------------------------------
    待裁决项有两类：门禁判定（:attr:`Finding.finding_id`）与**待确认的决策项**
    （``Decision.decision_id``，模型低置信走了规则默认值的那条）。DD-13.2 只给了
    一条裁决提交路径，两类待办共用同一套规范动作——为决策项另开一条路径，
    等于让"裁决"的语义在两处各定义一次。故这里按"**待裁决项**"命名。
    """

    format_family: ClassVar[str] = "qa"

    adjudication_id: str
    subject_id: str = Field(description="待裁决项标识：门禁判定 id 或决策项 id")
    action: AdjudicationAction
    operator: str = Field(description="责任人；必填，可追溯性的一半（NFR-15）")
    reason: str | None = Field(default=None, description="理由；``action=waive`` 时必填")
    created_at: datetime
    waiver_id: str | None = Field(default=None, description="仅 ``action=waive``")


class RuleSet(FormatModel):
    """规则集：内置规则 + 项目覆盖。"""

    format_family: ClassVar[str] = "qa"

    rule_set_id: str
    title: str | None = None
    included_rules: list[str] = Field(default_factory=list)
    excluded_rules: list[str] = Field(default_factory=list)
    severity_overrides: dict[str, RuleSeverity] = Field(default_factory=dict)
    param_overrides: dict[str, dict[str, Any]] = Field(default_factory=dict)


class Report(FormatModel):
    """门禁报告。

    ``verdict`` 的算法由服务端给出，前端**不得推断**（唯一实现 = :func:`verdict_of`）：

    * 存在未豁免的 ``blocker`` -> ``blocked``
    * 否则存在未豁免的 ``major`` / ``minor`` -> ``warning``
    * 否则 -> ``pass``

    ``skipped`` 为什么是一个字段
    ---------------------------
    "跳过了哪几条规则、因为缺哪项能力"原本只写在 ``summary`` 那句散文里。
    一旦需要**重算**摘要（人工裁决之后必须重算，否则报告自相矛盾），散文里的话就
    提取不出来了——于是"跳过"这条信息会在第一次裁决后静默消失。做成字段之后，
    重算只需 :meth:`recompute`，而"跳过"作为数据可被查、可被断言。
    """

    format_family: ClassVar[str] = "qa"

    report_id: str
    job_id: str
    generated_at: datetime
    findings: list[Finding] = Field(default_factory=list)
    waivers: list[Waiver] = Field(default_factory=list)
    adjudications: list[Adjudication] = Field(
        default_factory=list,
        description="人工裁决留痕（FR-M7-06）；与 waivers 并存：豁免只是三种动作之一",
    )
    verdict: Verdict = "pass"
    summary: str | None = None
    skipped: list[str] = Field(
        default_factory=list,
        description="因缺依赖能力而跳过的规则（``rule_id(缺 能力)``）；**不是**「跑过且通过」",
    )

    def open_findings(self) -> list[Finding]:
        """返回未豁免的判定项。"""
        return [item for item in self.findings if item.status != "waived"]

    def adjudication_of(self, subject_id: str) -> Adjudication | None:
        """某条待裁决项最近一次裁决（后出现的胜出）。

        "最近一次"而不是"第一次"：同一条项被裁决两次（先接受、再改成豁免）时，
        生效的是后一次，而 `open_findings` 之类读的是**当前状态**。
        """
        found = [item for item in self.adjudications if item.subject_id == subject_id]
        return found[-1] if found else None

    def recompute(self) -> Report:
        """按**当前** ``findings`` 重算 ``verdict`` 与 ``summary``，返回新报告。

        报告一经产出即不可变，但人工裁决**就是**对它的合法修改（DD-10.3 的豁免）：
        判定状态变了而结论不变，等于报告自相矛盾。重算走 :func:`verdict_of` /
        :func:`summarize` 这两份唯一实现，不在此处另写一遍规则。
        """
        verdict = verdict_of(self.findings)
        return self.model_copy(
            update={
                "verdict": verdict,
                "summary": summarize(self.findings, verdict=verdict, skipped=self.skipped),
            }
        )

    def counts_by_severity(self) -> dict[str, int]:
        counts = {"blocker": 0, "major": 0, "minor": 0}
        for item in self.open_findings():
            counts[item.severity] = counts.get(item.severity, 0) + 1
        return counts


class TraceEdge(FormatModel):
    """追溯边：内容单元 <- 领域对象。"""

    format_family: ClassVar[str] = "qa"

    unit_id: str
    object_ref: str = Field(description="领域对象引用，形如 '<kind>:<key 各段以 / 连接>'")
    source: str
    locator: str


class TraceMatrix(FormatModel):
    """追溯矩阵。

    * ``orphans``：在文档里但查不到来源的单元
    * ``unused``：在事实源里但未进入文档的对象

    两者都是**信息**而非错误——但必须呈现出来，让使用者判断是否遗漏。
    """

    format_family: ClassVar[str] = "qa"

    edges: list[TraceEdge] = Field(default_factory=list)
    orphans: list[str] = Field(default_factory=list)
    unused: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_edges(self) -> TraceMatrix:
        seen: set[tuple[str, str]] = set()
        for edge in self.edges:
            seen.add((edge.unit_id, edge.object_ref))
        # 仅做去重性检查：重复边会让追溯计数失真
        if len(seen) != len(self.edges):
            raise ValueError("追溯矩阵存在重复边（同一单元同一对象）")
        return self


class SnapshotDiff(FormatModel):
    """稳定化比对结果（DD-10.4）。"""

    format_family: ClassVar[str] = "qa"

    baseline_ref: str
    candidate_ref: str
    normalized: bool = Field(
        default=True, description="是否已归一化易变标识；False 时结论不可用于门禁"
    )
    added: list[str] = Field(default_factory=list, description="新增节点路径")
    removed: list[str] = Field(default_factory=list)
    changed: list[str] = Field(default_factory=list)


__all__ = [
    "VERDICT_TEXT",
    "Adjudication",
    "AdjudicationAction",
    "Finding",
    "FindingStatus",
    "Location",
    "Report",
    "Rule",
    "RuleCategory",
    "RuleSet",
    "RuleSeverity",
    "SnapshotDiff",
    "TraceEdge",
    "TraceMatrix",
    "Verdict",
    "Waiver",
    "WaiverScope",
    "summarize",
    "verdict_of",
]
