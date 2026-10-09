"""章节注入（DD-9.2 / FR-M2-13 / FR-M6-05）—— 裁掉模板示例区、按 IR 重建内容。

这一步是渲染层的**主戏**：把内容 IR 的章节树，按模板自己的原型段落与表格，
重新填回正文的内容区。渲染层的两条纪律在这里同时生效：

* **复用优先**（1.4 原则）：段落、表格一律**深拷贝模板原型**，绝不自行拼装
  ``w:pPr``。模板里没有对应原型时跳过该单元并记 Finding——"造一个看起来能用
  的段落"会让样式保真当场失效，而且产物表面上完全正常。
* **IR 无样式痕迹**：``unit.type`` 只说明"这是什么内容"，样式一律由原型的
  ``w:pStyle`` 决定。故本模块内**没有任何**"给段落挑样式"的逻辑；唯一的例外是
  题注（表注 / 图注），因为题注样式在 IR 里被显式声明为 ``caption_style_id``。

分派用封闭枚举字典，不用 if/elif 链
----------------------------------
十个 ``UnitType`` 取值对应十个处理函数。写成 ``if unit.type == "table"`` 的链子，
既会被类型无关检索器盯着（``branch_literal`` 一栏），也会在新加一种单元类型时
**静默漏掉**——漏掉的表现是内容凭空少一段，而不是报错。故分派表是模块级的
``_HANDLERS`` 字典，缺项走显式兜底并记 Finding。

裁剪与注入共用同一个区间
------------------------
模板的示例内容区（第一个骨架标题 → 尾节属性之前）先整片删除，再把注入内容插在
**原来的起点位置**。区间由 :func:`context.content_region` 算一次、两处共用——
各算一遍必然漂移一格，结果就是"少一章"或"多一段模板残留"。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Final

from docfidelity.contracts.ir import ContentUnit
from docfidelity.ooxml import (
    Element,
    ensure_child_in_order,
    ensure_paragraph_properties,
    set_attr,
    set_on_off,
    set_paragraph_text,
)

from .blocks import block_reference_of, copy_block
from .context import (
    STYLE_FALLBACK_RULE_ID,
    TEMPLATE_FIDELITY_RULE_ID,
    RenderContext,
)
from .order import P_PR_ORDER
from .tables import build_table, table_data_of

__all__ = [
    "UNKNOWN_UNIT_RULE_ID",
    "InjectionPolicy",
    "InjectionReport",
    "inject_chapters",
    "inject_unit",
    "list_items_of",
]

#: 出现契约外的单元类型（IR 与渲染层不同步）时使用的规则编号。
#: 复用 FR-M2-13（样式 / 类型回退）：它与"契约里没有对应物"同源。
UNKNOWN_UNIT_RULE_ID: Final = STYLE_FALLBACK_RULE_ID

#: 可判定"批注位置"的取值（``TableData.caption_position`` 之外，也用于列表）。
CAPTION_ABOVE: Final = "above"
CAPTION_BELOW: Final = "below"

#: ``fields`` 里装列表条目的键名。与 ``compose.glossary._TEXT_LIST_FIELDS``、
#: ``model.projection`` 的 ``{"items": ...}`` 是同一个键——三处写法一旦分叉，
#: 表现是"术语替换漏掉列表"或"列表整段消失"，两者都不会报错。
LIST_FIELD: Final = "items"


@dataclass(frozen=True)
class InjectionPolicy:
    """注入策略。

    ``list_split`` 决定"一个列表单元 = 一条还是多条"：IR 既允许"一个单元装整张
    列表"（``text`` 多行），也允许"每行一个单元"。两者都能处理才是安全的——
    按单条处理会让多行文本挤成一段（行内的 ``w:br`` 不是列表项），按拆分处理
    对单条单元无副作用（拆完还是一条）。
    """

    caption_style_id: str | None = None
    table_caption_position: str = CAPTION_ABOVE
    list_split: bool = True


@dataclass
class InjectionReport:
    """注入结果摘要（进 ``StepResult.checkpoint_outputs``）。"""

    region_cleared: int = 0
    injected_elements: int = 0
    headings: int = 0
    paragraphs: int = 0
    list_items: int = 0
    tables: int = 0
    figures: int = 0
    block_refs: int = 0
    breaks: int = 0
    trace_paragraphs: int = 0
    skipped: int = 0
    by_type: dict[str, int] = field(default_factory=dict)

    def bump(self, unit_type: str) -> None:
        self.by_type[unit_type] = self.by_type.get(unit_type, 0) + 1

    def as_payload(self) -> dict[str, Any]:
        return {
            "region_cleared": self.region_cleared,
            "injected_elements": self.injected_elements,
            "headings": self.headings,
            "paragraphs": self.paragraphs,
            "list_items": self.list_items,
            "tables": self.tables,
            "figures": self.figures,
            "block_refs": self.block_refs,
            "breaks": self.breaks,
            "trace_paragraphs": self.trace_paragraphs,
            "skipped": self.skipped,
            "by_type": dict(sorted(self.by_type.items())),
        }


# ------------------------------------------------------------------- 入口
def inject_chapters(
    ctx: RenderContext, *, policy: InjectionPolicy | None = None
) -> InjectionReport:
    """DD-9.1 第 6 步：裁掉示例内容区并按 IR 注入章节。

    只做**结构**：表格只建骨架（规范化属第 7 步），图片只占位（字节与缩放属第 8 步），
    分页只登记（分页策略属第 9 步）。这样每一步都有自己的产物，"第 7 步没跑"与
    "第 6 步没做"才能被区分开。
    """
    settings = policy or InjectionPolicy()
    report = InjectionReport()
    body = ctx.body
    # 用 ``content_bounds()`` 而**不是** ``ctx.region``：冗余裁剪（第 3 步）可能在
    # 示例区内删掉若干块，区间的右端已经挪位；``content_bounds`` 现算尾节属性的位置，
    # 用构造时那个快照会切到区间之外。
    start, end = ctx.content_bounds()

    for element in list(body)[start:end]:
        body.remove(element)
        report.region_cleared += 1

    cursor = start
    for unit in ctx.ir.walk():
        cursor += inject_unit(ctx, unit, at=cursor, policy=settings, report=report)

    ctx.part.mark_dirty()
    report.injected_elements = len(ctx.injected)
    return report


def inject_unit(
    ctx: RenderContext,
    unit: ContentUnit,
    *,
    at: int,
    policy: InjectionPolicy,
    report: InjectionReport | None = None,
) -> int:
    """注入一个单元，返回**插入的元素个数**（调用方据此推进插入游标）。

    对外暴露是为了让"逐单元可调用"这条验收（DD-9.1 步骤函数可单独调用）成立：
    扩展点与测试都不必先裁一遍示例区。
    """
    sink = report if report is not None else InjectionReport()
    handler = _HANDLERS.get(unit.type)
    if handler is None:
        sink.skipped += 1
        engine_gap = ctx.profile_allows(unit.type)
        ctx.note_once(
            UNKNOWN_UNIT_RULE_ID,
            "major" if engine_gap else "minor",
            f"unknown-unit-type:{unit.type}",
            (
                f"体例档案允许单元类型 {unit.type!r}，但渲染层没有对应处理函数，该单元已被跳过"
                if engine_gap
                else f"IR 出现了体例档案未允许的单元类型 {unit.type!r}，该单元已被跳过"
            ),
            unit=unit,
            evidence=f"unit_type={unit.type!r}",
            suggestion=(
                "在渲染层的分派表里补上该单元类型的处理函数"
                if engine_gap
                else "核对档案的 allowed_unit_types 与投影配置：IR 里不该出现该类型"
            ),
        )
        return 0

    before = len(ctx.injected)
    handler(ctx, unit, at, policy, sink)
    inserted = len(ctx.injected) - before
    if inserted:
        sink.bump(unit.type)
    return inserted


# ------------------------------------------------------------------- 内部
def _insert(ctx: RenderContext, element: Element, at: int) -> None:
    """把元素插到 body 的 ``at`` 位，并登记为"我们放进去的"。

    登记是必要的：分页与裁剪都要排除注入内容（"我们自己的块"不是模板冗余）。
    """
    ctx.body.insert(at, element)
    ctx.injected.append(element)


def _apply_style_id(ctx: RenderContext, paragraph: Element, style_id: str | None) -> None:
    """把段落样式设为 ``style_id``；样式不可用时**保持原型自带的样式**并记 Finding。

    保持原型样式而不是"什么都不写"：原型的 ``w:pStyle`` 本来就是模板体例的一部分，
    为了一行题注把整段的样式引用抹掉，会让这一段回落到 ``Normal``——那才是真正
    的样式保真破坏。
    """
    if style_id is None:
        return
    resolved, _created = ctx.ensure_style(style_id)
    if resolved is None:
        ctx.note_once(
            STYLE_FALLBACK_RULE_ID,
            "minor",
            f"style-missing:{style_id}",
            f"样式 {style_id!r} 既不在目标包也不在模板契约里，该段落沿用原型自带样式",
            evidence=f"style_id={style_id!r}",
            suggestion="确认模板包已重新入库，或改正档案中声明的样式 ID",
        )
        return
    properties = ensure_paragraph_properties(paragraph)
    style = ensure_child_in_order(properties, "w:pStyle", P_PR_ORDER)
    set_attr(style, "w:val", resolved)


def _paragraph_from(ctx: RenderContext, unit: ContentUnit, text: str) -> Element | None:
    """造一个正文段落（原型副本 + 文本）；无原型返回 ``None`` 并记 Finding。"""
    prototype = ctx.prototypes.body_paragraph()
    if prototype is None:
        ctx.note_once(
            TEMPLATE_FIDELITY_RULE_ID,
            "major",
            "no-paragraph-prototype",
            "模板的示例内容区里取不到正文段落原型，无法注入该单元",
            unit=unit,
            suggestion="在模板中保留至少一段示例正文，作为正文 / 列表的原型",
        )
        return None
    set_paragraph_text(prototype, text)
    return prototype


def _split_items(text: str) -> list[str]:
    """把列表单元的文本拆成条目（按行，去空白行）。空文本退化为一个空段落。

    这样"一个单元装整张列表"与"每个条目一个单元"两种 IR 形态落到同一段代码上，
    不依赖上游选了哪一种。
    """
    items = [line.strip() for line in text.splitlines()]
    return [item for item in items if item] or [""]


def list_items_of(unit: ContentUnit) -> list[str]:
    """列表单元的条目：**先认 ``fields["items"]``，再认 ``text`` 的行**。

    两条通路必须都认，而且顺序不能反
    --------------------------------
    装配层对 ``unit=bullets`` 把条目写进 ``fields["items"]``
    （``model.projection._bullet_items``），而 ``text`` 是"人写 IR / 夹具手搓"的
    形态（``tests/fixtures/render_fixture`` 就是这种）。只认 ``text`` 时，装配层
    产出的列表会退化成**一个空段落**——单元在、字段有值、判定层面全绿，只有人眼
    能看出列表整段消失。这正是本项目 1.2 反复记的那类"静默丢失"。

    先 ``fields`` 后 ``text``：装配套路产出的 IR 里 ``text`` 恒为 ``None``，两者
    不会同时有值；先后只是为了让"有条目"这一支尽早命中，避免空 ``text`` 把
    有内容的字段盖掉。
    """
    raw = unit.fields.get(LIST_FIELD)
    if isinstance(raw, list):
        items = [str(item).strip() for item in raw if str(item).strip()]
        if items:
            return items
    return _split_items(unit.text or "")


# -- 各类型处理函数：签名统一，便于放进分派表 ------------------------------
def _inject_heading(
    ctx: RenderContext, unit: ContentUnit, at: int, policy: InjectionPolicy, report: InjectionReport
) -> None:
    """标题：按层级取原型副本，设置文本。样式来自原型，**不重写**（见模块文档）。"""
    level = unit.level if unit.level is not None else ctx.chapter_level
    prototype, hit = ctx.prototypes.heading_for(level)
    if prototype is None:
        report.skipped += 1
        ctx.note_once(
            TEMPLATE_FIDELITY_RULE_ID,
            "major",
            "no-heading-prototype",
            "模板里取不到任何标题原型，该标题单元已被跳过（不自行拼装标题段落）",
            unit=unit,
            suggestion="在模板的示例内容区保留各级示例标题，作为标题原型",
        )
        return
    if not hit:
        ctx.note_once(
            STYLE_FALLBACK_RULE_ID,
            "minor",
            f"heading-level-fallback:{level}",
            f"模板骨架没有 {level} 级标题原型，已回落到最接近的层级（回退方向朝浅）",
            unit=unit,
            evidence=f"level={level}, available={ctx.prototypes.heading_levels()}",
            suggestion="在模板中补齐该级标题样式，或调整档案的章节骨架层级",
        )
    set_paragraph_text(prototype, unit.text or "")
    _insert(ctx, prototype, at)
    report.headings += 1
    if level == ctx.chapter_level:
        ctx.chapter_headings.append(prototype)


def _inject_para(
    ctx: RenderContext, unit: ContentUnit, at: int, policy: InjectionPolicy, report: InjectionReport
) -> None:
    paragraph = _paragraph_from(ctx, unit, unit.text or "")
    if paragraph is None:
        report.skipped += 1
        return
    _insert(ctx, paragraph, at)
    report.paragraphs += 1


def _emit_list(
    ctx: RenderContext,
    unit: ContentUnit,
    at: int,
    report: InjectionReport,
    *,
    prototype_of: Callable[[], Element | None],
) -> None:
    """列表（项目符号 / 有序编号）的统一实现：逐条造段落，编号沿用原型的 ``numPr``。"""
    items = list_items_of(unit)
    for offset, item in enumerate(items):
        prototype = prototype_of()
        if prototype is None:
            if offset == 0:
                report.skipped += 1
                ctx.note_once(
                    TEMPLATE_FIDELITY_RULE_ID,
                    "major",
                    "no-list-prototype",
                    "模板里取不到列表段落原型（也没有正文原型可回落），该列表单元已被跳过",
                    unit=unit,
                    suggestion="在模板的示例内容区保留一个带编号的示例列表",
                )
            return
        set_paragraph_text(prototype, item)
        _insert(ctx, prototype, at + offset)
        report.list_items += 1


def _inject_bullets(
    ctx: RenderContext, unit: ContentUnit, at: int, policy: InjectionPolicy, report: InjectionReport
) -> None:
    _emit_list(ctx, unit, at, report, prototype_of=ctx.prototypes.bullet_paragraph)


def _inject_numbered(
    ctx: RenderContext, unit: ContentUnit, at: int, policy: InjectionPolicy, report: InjectionReport
) -> None:
    _emit_list(ctx, unit, at, report, prototype_of=ctx.prototypes.numbered_paragraph)


def _inject_table(
    ctx: RenderContext, unit: ContentUnit, at: int, policy: InjectionPolicy, report: InjectionReport
) -> None:
    """表格：建骨架 + 表注（位置按策略），登记到 ``ctx.injected_tables`` 待第 7 步规范化。"""
    table = build_table(ctx, unit)
    if table is None:
        report.skipped += 1
        return

    caption = _table_caption(ctx, unit, policy)
    cursor = at
    inserted = 0
    above = policy.table_caption_position != CAPTION_BELOW
    if caption is not None and above:
        _insert(ctx, caption, cursor)
        cursor += 1
        inserted += 1
    _insert(ctx, table, cursor)
    cursor += 1
    inserted += 1
    if caption is not None and not above:
        _insert(ctx, caption, cursor)
        inserted += 1

    ctx.injected_tables.append((unit.unit_id, table))
    report.tables += 1


def _table_caption(
    ctx: RenderContext, unit: ContentUnit, policy: InjectionPolicy
) -> Element | None:
    """表注段落（文本来自 IR，编号已由装配层填好）；无文本返回 ``None``。"""
    text = (table_data_of(unit).caption or "").strip()
    if not text:
        return None
    caption = _paragraph_from(ctx, unit, text)
    if caption is None:
        return None
    _apply_style_id(ctx, caption, ctx.resolve_caption_style(policy.caption_style_id))
    return caption


def _inject_figure(
    ctx: RenderContext, unit: ContentUnit, at: int, policy: InjectionPolicy, report: InjectionReport
) -> None:
    """图片 / 截图：只放**承载段落**并登记；字节与缩放属第 8 步（图片自适应）。"""
    paragraph = _paragraph_from(ctx, unit, "")
    if paragraph is None:
        report.skipped += 1
        return
    _insert(ctx, paragraph, at)
    ctx.pending_figures.append((unit, paragraph))
    report.figures += 1


def _inject_trace(
    ctx: RenderContext, unit: ContentUnit, at: int, policy: InjectionPolicy, report: InjectionReport
) -> None:
    """追溯单元：以正文段落逐行呈现追溯键。

    DD-9.2 只示范了 heading / table / figure / screenshot 四种，**没有规定追溯单元的
    版面形态**（矩阵表格？脚注？附录？）。渲染层不猜：先按"一行一个追溯键"的
    最朴素形态落地，具体形态留到引入追溯矩阵样式契约的那一轮。P0 的 ``compose``
    也不产出该类型，故这里不会影响本轮验收。
    """
    text = (unit.text or "").strip() or "\n".join(unit.trace)
    for offset, item in enumerate(_split_items(text)):
        paragraph = _paragraph_from(ctx, unit, item)
        if paragraph is None:
            report.skipped += 1
            return
        _insert(ctx, paragraph, at + offset)
        report.trace_paragraphs += 1


def _inject_pagebreak(
    ctx: RenderContext, unit: ContentUnit, at: int, policy: InjectionPolicy, report: InjectionReport
) -> None:
    """显式分页：造空段落并置 ``pageBreakBefore``，登记为**声明的**分页点。

    必须登记到 ``ctx.declared_breaks``：第 9 步（分页策略）会先清掉全部分页点再按
    白名单重建；不登记就等于作者显式写的分页点被清掉，而且反被判成"未声明"。
    """
    paragraph = _paragraph_from(ctx, unit, "")
    if paragraph is None:
        report.skipped += 1
        return
    set_on_off(ensure_paragraph_properties(paragraph), "w:pageBreakBefore", True, order=P_PR_ORDER)
    _insert(ctx, paragraph, at)
    ctx.declared_breaks.append(paragraph)
    report.breaks += 1


def _inject_block_ref(
    ctx: RenderContext, unit: ContentUnit, at: int, policy: InjectionPolicy, report: InjectionReport
) -> None:
    """块复用：按 ``block_id`` 取模板块原件深拷贝（可跨模板，见 ``blocks``）。"""
    block_id = block_reference_of(unit)
    block = copy_block(ctx, block_id)
    if block is None:
        report.skipped += 1
        return
    _insert(ctx, block, at)
    report.block_refs += 1


#: 单元类型 → 处理函数。缺项走 ``inject_unit`` 的显式兜底。
#: 用 ``ContentUnit.type`` 的封闭枚举做键：这是引擎自有的取值域，与"文档类型 /
#: 领域概念"无关，故不构成类型无关性违规。
_HANDLERS: Final[dict[str, Callable[..., None]]] = {
    "heading": _inject_heading,
    "para": _inject_para,
    "bullets": _inject_bullets,
    "numbered": _inject_numbered,
    "table": _inject_table,
    "figure": _inject_figure,
    "screenshot": _inject_figure,
    "trace": _inject_trace,
    "pagebreak": _inject_pagebreak,
    "block_ref": _inject_block_ref,
}
