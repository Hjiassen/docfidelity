"""事实 → 领域对象（FR-M1-14 / DD-5.3）与接入期硬校验（FR-M1-12）。

为什么归一化集中在**一处**而不是每个接入器各写一份
-----------------------------------------------------
DD-5.3 把 ``normalize`` 定为独立一步：接入器只负责"把外部东西读成事实"，归一化与
冲突裁决集中处理。若让每个接入器自己把裸字段拼成 ``DomainObject``，同一份领域模型
会在 N 处被解释，字段命名与语义立刻开始漂移——而"不同来源的同名对象可合并"正是
FR-M1-14 的全部价值。

因此本模块只做三件事，且每件都只有一处实现：

1. ``kind`` 必须已登记（未登记即抛，文案由注册表给出，不在这里复述）。
2. 键与字段类型交给 ``model`` 的校验器（``E_DOMAIN_KEY_MISSING`` /
   ``E_DOMAIN_TYPE_MISMATCH``）——**接入阶段即失败**（FR-M1-12）。
3. 给失败补上 ``source`` 与 ``locator``。没有定位的接入报错是无法处置的：用户知道
   "少了一个键"，但不知道去哪一行补。定位信息只存在于事实里，所以补位只能在这里做。
"""

from __future__ import annotations

from collections.abc import Iterable

from docfidelity.contracts.domain import DomainObject, DomainObjectOrigin
from docfidelity.contracts.errors import EngineError
from docfidelity.contracts.fact import Fact
from docfidelity.model import DomainModelRegistry, validate_objects


def _to_object(fact: Fact) -> DomainObject:
    """把一条事实摊成领域对象；来源信息进 ``origin`` 而不是丢掉。"""
    return DomainObject(
        kind=fact.kind,
        key=list(fact.key),
        fields=dict(fact.payload),
        origin=DomainObjectOrigin(
            source=fact.source,
            locator=fact.locator,
            verified=fact.verified,
            priority=fact.priority,
        ),
    )


def normalize(facts: Iterable[Fact], registry: DomainModelRegistry) -> list[DomainObject]:
    """把事实序列归一为领域对象序列（顺序与入参一致）。

    去重与排序**不在这一步**——那是 :func:`docfidelity.ingest.merge.merge` 的职责。
    未登记的 ``kind`` 直接抛 ``E_DOMAIN_KIND_UNKNOWN``（不返回空集合）。

    Raises
    ------
    EngineError
        ``E_DOMAIN_KIND_UNKNOWN`` / ``E_DOMAIN_KEY_MISSING`` / ``E_DOMAIN_TYPE_MISMATCH``，
        且错误里带 ``source`` 与 ``locator``。
    """
    objects: list[DomainObject] = []
    locations: dict[str, tuple[str, str]] = {}

    for fact in facts:
        try:
            registry.get(fact.kind)  # 未登记即抛，错误码与文案由注册表给出
            obj = _to_object(fact)
        except EngineError as error:
            error.where.setdefault("source", fact.source)
            error.where.setdefault("locator", fact.locator)
            raise
        objects.append(obj)
        locations.setdefault(obj.ref, (fact.source, fact.locator))

    try:
        validate_objects(objects, registry)
    except EngineError as error:
        location = locations.get(str(error.where.get("object", "")))
        if location is not None:
            error.where.setdefault("source", location[0])
            error.where.setdefault("locator", location[1])
        raise

    return objects


__all__ = ["normalize"]
