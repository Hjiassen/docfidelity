"""章节装配（DD-8.1）。

``compose`` 是"事实 → 文档内容"的最后一步：吃一份合并好的事实快照与一份文档类型
档案，产出**内容 IR**。它是 ``compose`` 层唯一的总装入口，其余模块各管一件事：

    facts ──normalize──▶ DomainObject ──evaluate──▶ ContentUnit
                                                       │
     档案骨架 + 槽位对齐 ──▶ 章节树 ──glossary──▶ captions ──▶ 纯净自检 ──▶ ContentIR
                                                       │
                                                  build_traces ──▶ TraceMatrix

对 DD-8.1 伪代码的六处偏离（逐条都有理由，不偏离就会产出错东西）
----------------------------------------------------------------
1. **吃 ``FactSnapshot`` 而不是 ``FactBundle``**。DD-8.1 写 ``compose(facts: FactBundle)``
   并读 ``facts.objects``；但 P0-07 已把"单一来源的产出"定为 ``FactBundle``、
   把"多源合并后的稳定输入"定为 ``FactSnapshot``（决策二 + ``contracts/fact.py``）。
   装配吃的是合并结果，故取 ``FactSnapshot``，并经 ``ingest.normalize`` 摊成领域对象。
   传 ``FactBundle`` 会让"只装了其中一个来源"这件事在类型上就不可见。
2. **不接收 ``contract``**。DD-8.1 的签名里有 ``contract: ContractQuery``，但它的
   函数体从头到尾**没有用到**。这不奇怪：IR 不含任何样式信息（DD-3.2（2）），
   装配期确实不需要模板知识——那属于渲染层与预检。保留一个用不到的参数只会让
   调用方以为"装配依赖模板"，进而把模板耦合带进来。
3. **章节内容挂为该章节标题的 ``children``**，而不是平铺进 ``ir.units``。契约的
   ``ContentUnit.children`` 就是为章节树准备的；平铺之后"哪几个单元属于哪一章"
   只能靠层级回溯去猜，而 ``Location.chapter_path`` 与渲染期的章节注入都需要这个
   事实。``ContentIR.walk()`` 的深度优先序两种写法一致，故不损失任何顺序信息。
4. **每个投影带一个 ``p<i>`` 路径前缀**。顶层投影求值时 ``ctx.index`` 恒为 0
   （``evaluate`` 只对子投影递增），于是两个同 ``for_each``、同 ``layout`` 的顶层
   投影会派生出**完全相同**的 ``unit_id``——而 ``unit_id`` 是对外引用内容单元的唯一
   句柄。前缀取自投影在档案中的声明序号，确定性可复现。
5. **不调用 :func:`~docfidelity.compose.precheck.satisfiable`**。DD-7.5 把"档案 ×
   模板包可满足性"定在**构建前**（服务端对应 ``E_PRECHECK_FAILED``），DD-8.1 的
   函数体里也确实没有它。预检需要模板包，而装配不需要——把它塞进装配会让每次
   装配都依赖模板库。
6. **``AssertIRClean`` 只查引擎不变量，外部词表的命中记为 Finding**。样式痕迹
   （``FORBIDDEN_IR_TOKENS``）一旦出现即抛 ``E_INTERNAL``：那是引擎缺陷。而
   文档类型名 / 领域概念名要靠外部词表判（``qa.type_agnostic``，P0-12），
   在装配期硬抛会把"事实数据里出现了一个类型名"升级成构建失败。

未声明 ``slot`` 的投影会落入保留槽位 ``"default"``，而骨架通常不声明它，于是这些
内容单元不会被任何章节接收。这不是踩坑而是 DD-8.1 的原样行为，但本项目对"静默丢
内容"零容忍（1.2 各条），故为这类落位槽记一条 major 并写进 ``unresolved``。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Final

from docfidelity.contracts.config import Terminology
from docfidelity.contracts.domain import DomainModel, DomainObject
from docfidelity.contracts.errors import EngineError, ErrorCode
from docfidelity.contracts.fact import FactSnapshot
from docfidelity.contracts.ir import ContentIR, ContentUnit
from docfidelity.contracts.profile import ChapterSkeletonItem, DocTypeProfile
from docfidelity.contracts.qa import Finding, Location, TraceMatrix
from docfidelity.ingest import normalize
from docfidelity.model import DomainModelRegistry, EvalContext, evaluate

from .captions import assign as assign_captions
from .glossary import apply_glossary_to_ir, glossary_apply, iter_texts, unresolved_variables
from .placement import PlacementResolver, place_unbound
from .purity import PURITY_RULE_ID, assert_ir_clean, check_ir_purity
from .slots import DEFAULT_SLOT, SLOT_RULE_ID, SlotPolicy, check_slots, orphan_slots
from .traces import build_traces, used_object_refs

__all__ = ["ComposeResult", "build_content_ir", "compose"]

#: 章节标题单元的 ``unit_id`` 前缀。与投影派生的 ``p<i>/…`` 天然不冲突。
_CHAPTER_PREFIX: Final = "chapter"

#: ``ContentIR.unresolved`` 里的类别前缀，让"未解析的东西"一眼可分类。
_UNRESOLVED_SLOT: Final = "slot"
_UNRESOLVED_TRACE: Final = "trace"
_UNRESOLVED_VARIABLE: Final = "var"

#: 章节内容相对其章节标题的层级偏移（章节是 1 级，其内容就是 2 级）。
_CONTENT_LEVEL_OFFSET: Final = 1


@dataclass(frozen=True)
class ComposeResult:
    """装配结果（DD-8.1 的 ``ComposeResult``）。

    ``findings`` 里既有投影求值期的告警，也有槽位、纯净、术语的告警——调用方读这
    一处即可，不必去翻各个模块。
    """

    ir: ContentIR
    findings: list[Finding] = field(default_factory=list)
    traces: TraceMatrix = field(default_factory=TraceMatrix)

    def counts_by_severity(self) -> dict[str, int]:
        counts = {"blocker": 0, "major": 0, "minor": 0}
        for item in self.findings:
            counts[item.severity] = counts.get(item.severity, 0) + 1
        return counts

    def blockers(self) -> list[Finding]:
        return [item for item in self.findings if item.severity == "blocker"]


def compose(
    snapshot: FactSnapshot,
    profile: DocTypeProfile,
    *,
    domain: DomainModelRegistry,
    doc_key: str,
    template_ref: str,
    terminology: Terminology | None = None,
    slot_policy: SlotPolicy | None = None,
    purity_words: Iterable[str] = (),
    placement_resolver: PlacementResolver | None = None,
) -> ComposeResult:
    """把事实快照装配为内容 IR。

    Parameters
    ----------
    snapshot:
        多源合并后的事实快照（``ingest.merge`` 的出口）。
    profile:
        文档类型档案；章节骨架、投影规则、术语作用域都由它声明。
    domain:
        领域模型注册表——``for_each`` 指向的 ``kind`` 必须已登记（FR-M2-23）。
    doc_key / template_ref:
        写入 ``ContentIR`` 的文档条目键与模板包引用（来自 ``DocumentEntry``）。
    terminology:
        术语表；``profile.bindings.terminology_scope == "none"`` 时一律不套用。
    slot_policy:
        槽位严重度策略（DD-8.3 的"可配置升级"）。
    purity_words:
        外部词表（文档类型名 / 领域概念名），命中引擎表面即记 blocker。
        装配期不硬抛——见模块文档第 6 条。

    Raises
    ------
    EngineError
        ``E_DOMAIN_*``（事实不合法）、``E_PROFILE_INVALID``（投影声明非法）、
        ``E_INTERNAL``（IR 带样式痕迹、或 ``unit_id`` 撞车——都是引擎缺陷）。
    """
    objects = normalize(snapshot.merged, domain)
    terms = _effective_terminology(profile, terminology)
    findings: list[Finding] = []
    materialized: list[DomainObject] = []

    chapters = list(profile.genre.chapter_skeleton)
    slot_level = _resolve_slot_levels(chapters, findings)
    units_by_slot = _evaluate(
        profile, objects, domain, terms, slot_level, findings, materialized, placement_resolver
    )

    ir = _assemble(
        profile=profile,
        snapshot=snapshot,
        doc_key=doc_key,
        template_ref=template_ref,
        chapters=chapters,
        units_by_slot=units_by_slot,
        terms=terms,
    )

    findings.extend(_orphan_findings(units_by_slot, chapters))
    findings.extend(check_slots(chapters, units_by_slot, policy=slot_policy))

    apply_glossary_to_ir(ir, terms)
    assign_captions(ir)

    findings.extend(_purity_findings(ir, purity_words))
    assert_ir_clean(ir)
    _require_unique_unit_ids(ir)

    known = _known_objects(objects, materialized)
    traces = build_traces(ir, known, untraceable=_chapter_unit_ids(chapters))
    ir.unresolved = _unresolved(ir, known, units_by_slot, chapters, terms)
    return ComposeResult(ir=ir, findings=findings, traces=traces)


def build_content_ir(
    snapshot: FactSnapshot,
    profile: DocTypeProfile,
    *,
    domain: Sequence[DomainModel],
    doc_key: str,
    template_ref: str,
    terminology: Terminology | None = None,
    purity_words: Iterable[str] | None = None,
) -> ContentIR:
    """装配内容 IR 的**工具门面**（需求 5.5 的 ``content_ir_build``）。

    与 :func:`compose` 的唯一区别是领域模型的**入参形态**：``compose`` 要一个
    :class:`~docfidelity.model.DomainModelRegistry`（运行时对象），而工具面只能传
    可序列化的声明清单。这里把清单折成注册表再原样转发——装配规则一字不改
    （决策一：编排逻辑只有一份，门面不复制它）。

    ``terminology`` / ``purity_words`` 显式接受 ``None``：工具面的"未传可选参数"
    就是 ``None``，门面负责把它归一成实现层的默认值，而不是把 ``None`` 转发下去
    （``compose`` 的 ``purity_words`` 是可迭代对象，``None`` 会让它就地炸在装配中段）。

    为什么门面可以直接住在本层：``compose`` 层零 OOXML 依赖（``.importlinter`` 的
    ``compose-has-no-ooxml``），所以 ``agents`` 用惰性点路径指向它不产生静态
    import 边，``agents-has-no-ooxml`` 契约不破——这也是本工具与 render / qa 门面
    的关键区别（后两者的门面只能住在各自层内）。
    """
    return compose(
        snapshot,
        profile,
        domain=DomainModelRegistry(domain),
        doc_key=doc_key,
        template_ref=template_ref,
        terminology=terminology,
        purity_words=purity_words or (),
    ).ir


# -- 术语 -----------------------------------------------------------------


def _effective_terminology(profile: DocTypeProfile, terminology: Terminology | None) -> Terminology:
    """档案的 ``terminology_scope == "none"`` 一律压成"不套用术语表"。"""
    terms = terminology if terminology is not None else Terminology()
    if profile.bindings.terminology_scope == "none":
        return terms.model_copy(update={"scope": "none"})
    return terms


# -- 槽位层级 -------------------------------------------------------------


def _resolve_slot_levels(
    chapters: Sequence[ChapterSkeletonItem], findings: list[Finding]
) -> dict[str, int]:
    """``slot_id -> 章节层级``（先声明者优先，冲突记 Finding）。

    内容单元的标题层级取"所在章节层级 + 1"：一级章节里的"每接口一节"应当是二级标题。
    DD-8.1 平铺追加、不设层级，会让所有投影产出的标题与章节标题同级——渲染期按
    ``level`` 选样式，于是整份文档的层次全部压平。
    """
    levels: dict[str, int] = {}
    for chapter in chapters:
        for slot in chapter.slots:
            existing = levels.get(slot.slot_id)
            if existing is None:
                levels[slot.slot_id] = chapter.level
            elif existing != chapter.level:
                findings.append(
                    Finding(
                        rule_id=SLOT_RULE_ID,
                        severity="major",
                        message=(
                            f"槽位 {slot.slot_id!r} 被不同层级的章节声明"
                            f"（{existing} 与 {chapter.level}），内容层级取先声明者"
                        ),
                        location=Location(chapter_path=[chapter.title]),
                        evidence=f"slot_id={slot.slot_id}",
                        suggestion="让同一槽位只在一处声明，或给两处各取一个 slot_id",
                    )
                )
    return levels


# -- 投影求值 -------------------------------------------------------------


def _evaluate(
    profile: DocTypeProfile,
    objects: Sequence[DomainObject],
    domain: DomainModelRegistry,
    terms: Terminology,
    slot_level: dict[str, int],
    findings: list[Finding],
    materialized: list[DomainObject],
    placement_resolver: PlacementResolver | None,
) -> dict[str, list[ContentUnit]]:
    """逐条投影求值，按槽位归集。

    ``findings`` 与 ``materialized`` 被所有 ``EvalContext`` **共享**（同一个 list
    对象）：前者让嵌套子投影的告警自动汇聚到调用方手里，后者收下求值期即时物化的
    嵌套成员——它们不在事实快照里，却是 ``trace`` 引用得着的对象（详见 ``EvalContext``）。
    """
    by_slot: dict[str, list[ContentUnit]] = {}
    for index, spec in enumerate(profile.projections):
        key = spec.slot or DEFAULT_SLOT
        ctx = EvalContext(
            domain=domain,
            profile=profile,
            glossary=profile.bindings.terminology_scope,
            path=(f"p{index}",),
            level=slot_level.get(key, 1) + _CONTENT_LEVEL_OFFSET,
            findings=findings,
            materialized=materialized,
        )
        units = evaluate(objects, spec, ctx)
        if spec.slot is None and placement_resolver is not None:
            place_unbound(
                units,
                profile.genre.chapter_skeleton,
                placement_resolver,
                by_slot,
                initial_level=ctx.level,
            )
        else:
            by_slot.setdefault(key, []).extend(units)
    return by_slot


def _known_objects(
    objects: Sequence[DomainObject], materialized: Sequence[DomainObject]
) -> list[DomainObject]:
    """追溯对账用的对象全集：顶层对象在前，求值期物化的嵌套成员在后，按引用去重。

    顶层优先的原因：同一个引用若两边都有（嵌套成员与其顶层同名对象），带 ``origin``
    的顶层对象信息更全，边上的 ``source`` / ``locator`` 才有值。
    """
    seen: dict[str, DomainObject] = {}
    for obj in (*objects, *materialized):
        seen.setdefault(obj.ref, obj)
    return list(seen.values())


# -- 章节装配 -------------------------------------------------------------


def _assemble(
    *,
    profile: DocTypeProfile,
    snapshot: FactSnapshot,
    doc_key: str,
    template_ref: str,
    chapters: Sequence[ChapterSkeletonItem],
    units_by_slot: dict[str, list[ContentUnit]],
    terms: Terminology,
) -> ContentIR:
    ir = ContentIR(
        project_id=snapshot.project_id,
        doc_key=doc_key,
        profile_id=profile.profile_id,
        template_ref=template_ref,
        source_digests=dict(snapshot.source_digests),
    )
    for index, chapter in enumerate(chapters):
        heading = ContentUnit(
            unit_id=f"{_CHAPTER_PREFIX}#{index}",
            type="heading",
            level=chapter.level,
            text=glossary_apply(chapter.title, terms),
        )
        for slot in chapter.slots:
            heading.children.extend(units_by_slot.get(slot.slot_id, ()))
        ir.units.append(heading)
    return ir


def _chapter_unit_ids(chapters: Sequence[ChapterSkeletonItem]) -> list[str]:
    """骨架章节标题的 ``unit_id``。

    这些单元的内容来自**档案**（``chapter.title``），不是事实，故不参与追溯对账——
    否则每份产物都会带上固定的 orphan 噪声（详见 ``traces.build_traces``）。
    """
    return [f"{_CHAPTER_PREFIX}#{index}" for index in range(len(chapters))]


def _orphan_findings(
    units_by_slot: dict[str, list[ContentUnit]], chapters: Sequence[ChapterSkeletonItem]
) -> list[Finding]:
    """产出了内容却无人接收的落位槽——静默丢内容的唯一防线。"""
    findings: list[Finding] = []
    for key in orphan_slots(units_by_slot, chapters):
        units = units_by_slot[key]
        findings.append(
            Finding(
                rule_id=SLOT_RULE_ID,
                severity="major",
                message=(
                    f"投影向槽位 {key!r} 产出了 {len(units)} 个内容单元，但章节骨架中"
                    "没有任何槽位接收它们，这些内容不会进入文档"
                ),
                evidence="units=" + ", ".join(unit.unit_id for unit in units[:5]),
                suggestion=(
                    "在档案的章节骨架里声明该槽位，或修正投影的 slot"
                    "（未声明 slot 的投影一律落入保留槽位 'default'）"
                ),
            )
        )
    return findings


# -- IR 硬约束 ------------------------------------------------------------


def _purity_findings(ir: ContentIR, purity_words: Iterable[str]) -> list[Finding]:
    return [
        Finding(
            rule_id=PURITY_RULE_ID,
            severity="blocker",
            message=f"内容 IR 出现禁用 token：{token!r}",
            evidence="IR 只表达'这是什么内容'；样式与类型名都不得进入 IR",
            suggestion="定位产出该 token 的装配代码或事实源取值并清理",
        )
        for token in check_ir_purity(ir, extra_words=purity_words)
    ]


def _require_unique_unit_ids(ir: ContentIR) -> None:
    """``unit_id`` 撞车即抛。

    产物完全正常、每条内容都在，只有"对外引用指向了错的那个单元"——追溯链随之断掉，
    而没有任何一层的门禁会喊（``ContentIR.duplicate_unit_ids()`` 是唯一会喊的地方）。
    """
    duplicates = ir.duplicate_unit_ids()
    if not duplicates:
        return
    raise EngineError(
        ErrorCode.INTERNAL,
        f"内容 IR 出现重复 unit_id：{sorted(set(duplicates))}",
        where={
            "duplicates": sorted(set(duplicates)),
            "hint": "unit_id 由投影路径派生，重复说明派生用的转义不是单射（多对一）",
        },
        remediation=["检查 model.projection 的 segment 转义与 compose 的路径前缀"],
    )


# -- 未解析项 -------------------------------------------------------------


def _unresolved(
    ir: ContentIR,
    objects: Sequence[DomainObject],
    units_by_slot: dict[str, list[ContentUnit]],
    chapters: Sequence[ChapterSkeletonItem],
    terms: Terminology,
) -> list[str]:
    """未能解析的引用（DD-3.2（2）的 ``ContentIR.unresolved``）。

    三类，各带类别前缀：``slot:`` 无人接收的落位槽、``trace:`` 指向不存在对象的
    追溯引用、``var:`` 术语表里查不到的变量名。
    """
    items: dict[str, None] = {}
    for key in orphan_slots(units_by_slot, chapters):
        items.setdefault(f"{_UNRESOLVED_SLOT}:{key}", None)
    known = {obj.ref for obj in objects}
    for ref in used_object_refs(ir):
        if ref not in known:
            items.setdefault(f"{_UNRESOLVED_TRACE}:{ref}", None)
    for text in iter_texts(ir):
        for name in unresolved_variables(text, terms):
            items.setdefault(f"{_UNRESOLVED_VARIABLE}:{name}", None)
    return sorted(items)
