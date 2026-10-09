"""追溯矩阵（FR-M6-07 / FR-M5-10）。

方向是双向的，两侧都要能问出话来：

* **正向**（文档 → 事实）：某个内容单元来自哪些领域对象 —— 用于"这句话是编的吗"；
* **反向**（事实 → 文档）：某个领域对象落在哪些内容单元里 —— 用于"这条事实漏了吗"。

``TraceMatrix`` 用两个列表承载"没对上"的两半，它们都是**信息而非错误**（契约文档
原文），但必须呈现出来让人判断：``orphans``（在文档里、查不到来源的单元）与
``unused``（在事实源里、未进入文档的对象）。

``orphans`` 里为什么要塞两种东西
--------------------------------
真正的孤儿有两种，且处置方式不同，故用写法区分而不是混成一串（两者**互斥**：
``trace`` 为空才算前者，非空但解析不到才算后者）：

=========================  =================================================================
``<unit_id>``              该单元的 ``trace`` 为空——没有任何追溯边
``<unit_id>-><ref>``       该单元声明了 ``trace``，但引用的对象**不在输入里**（悬挂引用）
=========================  =================================================================

悬挂引用不可能是"数据本来就没有"——``ContentUnit.trace`` 由 ``model.evaluate`` 从
实际的 ``DomainObject`` 派生，出现悬挂说明装配链路里对象集合被换过（典型是
``compose`` 用了快照而追溯表用了另一批对象）。不区分写法的后果是：这类缺陷长得和
"这一章本来就没来源"一模一样。

重复边会被 ``TraceMatrix`` 的校验器直接拒收，故这里按 ``(unit_id, object_ref)``
去重（保序）。同一单元的 ``trace`` 列表里出现重复引用是可能的：嵌套集合里同一个
实体被列了两次，``model`` 的即时校验只查标识键、不去重。
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Final

from docfidelity.contracts.domain import DomainObject
from docfidelity.contracts.ir import ContentIR
from docfidelity.contracts.qa import TraceEdge, TraceMatrix

__all__ = ["TRACE_RULE_ID", "build_traces", "used_object_refs"]

#: 追溯完整门禁的规则编号（FR-M5-10 追溯完整 | trace | major）。
TRACE_RULE_ID: Final = "FR-M5-10"

#: 悬挂引用的写法分隔符（``<unit_id>-><ref>``）。
DANGLING_MARKER: Final = "->"


def build_traces(
    ir: ContentIR,
    objects: Iterable[DomainObject],
    *,
    untraceable: Iterable[str] = (),
) -> TraceMatrix:
    """构造追溯矩阵。``objects`` 是本次装配吃进去的**全部**领域对象。

    ``untraceable`` 是**结构单元**的 ``unit_id`` 集合：档案骨架里的章节标题（DD-8.1
    的 ``heading(chapter.title)``）。它们的内容来自**档案**而不是事实，若一并计入
    ``orphans``，每一份产物都会带上与内容无关的固定噪声——而"每次都出现、每次都
    被忽略"正是 FR-M5-10 失效的方式。章节与事实的对应关系并不因此丢失：它由该章节
    的子单元边表达（FR-M6-07 要求的"章节 ↔ 需求"方向可由父子关系推出）。
    """
    skip = set(untraceable)
    origin_by_ref = {obj.ref: obj for obj in objects}
    edges: list[TraceEdge] = []
    seen_pairs: set[tuple[str, str]] = set()
    orphans: list[str] = []

    for unit in ir.walk():
        if unit.unit_id in skip:
            continue
        refs = list(dict.fromkeys(unit.trace))
        if not refs:
            orphans.append(unit.unit_id)
            continue
        for ref in refs:
            origin = origin_by_ref.get(ref)
            if origin is None:
                orphans.append(f"{unit.unit_id}{DANGLING_MARKER}{ref}")
                continue
            pair = (unit.unit_id, ref)
            if pair in seen_pairs:
                continue
            seen_pairs.add(pair)
            edges.append(_edge(unit.unit_id, ref, origin))

    used = {edge.object_ref for edge in edges}
    unused = sorted(ref for ref in origin_by_ref if ref not in used)
    return TraceMatrix(edges=edges, orphans=orphans, unused=unused)


def used_object_refs(ir: ContentIR) -> list[str]:
    """IR 声明的全部对象引用（去重、保遍历顺序）。"""
    seen: dict[str, None] = {}
    for unit in ir.walk():
        for ref in unit.trace:
            seen.setdefault(ref, None)
    return list(seen)


def _edge(unit_id: str, ref: str, origin: DomainObject) -> TraceEdge:
    location = origin.origin
    return TraceEdge(
        unit_id=unit_id,
        object_ref=ref,
        source=location.source if location is not None else "",
        locator=location.locator if location is not None else "",
    )
