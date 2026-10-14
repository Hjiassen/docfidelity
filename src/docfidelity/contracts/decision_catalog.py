"""声明目录：决策点分级表 + 五 Agent 规格（DD-12.1 / DD-12.3 / 需求 5.2.3、5.4）。

一处分层约定（`routing` 与 `tools` 同此）
---------------------------------------
* **声明**一律用 ``dataclass``：它们是给人与 CI 看的**事实记录**，不进 JSON，
  也不需要 `format_version`。
* **载荷**一律用 :class:`~docfidelity.contracts.base.FormatModel`：它们要序列化、
  进留痕、被前端与报告消费。

把声明也做成 pydantic 模型的代价是"为了存一个类型引用而开 `arbitrary_types_allowed`"，
而那等于关掉契约层最重要的一条保护（`extra="forbid"` 挡不住被关掉之前的任何事）。

为什么决策点要显式成表
----------------------
需求 5.2.3 的原话是"每个决策点都必须在设计阶段显式归类，否则实现时会出现
'顺手让模型判断一下'的蔓延"。故 :data:`DECISION_POINTS` **逐行对应那张分级表**：
条目数、归属、回退来源都能逐条比对。增删一个决策点必须同时改这张表与配置——
这就是把"防止范围蔓延"从评审项变成可执行约束的地方。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final, Literal

from docfidelity.contracts.base import FormatModel
from docfidelity.contracts.config import DecisionLevel, DecisionRoute, DecisionStrategy
from docfidelity.contracts.errors import EngineError, ErrorCode

from .decision_outputs import (
    AGENT_OUTPUT_TYPES,
    AgentName,
    ConventionAdvice,
    CopyRewrite,
    DuplicateJudgement,
    PlacementChoice,
    ReviewNote,
)

__all__ = [
    "AGENT_SPECS_VERSION",
    "DECISION_POINTS",
    "DECISION_POINT_IDS",
    "DEFAULT_CONFIDENCE_THRESHOLD",
    "DEFAULT_CONTEXT_BUDGET",
    "MAX_TOOLS_PER_AGENT",
    "MODEL_ROUTED",
    "ROUTE_LEVEL",
    "ROUTE_STRATEGY",
    "DecisionPointDef",
    "FallbackSource",
    "FallbackSpec",
    "RouteKind",
    "decision_point",
    "validate_decision_catalog",
]

#: 决策点归属（需求 5.2.3 的三档 + 人工）。**取值来源是契约**
#: （`contracts.config.DecisionRoute`），这里只做别名——两处各写一份字面量集合，
#: 漂移的后果是"配置里写的路由名运行时认不出来"。
RouteKind = DecisionRoute

#: 规则默认值的取值来源。每一条都指向一个**已存在**的确定性实现（见 `FallbackSpec.owner`）。
FallbackSource = Literal[
    "contract",  # 模板契约里已有确定答案
    "ruleset",  # 规则集的断言
    "kernel",  # 确定性代码（解析 / 指纹 / 版本比对），本不该问模型
    "geometry",  # 几何纯函数
    "profile",  # 档案声明（章节骨架、绑定的槽位）
    "template_text",  # 退化为模板化描述（关闭大模型后的文案路径）
    "priority_table",  # 事实源优先级表（FR-M1-11）
    "omit",  # 不产出结论
]

#: 逐决策点的上下文预算（NFR-25：不投喂完整契约与全量事实）。单位是"个片段"而不是
#: token——内核不该认识具体模型的 tokenizer，切片由检索侧执行。
DEFAULT_CONTEXT_BUDGET: Final = 32

#: 未配置时的置信度阈值下界（DD-12.3 的 ``threshold: float = 0.6`` 是**记录下限**，
#: 这里的 0.7 是**采用门槛**：低于它不得自动采用）。两者语义不同，故取不同常量名。
DEFAULT_CONFIDENCE_THRESHOLD: Final = 0.7

#: 单个 Agent 的工具数上限（NFR-25 / 需求 5.2.2：工具数 ≤ 7 且参数扁平）。
MAX_TOOLS_PER_AGENT: Final = 7

#: 五 Agent 规格的版本。与 `instructions.INSTRUCTIONS_VERSION` 分开：一个管"给模型看什么"，
#: 一个管"有哪五个角色、各自能碰什么"。混在一起会让"改了一句提示词"看起来像"改了角色划分"。
AGENT_SPECS_VERSION: Final = 1


# --------------------------------------------------------------------- 决策点
@dataclass(frozen=True)
class FallbackSpec:
    """某个决策点走规则默认值时的取值来源（DD-12.3 的 ``FallbackSpec``）。

    ``owner`` 是**唯一实现所在**。取值由那里的确定性代码算出，本表只声明"从哪取"——
    在声明里再算一遍就出现了第二份实现，而两份实现的漂移表现为
    "报告说按优先级表裁决了、实际是另一个值"。
    """

    source: FallbackSource
    owner: str
    detail: str
    mark_pending: bool = True

    def as_record(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "owner": self.owner,
            "detail": self.detail,
            "mark_pending": self.mark_pending,
        }


@dataclass(frozen=True)
class DecisionPointDef:
    """一个决策点的声明（DD-12.3 的 ``DecisionPoint``）。

    ``output_type`` 为 ``None`` 表示该点**根本不调模型**（``rule`` / ``human``）——
    这不是"还没配输出类型"，而是"它没有模型输出"。
    """

    id: str
    title: str
    kind: RouteKind
    owner_agent: AgentName | None
    fallback: FallbackSpec
    output_type: type[FormatModel] | None = None
    context_budget: int = DEFAULT_CONTEXT_BUDGET
    baseline_threshold: float = 0.9
    enabled_by_baseline: bool = True

    def as_record(self) -> dict[str, Any]:
        """可序列化视图（验收 41「决策点归属可查」的数据面）。"""
        return {
            "id": self.id,
            "title": self.title,
            "declared": self.kind,
            "owner_agent": self.owner_agent,
            "output_type": self.output_type.__name__ if self.output_type else None,
            "context_budget": self.context_budget,
            "baseline_threshold": self.baseline_threshold,
            "enabled_by_baseline": self.enabled_by_baseline,
            "fallback": self.fallback.as_record(),
        }


DECISION_POINTS: tuple[DecisionPointDef, ...] = (
    DecisionPointDef(
        id="layout.decide",
        title="版式决策（标题样式 / 列宽 / 版心 / 分页 / 编号）",
        kind="rule",
        owner_agent="renderer",
        fallback=FallbackSpec(
            source="contract",
            owner="docfidelity.registry.contract.ContractQuery",
            detail="契约里已有确定答案；问模型只会引入不确定性（需求 4.4.4 查契约）",
            mark_pending=False,
        ),
    ),
    DecisionPointDef(
        id="gate.judge",
        title="门禁判定（样式 / 版式 / 术语 / 完整性 / 追溯）",
        kind="rule",
        owner_agent="inspector",
        fallback=FallbackSpec(
            source="ruleset",
            owner="docfidelity.qa.engine.run_rule_set",
            detail="能写成断言且必须可复现；唯一裁决出口是 qa.can_finalize",
            mark_pending=False,
        ),
    ),
    DecisionPointDef(
        id="template.parse",
        title="模板结构解析 / 契约生成 / 指纹计算与版本比对",
        kind="rule",
        owner_agent="curator",
        fallback=FallbackSpec(
            source="kernel",
            owner="docfidelity.registry.ingest_template",
            detail="需逐字段精确，禁止模型参与（需求 5.2.3）",
            mark_pending=False,
        ),
    ),
    DecisionPointDef(
        id="geometry.compute",
        title="几何计算（图片等比缩放、表格宽度归一）",
        kind="rule",
        owner_agent="renderer",
        fallback=FallbackSpec(
            source="geometry",
            owner="docfidelity.ooxml.geometry",
            detail="与语义无关的确定性问题，纯函数求值",
            mark_pending=False,
        ),
    ),
    DecisionPointDef(
        id="dedup.semantic_duplicate",
        title="变更点的语义判重（路径不同、功能相同）",
        kind="small_model",
        owner_agent="architect",
        output_type=DuplicateJudgement,
        fallback=FallbackSpec(
            source="omit",
            owner="docfidelity.ingest.merge",
            detail="判不出即保守取「不重复」（路径已由规则去重）并标待确认送裁决队列",
            mark_pending=True,
        ),
    ),
    DecisionPointDef(
        id="placement.section_slot",
        title="章节内容落位（候选清单已给出）",
        kind="small_model",
        owner_agent="architect",
        output_type=PlacementChoice,
        fallback=FallbackSpec(
            source="profile",
            owner="docfidelity.compose.builder.compose",
            detail="按档案声明的章节骨架顺序落位；无候选则不落位并标待确认",
            mark_pending=True,
        ),
    ),
    DecisionPointDef(
        id="convention.advisory",
        title="体例冲突的裁决建议",
        kind="small_model",
        owner_agent="inspector",
        output_type=ConventionAdvice,
        fallback=FallbackSpec(
            source="ruleset",
            owner="docfidelity.qa.engine.run_rule_set",
            detail="只出建议，冲突本身按规则集记 Finding 交人裁决",
            mark_pending=True,
        ),
    ),
    DecisionPointDef(
        id="copy.generate",
        title="文案生成与改写（可选）",
        kind="optional_large_model",
        owner_agent="composer",
        output_type=CopyRewrite,
        fallback=FallbackSpec(
            source="template_text",
            owner="docfidelity.compose.builder.compose",
            detail="关闭后退化为模板化描述，链路不受影响（需求 5.2.3）",
            mark_pending=False,
        ),
    ),
    DecisionPointDef(
        id="review.document",
        title="文档评审（缺失 / 薄弱 / 矛盾，可选）",
        kind="optional_large_model",
        owner_agent="inspector",
        output_type=ReviewNote,
        fallback=FallbackSpec(
            source="omit",
            owner="docfidelity.agents.outputs.QaVerdict",
            detail="不产出评审结论；评审结论本就不作为阻断依据",
            mark_pending=False,
        ),
    ),
    DecisionPointDef(
        id="source.conflict",
        title="事实源冲突裁决",
        kind="human",
        owner_agent=None,
        fallback=FallbackSpec(
            source="priority_table",
            owner="docfidelity.ingest.merge.PriorityTable",
            detail="属业务判断，模型不参与；平手记 unresolved 并送人工（FR-M1-11）",
            mark_pending=True,
        ),
    ),
)

#: 决策点标识集合。**目录是唯一来源**：配置里出现目录之外的 id 视为声明错误。
DECISION_POINT_IDS: Final[frozenset[str]] = frozenset(point.id for point in DECISION_POINTS)

_BY_ID: Final[dict[str, DecisionPointDef]] = {point.id: point for point in DECISION_POINTS}

#: 归属 → 决策点分级（`contracts.config.DecisionLevel`，TASK-P0-17 的评测依据）。
ROUTE_LEVEL: dict[RouteKind, DecisionLevel] = {
    "rule": "auto",
    "small_model": "auto",
    "optional_large_model": "confirm",
    "human": "manual",
}

#: 归属 → 采用策略。``rule_then_model`` 的表达是"规则先给出一个保守取值，
#: 模型可用且达标时用模型结论覆盖"——这正是"每个决策点都必须有规则默认值"的字面含义。
ROUTE_STRATEGY: dict[RouteKind, DecisionStrategy] = {
    "rule": "rule_only",
    "small_model": "rule_then_model",
    "optional_large_model": "rule_then_model",
    "human": "rule_only",
}

#: 调模型的归属。规则与人工两档**没有模型输出**，不该被要求配一个输出契约。
MODEL_ROUTED: Final[frozenset[str]] = frozenset({"small_model", "optional_large_model"})


def decision_point(point_id: str) -> DecisionPointDef:
    """按标识取决策点声明；未登记即抛（配置写错必须当场可见）。"""
    point = _BY_ID.get(point_id)
    if point is None:
        raise EngineError(
            ErrorCode.CONFIG_INVALID,
            f"未登记的决策点：{point_id}",
            where={"decision_point": point_id},
            remediation=[
                "新增决策点必须先归类并登记到 agents/catalog.py 的 DECISION_POINTS",
                "或在配置中去掉该条目",
            ],
        )
    return point


def validate_decision_catalog() -> None:
    """目录自检：每条声明都要指向真实存在的东西。

    三件事都必须成立，缺任何一条都会让"归属可查"变成"归属看起来可查"：

    1. 调模型的点必须有输出契约（否则适配层拿不到 schema，只能自由生成）；
    2. 声明了归属 Agent 的点，该 Agent 必须存在（否则界面上会点到一个不存在的角色）；
    3. 回退来源的 ``owner`` 必须写出来（"来源：无"等于没有回退路径，
       而 NFR-26 要求每个点都有规则默认值）。
    """
    seen: set[str] = set()
    for point in DECISION_POINTS:
        if point.id in seen:
            raise EngineError(
                ErrorCode.CONFIG_INVALID,
                f"决策点标识重复：{point.id}",
                where={"decision_point": point.id},
            )
        seen.add(point.id)
        if point.kind in MODEL_ROUTED and point.output_type is None:
            raise EngineError(
                ErrorCode.CONFIG_INVALID,
                f"决策点 {point.id} 归属 {point.kind} 却没有输出契约",
                where={"decision_point": point.id},
                remediation=["在 agents/outputs.py 定义其输出模型并挂到 DECISION_POINTS 上"],
            )
        if point.owner_agent is not None and point.owner_agent not in AGENT_OUTPUT_TYPES:
            raise EngineError(
                ErrorCode.INTERNAL,
                f"决策点 {point.id} 声明的归属 Agent {point.owner_agent} 不存在",
                where={"decision_point": point.id},
            )
        if not point.fallback.owner:
            raise EngineError(
                ErrorCode.CONFIG_INVALID,
                f"决策点 {point.id} 没有声明规则默认值的实现位置（NFR-26 要求每个点都有兜底）",
                where={"decision_point": point.id},
            )
