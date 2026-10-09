"""五 Agent 的结构化输出契约（DD-12.1）。

为什么这些模型**不放进 `contracts`**
------------------------------------
`contracts` 家族（`docs/spec/*/schema.json`）是对外交付承诺，其增删要走弃用流程。
而这五个模型是**模型 I/O 边界**：它们约束"模型必须吐什么形状"，只被薄适配层与
Agent 使用，前端不消费（前端看的是 `contracts.job.Decision` / `ModelCall`）。

把它们留在 `agents` 同时仍继承 :class:`~docfidelity.contracts.base.FormatModel`，
于是版本校验、`extra="forbid"`、前向兼容加载一样不少——但改它们**不动契约家族**，
符合 5.2.4 硬约束 2「升级模型不得改契约」的字面含义：换模型只改 instructions 与适配层。

复用而非重定义
--------------
凡是 `contracts` 里已有的结构，这里一律**引用**，绝不抄第二份：

* Composer 的 IR 用 :class:`~docfidelity.contracts.ir.ContentIR`；
* Inspector 的判定用 :class:`~docfidelity.contracts.qa.Verdict` 与 `Finding`。

两份 IR / 两份判定定义一定会漂移，而漂移的表现是"模型按 A 填、门禁按 B 判"。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final, Literal

from pydantic import Field

from docfidelity.contracts.base import FormatModel
from docfidelity.contracts.ir import ContentIR
from docfidelity.contracts.qa import Finding, Verdict

__all__ = [
    "AGENT_NAMES",
    "AGENT_OUTPUT_TYPES",
    "AgentName",
    "Annotation",
    "BlockRegionChoice",
    "ContentIROutput",
    "ConventionAdvice",
    "CopyRewrite",
    "DuplicateJudgement",
    "Placement",
    "PlacementChoice",
    "PlacementPlan",
    "QaVerdict",
    "RenderOutcome",
    "RenderStepRecord",
    "ReviewNote",
    "StyleAliasChoice",
    "TemplateConfirmResult",
    "chosen_label",
]

#: 五 Agent 的标识。**在输出契约模块定义**：DD-12.1 是一 Agent 一输出，
#: 故"有哪些 Agent"与"有哪些输出模型"是同一件事——两处各写一份名单必然漂移，
#: 而漂移的表现是"编排层认得一个 Agent、输出层不认得它"。
AgentName = Literal["curator", "architect", "composer", "renderer", "inspector"]

AGENT_NAMES: Final[tuple[AgentName, ...]] = (
    "curator",
    "architect",
    "composer",
    "renderer",
    "inspector",
)


# ------------------------------------------------------------------ 1. Curator
class StyleAliasChoice(FormatModel):
    """一次样式别名确认。

    ``alias`` 与 ``style_id`` **都必须来自候选集**（DD-12.1：不得新增候选）。
    两端都记，是为了让报告能直接指出"源文档的别名 X 被并到模板的 styleId Y"。
    """

    alias: str = Field(description="源文档中的样式别名（候选之一）")
    style_id: str = Field(description="目标模板中的 styleId（候选之一）")


class BlockRegionChoice(FormatModel):
    """一次版块定位确认。"""

    block_id: str = Field(description="模板包中的版块标识（候选之一）")
    region: str = Field(description="版块归属（候选之一，取值由 registry 的版块类型集合给出）")
    slot_id: str | None = Field(default=None, description="挂载槽位；不挂载则为空")


class TemplateConfirmResult(FormatModel):
    """Curator 输出：**在候选列表内**确认样式别名与版块定位。

    无法在候选中定位的项一律进 ``unconfirmed``，交人工——不得臆造一个别名或版块
    （职责边界：候选注入代替自由生成，见需求 5.2.2）。
    """

    agent: Literal["curator"] = "curator"
    style_aliases: list[StyleAliasChoice] = Field(default_factory=list)
    block_regions: list[BlockRegionChoice] = Field(default_factory=list)
    unconfirmed: list[str] = Field(
        default_factory=list, description="候选中找不到对应项的条目，原样回抛交人工"
    )
    notes: str | None = None


# ---------------------------------------------------------------- 2. Architect
class Placement(FormatModel):
    """一次章节落位。``chapter_path`` **必须是候选章节路径之一**。"""

    unit_ref: str = Field(description="内容单元引用（来自输入，不得虚构）")
    chapter_path: list[str] = Field(description="落位到的章节路径（候选之一）")
    slot_id: str | None = Field(default=None, description="落位到的槽位；不指定则用默认槽位")
    reason: str | None = Field(default=None, description="选择理由（NFR-15 决策可解释）")


class PlacementPlan(FormatModel):
    """Architect 输出：在给定候选章节中选择落位；**不得创造章节**。

    ``created_chapters`` 的语义是"模型试图新增章节时留下的痕迹"：契约层只做记录，
    由 `agents.catalog` 的守卫与报告决定如何呈现——引擎不会因为模型想要一个新章节
    就真的加一章（那会让体例脱离模板）。
    """

    agent: Literal["architect"] = "architect"
    placements: list[Placement] = Field(default_factory=list)
    unplaced: list[str] = Field(default_factory=list, description="候选中无处可放的单元引用")
    created_chapters: list[str] = Field(
        default_factory=list, description="模型请求但候选中不存在的新章节（违规痕迹，不做落地）"
    )
    notes: str | None = None


# ----------------------------------------------------------------- 3. Composer
class Annotation(FormatModel):
    """事实外内容标注（DD-12.1：事实外内容必须标注）。"""

    unit_id: str = Field(description="被标注的内容单元")
    kind: Literal["unsupported", "assumption", "pending"] = Field(
        description="unsupported 无事实支撑 / assumption 依上下文推断 / pending 待确认"
    )
    detail: str = Field(description="标注原因，供裁决队列展示")


class ContentIROutput(FormatModel):
    """Composer 输出：内容 IR + **事实外内容的显式标注**。

    ``generated_by_model`` 是本模块唯一的"解释性"字段：它让"这句文案是模型写的"
    可被报告点名（NFR-15 决策可解释）。IR 本身仍是 :class:`ContentIR`，没有第二份定义。
    """

    agent: Literal["composer"] = "composer"
    ir: ContentIR
    annotations: list[Annotation] = Field(default_factory=list)
    generated_by_model: list[str] = Field(
        default_factory=list, description="由模型生成文案的内容单元 id"
    )
    notes: str | None = None


# ----------------------------------------------------------------- 4. Renderer
class RenderStepRecord(FormatModel):
    """一步渲染的结果留痕。"""

    step: str = Field(description="渲染步骤名（取 render.STEP_ORDER 中的标识）")
    ok: bool = True
    detail: str | None = None


class RenderOutcome(FormatModel):
    """Renderer 输出：**只报告调用了哪些渲染工具**，不携带任何 OOXML 内容。

    命名偏离说明：DD-12.1 记为 ``RenderResult``，但 ``render.pipeline`` 已有同名
    数据类（渲染器的内部产出）。二者同义不同物，同名会让评审与检索都出错，故本层
    记为 ``RenderOutcome``；DD 名称保留在 :data:`AGENT_OUTPUT_TYPES` 的对照里。

    ``guardrail_blockers`` 是**写盘护栏的既有结论**（``render.RenderResult.guardrail_blockers``）
    的原样搬运，不重算——同一口径只能有一处实现（见 `agents.guardrails`）。
    非空即"没有写盘"，Agent 不得据此声称产物已就绪。
    """

    agent: Literal["renderer"] = "renderer"
    artifact_ref: str | None = Field(default=None, description="产物引用；被护栏拦下时为空")
    steps: list[RenderStepRecord] = Field(default_factory=list)
    guardrail_blockers: list[str] = Field(
        default_factory=list, description="写盘护栏命中的规则号；非空表示未写盘"
    )
    notes: str | None = None

    @property
    def wrote_artifact(self) -> bool:
        """是否真的落了盘（护栏命中即否）。"""
        return self.artifact_ref is not None and not self.guardrail_blockers


# ---------------------------------------------------------------- 5. Inspector
class QaVerdict(FormatModel):
    """Inspector 输出：结构化判定，**不作为阻断依据**。

    ``is_binding`` 恒为 ``False``，且用 ``Literal`` 而非 ``bool`` 表达——它不是一个
    可配置开关，而是本层的**职责边界**：唯一裁决出口是 ``qa.can_finalize``
    （需求 5.3：能写成规则的绝不问 LLM）。
    """

    agent: Literal["inspector"] = "inspector"
    verdict: Verdict = "pass"
    findings: list[Finding] = Field(default_factory=list)
    review_suggestions: list[str] = Field(
        default_factory=list, description="评审建议（缺失 / 薄弱 / 矛盾），仅供人参考"
    )
    drift_notes: list[str] = Field(default_factory=list, description="漂移提示")
    is_binding: Literal[False] = False


# ------------------------------------------------------------------ 决策点输出
# 下面这五个是**决策点级**输出，与上面的 Agent 级输出是两回事：
# Agent 输出约束"一个 Agent 交付什么"，决策点输出约束"一次决策回答什么"。
# 后者必须小且封闭——需求 5.2.3 对交给模型的三个点都限定了输出形态（封闭选择 + 理由），
# 让决策点的输出等于整个 Agent 输出，会把这些点变成自由生成。


class DuplicateJudgement(FormatModel):
    """语义判重的封闭选择（需求 5.2.3：输出被限制为"重复 / 不重复 + 理由"）。"""

    is_duplicate: bool
    reason: str


class PlacementChoice(FormatModel):
    """一次落位选择。``chapter_path`` 必须是候选之一。"""

    unit_ref: str
    chapter_path: list[str] = Field(default_factory=list)
    reason: str = ""


class ConventionAdvice(FormatModel):
    """体例冲突的裁决建议。**只出建议**，最终由人裁决。"""

    finding_ref: str
    suggestion: str
    reason: str = ""


class CopyRewrite(FormatModel):
    """一段文案改写。关闭模型后退化为模板化描述（需求 5.2.3）。"""

    unit_id: str
    text: str


class ReviewNote(FormatModel):
    """一条文档评审结论（缺失 / 薄弱 / 矛盾）。不作为阻断依据。"""

    unit_id: str | None = None
    kind: Literal["missing", "weak", "contradictory"] = "weak"
    detail: str


#: 五 Agent 输出类型的注册表：``AgentName -> 模型类``。
#:
#: 键是**Agent 名**而非类名，因为 `agents.catalog.AgentSpec` 按 Agent 名查找输出类型；
#: 键与值为一一对应，故不存在"两个 Agent 共用一个输出模型"的模糊情形（DD-12.1 的
#: 单一职责在输出侧也成立）。
#:
#: ``renderer`` 的值是 :class:`RenderOutcome`（DD-12.1 记作 `RenderResult`，见其文档）。
AGENT_OUTPUT_TYPES: dict[AgentName, type[FormatModel]] = {
    "curator": TemplateConfirmResult,
    "architect": PlacementPlan,
    "composer": ContentIROutput,
    "renderer": RenderOutcome,
    "inspector": QaVerdict,
}


# ------------------------------------------------------- 决策点级：取"选了什么"
#: 自由文本字段折成短标签时的截断长度。``Decision.chosen`` 是给人看的**标识**
#: （与 ``Decision.options[].value`` 可比），不是结论正文——把整段改写文案塞进去，
#: 裁决队列的列表列会被一段话撑开，而人真正需要看正文的地方是 ``Decision.reason``。
_CHOSEN_LABEL_LIMIT: Final = 40


def _clip(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    return text if len(text) <= _CHOSEN_LABEL_LIMIT else text[: _CHOSEN_LABEL_LIMIT - 1] + "…"


def chosen_label(
    output_type: type[FormatModel] | None, payload: Mapping[str, Any] | None
) -> str | None:
    """从决策点输出载荷里取出"这次选了什么"（写进 ``Decision.chosen`` 的那个短标识）。

    为什么需要它
    ------------
    ``sdk_adapter._chosen_of`` 只认 SDK 约定的字段名（``choice`` / ``chosen`` / ``value``），
    而本项目的五个决策点级输出**没有一个是那个形状**（见上方的决策点级输出段）。
    后果是 ``Decision.chosen`` 在真实决策里**恒为空**——"模型选了什么"这件事在裁决队列
    与报告里都看不到，而列表上少一个取值与"模型没给结论"长得一模一样。

    为什么逐点写死而不泛化
    ----------------------
    "取第一个字符串字段"会在多字段输出上取错（``PlacementChoice`` 的 ``reason``、
    ``ConventionAdvice`` 的 ``finding_ref`` 都是字符串），而**错的取值会一路写进
    报告与追溯链**。故取值口径按决策点逐条写在唯一一处；新增决策点必须在这里补一行
    （与 ``catalog.DECISION_POINTS`` 的"增删一个点必须同时改表"是同一条纪律）。

    ``None`` 是合法返回：该点没有"选择"这个概念（自由生成）或载荷里缺该字段时，
    留痕上的 ``chosen`` 就该是空的——不编一个占位值。
    """
    if payload is None:
        return None
    if output_type is DuplicateJudgement:
        # 布尔取值转成人读的标签：``Decision.chosen`` 是与人对话的字段（裁决队列列头）。
        value = payload.get("is_duplicate")
        if not isinstance(value, bool):
            return None
        return "重复" if value else "不重复"
    if output_type is PlacementChoice:
        # 章节路径是**列表**，故按目录分隔符折平——留痕要能与"候选清单里的哪一项"对上。
        path = payload.get("chapter_path")
        if not isinstance(path, list) or not path:
            return None
        return " / ".join(str(part) for part in path)
    if output_type is ConventionAdvice:
        # 建议内容本身即结论；候选集给的是**建议取值**（调用方注入），故直接取它。
        return _clip(payload.get("suggestion"))
    if output_type is CopyRewrite:
        # 改写点没有"选择"，只有一个作用对象；取值即"改的是哪一段"。
        return _clip(payload.get("unit_id"))
    if output_type is ReviewNote:
        return _clip(payload.get("kind"))
    return None
