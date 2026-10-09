"""投影求值器（DD-6.2）。

把领域对象按档案声明的投影规则映射为内容单元。这是"通用装配"的执行体：
换一类文档只换声明（``for_each`` / ``group_by`` / ``layout`` / ``fields``），
执行它的就是本模块这几十行——**引擎不知道也不需要知道** ``for_each`` 指向什么类型。

对 DD-6.2 字面伪代码的两处有意偏离
--------------------------------------
1. **分组粒度**。伪代码写 ``groups = group(...) if proj.group_by else {None: items}``，
   即不带 ``group_by`` 时全部条目并成一个单元。这条在需求 12 章明确要求的能力上不成立：
   "一接口一节型" 对应的正是 ``layout: one_section_per`` 而不带 ``group_by``，
   按字面实现会产出**一节**而不是每接口一节。故本实现为：

   =========================  ====================================================
   ``group_by`` 有值          按字段取值分组，组序 = 排序后首次出现序
   ``group_by`` 为空 + 聚合型  全部条目一组（表格本就是"一组一张表"）
   ``group_by`` 为空 + 逐条型  一条目一组（每条一节 / 一张图 / 一段）
   =========================  ====================================================

   聚合型 = ``table`` / ``row`` / ``bullets``（其内容就是组的条目集合）；
   逐条型 = ``section`` / ``para`` / ``figure`` / ``screenshot``。
2. **子投影的求值次数**。伪代码为 ``for c in children for gi in gitems``，即每个
   组内条目都求值一次。当子投影引用的是**全局**集合（如 ``for_each: <某全局类型>``）
   时，这会把它重复 N 遍。故本实现只在子投影**依赖所在条目**时逐条求值
   （``for_each`` 含点路径，或恰好等于该条目的类型），否则整组求值一次。

确定性（NFR-02 / FR-M4-12）
------------------------------
- 排序键是 ``(类型序, 数值, 文本, *标识键)``：混合类型不会抛 ``TypeError``，
  同 ``order_by`` 取值的条目按标识键收敛，**结果不依赖入参顺序**。
- 分组用 ``dict``（保序），不用 ``set`` 迭代——``set`` 的迭代序随哈希种子变。
- ``unit_id`` 由投影路径派生：``"<段>#<投影序号>"``，嵌套以 ``/`` 连接。
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from docfidelity.contracts.domain import DomainObject, DomainObjectOrigin
from docfidelity.contracts.errors import EngineError, ErrorCode
from docfidelity.contracts.ir import ContentUnit, FigureData, TableData
from docfidelity.contracts.profile import DocTypeProfile, ProjectionSpec
from docfidelity.contracts.qa import Finding, Location
from docfidelity.platform.hashing import sha256_hex

from .registry import DomainModelRegistry
from .validation import is_blank, require_registered_kind, validate_object
from .values import (
    cell_text,
    first_non_blank,
    is_aggregate,
    optional_int,
    optional_text,
    read_field,
    resolve_expression,
    scalar_text,
    sort_key,
)

__all__ = [
    "CONVENTION_RULE_ID",
    "EvalContext",
    "PROJECTION_RULE_ID",
    "apply_filter",
    "compose_sort",
    "content_type_of",
    "evaluate",
    "is_per_item",
    "resolve_mapping",
]

#: 投影机制类问题的规则编号（FR-M3-14 投影规则驱动装配）。
PROJECTION_RULE_ID: str = "FR-M3-14"

#: 体例声明未满足类问题的规则编号（FR-M3-15）。必填缺值与单元类型越界都归它。
CONVENTION_RULE_ID: str = "FR-M3-15"

#: ``unit`` → ``ContentUnit.type``。``row`` 落到 ``table``：``ContentUnit`` 没有
#: "row" 类型，一行只存在于表格里；渲染层按连续的行单元合并成表（DD-9.4 的表格规范化）。
_CONTENT_TYPE_BY_UNIT: Final[dict[str, str]] = {
    "section": "heading",
    "table": "table",
    "row": "table",
    "bullets": "bullets",
    "para": "para",
    "figure": "figure",
    "screenshot": "screenshot",
}

#: 内容即"组的条目集合"的单元：不带 ``group_by`` 时整批一组。
_AGGREGATING_UNITS: Final[frozenset[str]] = frozenset({"table", "row", "bullets"})

#: 内容即"单条"的单元：不带 ``group_by`` 时一条一组。
_PER_ITEM_UNITS: Final[frozenset[str]] = frozenset({"section", "para", "figure", "screenshot"})

#: ``unit_id`` 段落里不允许多字节字符与分隔符。``#`` 是段内分隔、``/`` 是段间分隔，
#: 放进段里会让 ``unit_id`` 无法被拆回路径。
_SEGMENT_UNSAFE_RE: Final = re.compile(r"[^0-9A-Za-z\u4e00-\u9fff.]")

#: 单段长度上限。超出时用内容摘要补尾，保证截断后仍然互不相同——
#: 两段被截成同一串会让两个不同的内容单元共用一个 ``unit_id``。
_MAX_SEGMENT: Final = 160


# -- 求值上下文 -----------------------------------------------------------


@dataclass
class EvalContext:
    """求值上下文：注册表 + 档案 + 当前位置 + Finding 收集器。

    ``findings`` 与 ``materialized`` 都被所有派生上下文**按引用共享**，因此嵌套求值
    产生的待确认项与即时物化的对象会自动汇聚到最外层那个 ctx 上——调用方只需要读
    ``ctx.findings`` / ``ctx.materialized``。

    ``materialized`` 存在的理由：嵌套集合的成员（``for_each: "<kind>.<字段>"``）在
    求值期才被物化成 :class:`DomainObject`，它们**不在**任何事实快照里。而 ``trace``
    记的是对象引用，追溯矩阵要解析这些引用就必须拿到这批对象——否则每一个嵌套投影
    都会产出一串"引用了不存在的对象"的假告警（P0-08 落地时由测试逼出）。

    ``glossary`` 由 ``compose`` 填充（术语作用域），``model`` 只透传不使用：
    DD-8.1 的 ``EvalContext(glossary=..., domain=REGISTRY)`` 就是这么构造的。
    """

    domain: DomainModelRegistry
    profile: DocTypeProfile | None = None
    glossary: str = "project"
    path: tuple[str, ...] = ()
    index: int = 0
    level: int = 1
    scope: DomainObject | None = None
    findings: list[Finding] = field(default_factory=list)
    materialized: list[DomainObject] = field(default_factory=list)

    def under(
        self,
        *,
        path: tuple[str, ...],
        index: int,
        level: int,
        scope: DomainObject | None,
    ) -> EvalContext:
        """派生一个子上下文，共享同一个 Finding 与物化对象收集器。"""
        return EvalContext(
            domain=self.domain,
            profile=self.profile,
            glossary=self.glossary,
            path=path,
            index=index,
            level=level,
            scope=scope,
            findings=self.findings,
            materialized=self.materialized,
        )


# -- 主入口 ---------------------------------------------------------------


def evaluate(
    objects: Sequence[DomainObject],
    proj: ProjectionSpec,
    ctx: EvalContext,
) -> list[ContentUnit]:
    """按 ``proj`` 把 ``objects`` 求值为内容单元。

    ``objects`` 假定已经过 :func:`docfidelity.model.validation.validate_objects`
    （接入阶段的职责，FR-M1-12）；嵌套集合的成员无法经过接入阶段，故在此即时校验。
    """
    require_registered_kind(proj.for_each, ctx.domain, trail="投影")
    items = _resolve_collection(proj.for_each, objects, ctx)
    kept, absent_fields = apply_filter(items, proj.filter)
    if absent_fields:
        ctx.findings.append(
            Finding(
                rule_id=PROJECTION_RULE_ID,
                severity="minor",
                message=(
                    f"投影 {proj.for_each!r} 的过滤条件引用了部分对象上不存在的字段："
                    f"{'、'.join(absent_fields)}"
                ),
                evidence=f"filter={proj.filter}",
                suggestion=(
                    "缺值不参与该子句的判定（这些对象不会被排除）——"
                    "静默排除等于静默丢内容；若确实要排除，请先把字段补全"
                ),
            )
        )
    ordered = sorted(kept, key=compose_sort(proj.order_by))

    units: list[ContentUnit] = []
    for group_key, group_items in _group(ordered, proj):
        segment = _segment(group_key, group_items, proj, ctx)
        unit_id = "/".join((*ctx.path, f"{segment}#{ctx.index}"))
        child_path = (*ctx.path, f"{segment}#{ctx.index}")
        mapping = resolve_mapping(proj.fields, group_items)
        children: list[ContentUnit] = []
        for child_index, child in enumerate(proj.children):
            children.extend(
                _evaluate_child(objects, child, child_index, group_items, child_path, ctx)
            )
        _check_group_fields(proj, group_items, unit_id, ctx)
        units.append(_build_unit(proj, mapping, group_key, group_items, children, unit_id, ctx))
    return units


def content_type_of(unit: str) -> str:
    """``ProjectionSpec.unit`` → ``ContentUnit.type``。

    未知取值抛错而不是回落默认值：回落会让"档案写了一个不存在的 unit"表现为
    "生成了一份类型不对的文档"，而不是一条错误。
    """
    content_type = _CONTENT_TYPE_BY_UNIT.get(unit)
    if content_type is None:
        raise EngineError(
            ErrorCode.PROFILE_INVALID,
            f"未知的投影单元类型：{unit!r}",
            where={"unit": unit, "known": sorted(_CONTENT_TYPE_BY_UNIT)},
            remediation=["把 unit 改为 contracts.profile.ProjectionUnit 中的取值"],
        )
    return content_type


# -- 集合解析 -------------------------------------------------------------


def _resolve_collection(
    expression: str, pool: Sequence[DomainObject], ctx: EvalContext
) -> list[DomainObject]:
    """解析 ``for_each``：``"<kind>"`` 或 ``"<kind>.<字段>[.<字段>...]"``。

    带点路径时以**所在条目**（``ctx.scope``）为容器下钻；不带点时，若类型与所在条目
    相同则取该条目本身（子投影写 ``for_each: <自身类型>`` 是常见的"就这一条"写法），
    否则取全局集合中该类型的全部对象。
    """
    base, dot, rest = expression.partition(".")
    containers = _containers(base, pool, ctx)
    if not dot:
        return containers

    member = ctx.domain.member_kind(base, rest)
    if member is None:
        raise EngineError(
            ErrorCode.PROFILE_INVALID,
            f"for_each={expression!r} 的字段 {rest!r} 未声明成员类型",
            where={"for_each": expression, "container_kind": base, "field": rest},
            remediation=[
                f"在 {base!r} 的领域模型里把 {rest!r} 声明为 type: list 并给出 of（成员类型）",
            ],
        )
    collected: list[DomainObject] = []
    for container in containers:
        raw = read_field(container, rest)
        if raw is None:
            continue
        if isinstance(raw, str | bytes) or not isinstance(raw, Sequence):
            raise EngineError(
                ErrorCode.DOMAIN_TYPE_MISMATCH,
                f"{container.ref} 的字段 {rest!r} 不是集合（for_each 需要可遍历的集合）",
                where={
                    "for_each": expression,
                    "object": container.ref,
                    "actual": type(raw).__name__,
                },
                remediation=["把该字段改为列表，或修正 for_each 的字段名"],
            )
        collected.extend(
            _materialize(entry, member, ctx, parent=container, field=rest, index=index)
            for index, entry in enumerate(raw)
        )
    return collected


def _containers(base: str, pool: Sequence[DomainObject], ctx: EvalContext) -> list[DomainObject]:
    if ctx.scope is not None and ctx.scope.kind == base:
        return [ctx.scope]
    return [item for item in pool if item.kind == base]


def _materialize(
    entry: Any,
    member_kind: str,
    ctx: EvalContext,
    *,
    parent: DomainObject | None = None,
    field: str = "",
    index: int = 0,
) -> DomainObject:
    """把嵌套集合里的一个成员实体化为领域对象并即时校验。

    标识键按成员模型声明的 ``key`` 抽取；抽不出键就是 ``E_DOMAIN_KEY_MISSING``——
    一个没有键的嵌套成员无法进入追溯矩阵，也就无法在报告里被指认。

    ``parent`` / ``field`` / ``index`` 用来给成员补一个**派生来源**：嵌套成员在事实源
    里没有独立的落点，但它确实来自父对象的某个集合字段的某一项。不补的话追溯边上的
    ``source`` 会是空串——"这句话是编的吗"就只能回答"不知道来源"，而来源其实明明白白
    就是父对象那条事实（P0-08 落地时由 e2e 测试逼出）。
    """
    if isinstance(entry, DomainObject):
        validate_object(entry, ctx.domain)
        if entry.origin is None:
            return entry.model_copy(update={"origin": _nested_origin(parent, field, index)})
        return entry
    if not isinstance(entry, Mapping):
        raise EngineError(
            ErrorCode.DOMAIN_TYPE_MISMATCH,
            f"嵌套集合的成员应为对象，实际为 {type(entry).__name__}",
            where={"member_kind": member_kind, "actual": type(entry).__name__},
            remediation=["把成员改为对象（键值对）形式"],
        )
    model = ctx.domain.get(member_kind)
    fields = {str(name): value for name, value in entry.items()}
    materialized = DomainObject(
        kind=member_kind,
        key=model.key_of(fields),
        fields=fields,
        origin=_nested_origin(parent, field, index),
    )
    validate_object(materialized, ctx.domain)
    ctx.materialized.append(materialized)
    return materialized


def _nested_origin(
    parent: DomainObject | None, field: str, index: int
) -> DomainObjectOrigin | None:
    """嵌套成员的派生来源：沿用父事实的 ``source``，``locator`` 指向集合字段的第 ``index`` 项。

    父对象没有 ``origin`` 时返回 ``None``：宁可让追溯边留空，也不编一个假落点——
    报告里一个看似可信、实则杜撰的 ``locator`` 比空串更有害。
    """
    if parent is None or parent.origin is None:
        return None
    locator = f"{parent.origin.locator}.{field}[{index}]" if field else parent.origin.locator
    return DomainObjectOrigin(
        source=parent.origin.source,
        locator=locator,
        verified=parent.origin.verified,
        priority=parent.origin.priority,
    )


# -- 过滤 / 排序 / 分组 ---------------------------------------------------


def apply_filter(
    items: Sequence[DomainObject], spec: Mapping[str, Sequence[str]]
) -> tuple[list[DomainObject], list[str]]:
    """白名单式过滤：字段取值必须落在允许集合内。

    Returns
    ------
    tuple[list[DomainObject], list[str]]
        ``(保留的条目, 在其上找不到过滤字段的字段名列表)``。

    第二项存在的理由：过滤字段整体缺失时，"排除它们"会让整批内容凭空消失
    （``filter: {scope: [inner]}`` 遇上没有 ``scope`` 字段的事实源 → 章节全空），
    而报告毫无提示。本实现的取舍是**缺值不参与判定**（该子句不排除它），
    并把这件事回报给调用方去记一条待确认——**绝不静默丢内容**。
    """
    if not spec:
        return list(items), []

    kept: list[DomainObject] = []
    absent: dict[str, int] = {}
    for item in items:
        excluded = False
        for name, allowed in spec.items():
            if name not in item.fields:
                absent[name] = absent.get(name, 0) + 1
                continue
            if scalar_text(item.get(name)) not in {str(token) for token in allowed}:
                excluded = True
                break
        if not excluded:
            kept.append(item)
    return kept, sorted(absent)


def compose_sort(order_by: str | None) -> Callable[[DomainObject], tuple[Any, ...]]:
    """构造稳定排序键。

    ``order_by`` 为空时按标识键排序——不保留入参顺序。入参顺序来自事实源的合并结果，
    把它当作排序依据会让"同一份事实、不同的合并轮次"产出不同的 ``unit_id`` 次序，
    进而让构建失去幂等性（NFR-02）。
    """

    def _key(item: DomainObject) -> tuple[Any, ...]:
        value = read_field(item, order_by) if order_by else None
        rank, number, text = sort_key(value)
        return (rank, number, text, *item.key)

    return _key


def is_per_item(unit: str) -> bool:
    """单元的分组粒度：逐条型（每条一个单元）还是聚合型（整组一个单元）。

    未分类的取值在此**显式失败**。静默按聚合型处理的后果是：``ProjectionUnit``
    新增一个逐条型取值后，"每条一节"悄悄退化成"只有一节"，而所有既有测试仍然全绿。
    """
    if unit in _PER_ITEM_UNITS:
        return True
    if unit in _AGGREGATING_UNITS:
        return False
    raise EngineError(
        ErrorCode.PROFILE_INVALID,
        f"投影单元类型 {unit!r} 未声明分组粒度（逐条 / 聚合）",
        where={
            "unit": unit,
            "per_item": sorted(_PER_ITEM_UNITS),
            "aggregating": sorted(_AGGREGATING_UNITS),
        },
        remediation=["在 model.projection 的粒度集合中登记该单元类型"],
    )


def _group(
    items: Sequence[DomainObject], proj: ProjectionSpec
) -> list[tuple[object, list[DomainObject]]]:
    """分组，组序 = 排序后**首次出现序**。

    用 ``dict`` 累积（Python 3.7+ 保证插入序）而不是 ``set``——``set`` 的迭代序
    依赖哈希种子，会让同一次构建在不同进程里产出不同的章节次序。
    """
    if proj.group_by is None:
        if is_per_item(proj.unit):
            return [(item.ref, [item]) for item in items]
        return [(None, list(items))]

    buckets: dict[str, list[DomainObject]] = {}
    for item in items:
        buckets.setdefault(scalar_text(read_field(item, proj.group_by)), []).append(item)
    return list(buckets.items())


def _segment(
    group_key: object,
    group_items: Sequence[DomainObject],
    proj: ProjectionSpec,
    ctx: EvalContext,
) -> str:
    """本单元的 ``unit_id`` 段。

    带 ``ctx.scope`` 时前缀所在条目的引用。这不是为了好看：子投影的段常常只由
    ``for_each`` 派生（如 ``endpoint.parameters``），一个组里 N 个条目各求值一次就会
    得到 N 个**完全相同**的 ``unit_id``——对外引用会指向错的那个单元，追溯链随之断掉。
    """
    own = _own_segment(group_key, group_items, proj)
    if ctx.scope is None:
        return own
    scope_segment = _sanitize_segment(ctx.scope.ref)
    if own == scope_segment:
        return own
    return f"{scope_segment}.{own}"


def _own_segment(
    group_key: object, group_items: Sequence[DomainObject], proj: ProjectionSpec
) -> str:
    if proj.group_by is not None:
        return _sanitize_segment(f"g={scalar_text(group_key)}")
    if is_per_item(proj.unit) and group_items:
        return _sanitize_segment(group_items[0].ref)
    return _sanitize_segment(proj.for_each)


# -- 字段映射 -------------------------------------------------------------


def resolve_mapping(expressions: Sequence[str], items: Sequence[DomainObject]) -> dict[str, Any]:
    """按声明顺序求值字段映射，**键就是声明里的原字符串**。

    键保持原样的原因：``ProjectionSpec.required`` 列的是字段名，要去 ``mapping`` 里
    查缺失；如果这里把 ``"sum(score)"`` 归一化成 ``"score"``，``required`` 就再也
    查不到聚合项了。DD-6.2 的 ``mapping.get(f)`` 依赖的正是"键 = 声明原文"。
    """
    representative = items[0] if items else None
    return {
        expression: resolve_expression(expression, items, representative)
        for expression in expressions
    }


# -- 单元构造 -------------------------------------------------------------


def _check_group_fields(
    proj: ProjectionSpec, group_items: Sequence[DomainObject], unit_id: str, ctx: EvalContext
) -> None:
    """分组后仍声明逐条字段时告警。

    ``fields`` 里的普通字段取自排序后的**首条**（DD-6.2 的字段映射语义）。分组之后
    组里有 N 条时，另外 N-1 条的取值就没有出口了——内容不是"没渲染"，而是
    "以第一条约值代表整组"，从产物上看不出区别，只有在这里提示才能发现。
    """
    if proj.group_by is None or len(group_items) <= 1:
        return
    plain = [expression for expression in proj.fields if not is_aggregate(expression)]
    if not plain:
        return
    ctx.findings.append(
        Finding(
            rule_id=PROJECTION_RULE_ID,
            severity="minor",
            message=(
                f"投影 {proj.for_each!r} 按 {proj.group_by!r} 分组后仍声明了逐条字段 {plain}，"
                f"本单元取的是排序后首条的取值（组内共 {len(group_items)} 条）"
            ),
            location=Location(unit_id=unit_id),
            evidence=f"trace={[item.ref for item in group_items]}",
            suggestion="把逐条字段下移到 children 子投影，或改用聚合（count / sum / min / max / join）",
        )
    )


def _trace_of(group_items: Sequence[DomainObject], ctx: EvalContext) -> list[str]:
    """本单元的追溯引用：组内成员的对象引用；空组回落所在条目。

    空组（``group_items == []``）不是"没有来源"——它恰恰是因为所在条目存在、只是那个
    子集合为空才被求值出来的。若记成空 ``trace``，追溯矩阵会把这张空表判成"凭空
    产生的内容"（孤儿），而它其实完整归属于外层条目。回落让引用非空，孤儿判定只
    留给真正无来源的单元（P0-08 落地时由 e2e 测试逼出）。
    """
    if group_items:
        return [item.ref for item in group_items]
    return [ctx.scope.ref] if ctx.scope is not None else []


def _build_unit(
    proj: ProjectionSpec,
    mapping: dict[str, Any],
    group_key: object,
    group_items: Sequence[DomainObject],
    children: list[ContentUnit],
    unit_id: str,
    ctx: EvalContext,
) -> ContentUnit:
    content_type = content_type_of(proj.unit)
    _check_allowed(content_type, ctx, unit_id)
    _check_required(proj, mapping, unit_id, ctx)
    trace = _trace_of(group_items, ctx)

    if proj.unit == "section":
        return ContentUnit(
            unit_id=unit_id,
            type="heading",
            level=ctx.level,
            text=_heading_text(mapping, group_key, proj),
            fields=dict(mapping),
            children=children,
            trace=trace,
        )
    if proj.unit == "table":
        return ContentUnit(
            unit_id=unit_id,
            type="table",
            fields=_table_fields(proj.columns, group_items, mapping),
            children=children,
            trace=trace,
        )
    if proj.unit == "row":
        return ContentUnit(
            unit_id=unit_id,
            type="table",
            fields=_table_fields(list(proj.fields), group_items, {}),
            children=children,
            trace=trace,
        )
    if proj.unit == "bullets":
        return ContentUnit(
            unit_id=unit_id,
            type="bullets",
            fields={"items": _bullet_items(proj, group_items)},
            children=children,
            trace=trace,
        )
    if proj.unit == "para":
        text = first_non_blank(mapping.get(expression) for expression in proj.fields)
        return ContentUnit(
            unit_id=unit_id,
            type="para",
            text=None if text is None else scalar_text(text),
            fields=dict(mapping),
            children=children,
            trace=trace,
        )
    return ContentUnit(
        unit_id=unit_id,
        type="figure" if proj.unit == "figure" else "screenshot",
        fields=_figure_fields(proj, mapping, unit_id, ctx),
        children=children,
        trace=trace,
    )


def _check_allowed(content_type: str, ctx: EvalContext, unit_id: str) -> None:
    allowed = list(ctx.profile.genre.allowed_unit_types) if ctx.profile is not None else []
    if allowed and content_type not in allowed:
        ctx.findings.append(
            Finding(
                rule_id=CONVENTION_RULE_ID,
                severity="major",
                message=f"投影产出的单元类型 {content_type!r} 不在体例允许的单元类型内",
                location=Location(unit_id=unit_id),
                evidence=f"allowed_unit_types={allowed}",
                suggestion="调整投影的 unit，或把该单元类型加入档案的 allowed_unit_types",
            )
        )


def _check_required(
    proj: ProjectionSpec, mapping: dict[str, Any], unit_id: str, ctx: EvalContext
) -> None:
    missing = [name for name in proj.required if is_blank(mapping.get(name))]
    for name in missing:
        ctx.findings.append(
            Finding(
                rule_id=CONVENTION_RULE_ID,
                severity="major",
                message=f"投影 {proj.for_each!r} 的必填字段缺值：{name}",
                location=Location(unit_id=unit_id),
                suggestion="补齐事实源中的该字段，或把它从投影的 required 中移除",
            )
        )


def _heading_text(mapping: dict[str, Any], group_key: object, proj: ProjectionSpec) -> str | None:
    """标题文本：显式 ``title`` 字段 > 分组键 > 首个非空映射值。

    优先级这样排的原因：分组时标题就是"这一组是什么"（分组键），逐条时标题是
    条目自身的第一个字段（如 ``path``）。显式 ``title`` 永远优先，因为档案作者
    写下来的意图比引擎的推断可靠。
    """
    explicit = first_non_blank(mapping.get(name) for name in ("title", "heading"))
    if explicit is not None:
        return scalar_text(explicit)
    group_text = scalar_text(group_key) if proj.group_by is not None else ""
    if group_text:
        return group_text
    fallback = first_non_blank(mapping.get(expression) for expression in proj.fields)
    if fallback is not None:
        return scalar_text(fallback)
    return group_text or None


def _table_fields(
    columns: Sequence[str], items: Sequence[DomainObject], mapping: Mapping[str, Any]
) -> dict[str, Any]:
    rows = [[cell_text(read_field(item, column)) for column in columns] for item in items]
    caption = mapping.get("caption")
    caption_slot = mapping.get("caption_slot")
    return TableData(
        columns=list(columns),
        rows=rows,
        caption=None if caption is None else scalar_text(caption),
        caption_slot=caption_slot
        if isinstance(caption_slot, int) and not isinstance(caption_slot, bool)
        else None,
    ).model_dump(mode="json")


def _bullet_items(proj: ProjectionSpec, items: Sequence[DomainObject]) -> list[str]:
    """要点文本：逐条取该条目在 ``fields`` 上第一个非空的取值。

    聚合表达式（``count`` / ``sum`` …）在这里被跳过——它们在组内是常量，
    展开成 N 条一模一样的要点只制造噪音。
    """
    plain = [expression for expression in proj.fields if not is_aggregate(expression)]
    bullets: list[str] = []
    for item in items:
        text = first_non_blank(read_field(item, expression) for expression in plain)
        bullets.append(item.ref if text is None else scalar_text(text))
    return bullets


def _figure_fields(
    proj: ProjectionSpec, mapping: Mapping[str, Any], unit_id: str, ctx: EvalContext
) -> dict[str, Any]:
    image_ref = first_non_blank(mapping.get(name) for name in ("image_ref", "image", *proj.fields))
    if image_ref is None:
        ctx.findings.append(
            Finding(
                rule_id=PROJECTION_RULE_ID,
                severity="major",
                message=f"图片单元 {unit_id} 没有取到图片逻辑名",
                location=Location(unit_id=unit_id),
                suggestion="在投影的 fields 中声明图片字段，或核对规范中的资产逻辑名",
            )
        )
    dpi = mapping.get("dpi")
    return FigureData(
        image_ref="" if image_ref is None else scalar_text(image_ref),
        caption=optional_text(mapping.get("caption")),
        caption_slot=optional_int(mapping.get("caption_slot")),
        width_px=optional_int(mapping.get("width_px")),
        height_px=optional_int(mapping.get("height_px")),
        dpi=dpi if isinstance(dpi, int) and not isinstance(dpi, bool) and dpi > 0 else 96,
    ).model_dump(mode="json")


def _evaluate_child(
    objects: Sequence[DomainObject],
    child: ProjectionSpec,
    child_index: int,
    group_items: Sequence[DomainObject],
    child_path: tuple[str, ...],
    ctx: EvalContext,
) -> list[ContentUnit]:
    depends_on_scope = "." in child.for_each or any(
        item.kind == child.for_each for item in group_items
    )
    if not depends_on_scope:
        child_ctx = ctx.under(path=child_path, index=child_index, level=ctx.level + 1, scope=None)
        return evaluate(objects, child, child_ctx)
    units: list[ContentUnit] = []
    for item in group_items:
        child_ctx = ctx.under(path=child_path, index=child_index, level=ctx.level + 1, scope=item)
        units.extend(evaluate(objects, child, child_ctx))
    return units


def _sanitize_segment(raw: str) -> str:
    """把任意文本收成可安全嵌进 ``unit_id`` 的一段。

    转义而不是简单地替换成 ``_``：替换是**多对一**的，``"a/b"`` 与 ``"a_b"`` 会撞成
    同一段，于是两个不同的内容单元共用 ``unit_id``——对外引用错单元、追溯链断掉，
    而 ``ContentIR.duplicate_unit_ids()`` 是唯一会喊出来的地方（前提是有人调它）。
    """
    cleaned = _SEGMENT_UNSAFE_RE.sub(lambda match: f"_{ord(match.group(0)):x}", raw)
    if len(cleaned) > _MAX_SEGMENT:
        tail = sha256_hex(raw.encode("utf-8"))[:8]
        cleaned = f"{cleaned[: _MAX_SEGMENT - 9]}-{tail}"
    return cleaned or "u"
