"""多源合并与冲突裁决（DD-5.3 / FR-M1-11）。

产物是 :class:`~docfidelity.contracts.fact.FactSnapshot`——契约层已经把这件事定死：
``FactSnapshot.merged`` 的类型是 ``list[Fact]``，说明"去重与裁决之后"的对象**仍是事实**
（保留 ``source`` / ``locator`` / ``verified`` / ``priority``），而不是领域对象。
DD-5.3 把返回写成 ``list[DomainObject]``；本实现跟契约层，理由与 ``ingest.protocol``
里那段相同：契约层是唯一对外承诺，接入侧的中间形态是实现细节。

三条纪律
--------
1. **无法裁决的冲突不得静默采用**（验收 39）。并列最高优先级就是"无法裁决"：
   记 ``ConflictRecord(resolution="unresolved")`` 与 ``Finding(major)``，并把该对象的
   ``verified`` 置 ``False``，供编排期标注「目标设计 / 待确认」（FR-M3-11）。
   "静默取第一个"的后果是产物看着完整，而其中某个值来自哪一方无人知晓。
2. **值比较只有一处口径**：复用 ``platform.hashing.digest_payload``。自写一套 `==`
   比较会与指纹 / 版本 diff 的判断分叉——同一份数据在两处得出两种结论。
3. **顺序由内容决定**：分组按首次出现顺序（不用 `set` 迭代，它每次都可能不同），
   最终快照与冲突清单各自显式排序（NFR-02）。

已知边界（有意为之）
--------------------
列表型字段不做**元素级**合并。元素级合并需要成员的标识键与逐元素的冲突规则，那是
编排期的语义；接入期遇到列表取值不同即记一条冲突，交人工判断。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from docfidelity.contracts.fact import (
    ConflictCandidate,
    ConflictRecord,
    Fact,
    FactBundle,
    FactSnapshot,
)
from docfidelity.contracts.qa import Finding
from docfidelity.platform.hashing import digest_payload

#: 冲突裁决的规则编号（FR-M1-11）。
CONFLICT_RULE_ID: str = "FR-M1-11"

#: 裁决方式：``priority`` 按优先级取最高；``unresolved`` 直接交人工。
Resolution = Literal["priority", "unresolved"]

_GroupKey = tuple[str, tuple[str, ...]]


@dataclass(frozen=True)
class PriorityTable:
    """事实源优先级表（外部数据）。只做查表，不做业务判断。"""

    by_source: Mapping[str, int] = field(default_factory=dict)
    default: int = 0

    def of(self, source: str) -> int:
        return self.by_source.get(source, self.default)


@dataclass(frozen=True)
class ConflictRules:
    """字段级裁决规则；未列出的字段走 ``default_resolution``。"""

    default_resolution: Resolution = "priority"
    by_field: Mapping[str, Resolution] = field(default_factory=dict)

    def resolution_for(self, field_name: str) -> Resolution:
        return self.by_field.get(field_name, self.default_resolution)


def _effective_priority(fact: Fact, priorities: PriorityTable | None) -> int:
    """生效优先级。

    给出 ``PriorityTable`` 时以它为准（它才是"事实源注册中心"声明的权威表，
    FR-M1-11）；否则退回事实自带的值（由 ``SourceConfig.priority`` 在接入时算好）。
    只有这一处解释优先级，避免两条口径各自演进。
    """
    return priorities.of(fact.source) if priorities is not None else fact.priority


def _pair_key(fact: Fact, priorities: PriorityTable | None) -> tuple[int, str, str]:
    """排序键：(-优先级, 来源, 定位)。顺序只由内容决定，与入参顺序无关。"""
    return (-_effective_priority(fact, priorities), fact.source, fact.locator)


def _order(members: Sequence[Fact], priorities: PriorityTable | None) -> list[Fact]:
    return sorted(members, key=lambda fact: _pair_key(fact, priorities))


def _merge_field(
    name: str,
    members: Sequence[Fact],
    priorities: PriorityTable | None,
    rules: ConflictRules,
    conflicts: list[ConflictRecord],
    findings: list[Finding],
    *,
    kind: str,
    key: Sequence[str],
) -> tuple[Any, bool]:
    """裁决单个字段，返回（取值，是否未决）。"""
    entries: list[tuple[Any, Fact]] = [
        (fact.payload[name], fact) for fact in members if name in fact.payload
    ]
    if len({digest_payload(value) for value, _ in entries}) == 1:
        return entries[0][0], False

    ordered = sorted(entries, key=lambda item: _pair_key(item[1], priorities))
    candidates = [
        ConflictCandidate(
            value=value,
            source=fact.source,
            locator=fact.locator,
            priority=_effective_priority(fact, priorities),
            verified=fact.verified,
        )
        for value, fact in ordered
    ]

    resolution = rules.resolution_for(name)
    chosen: Any = None
    if resolution == "priority":
        top = _effective_priority(ordered[0][1], priorities)
        tied = [value for value, fact in ordered if _effective_priority(fact, priorities) == top]
        if len({digest_payload(value) for value in tied}) == 1:
            chosen = ordered[0][0]
        else:
            resolution = "unresolved"

    if chosen is None:
        findings.append(
            Finding(
                rule_id=CONFLICT_RULE_ID,
                severity="major",
                message=(
                    f"{kind}:{'/'.join(key)} 的字段 {name} 存在无法裁决的冲突"
                    f"（{len(candidates)} 个来源取值不一致，最高优先级并列）"
                ),
                evidence="；".join(f"{item.source}={item.value!r}" for item in candidates),
                suggestion="在事实源优先级表中区分这两个来源的优先级，或人工指定采用值",
            )
        )
    conflicts.append(
        ConflictRecord(
            kind=kind,
            key=list(key),
            field=name,
            candidates=candidates,
            resolution=resolution,
            chosen=chosen,
            reason=(
                f"按优先级取最高（{candidates[0].source}）"
                if chosen is not None
                else "最高优先级并列，无法裁决"
            ),
        )
    )
    return chosen, chosen is None


def _merge_group(
    group_key: _GroupKey,
    members: Sequence[Fact],
    priorities: PriorityTable | None,
    rules: ConflictRules,
    conflicts: list[ConflictRecord],
    findings: list[Finding],
) -> Fact:
    kind, key = group_key
    field_names: dict[str, None] = {}
    for fact in members:
        for name in fact.payload:
            field_names.setdefault(name, None)

    payload: dict[str, Any] = {}
    unresolved = False
    for name in sorted(field_names):
        value, pending = _merge_field(
            name, members, priorities, rules, conflicts, findings, kind=kind, key=key
        )
        unresolved = unresolved or pending
        if not pending:
            payload[name] = value

    dominant = _order(members, priorities)[0]
    return Fact(
        source=dominant.source,
        kind=kind,
        key=list(key),
        payload=payload,
        locator=dominant.locator,
        verified=all(fact.verified for fact in members) and not unresolved,
        priority=_effective_priority(dominant, priorities),
        asset_names=sorted({name for fact in members for name in fact.asset_names}),
    )


def merge(
    bundles: Sequence[FactBundle],
    *,
    project_id: str,
    priorities: PriorityTable | None = None,
    rules: ConflictRules | None = None,
) -> tuple[FactSnapshot, list[Finding]]:
    """把多个事实包合并为一个稳定快照。

    Returns
    ------
    tuple[FactSnapshot, list[Finding]]
        快照（``merged`` 去重裁决后的对象、``conflicts`` 冲突记录、``source_digests``
        各源摘要）与需要人工复核的冲突告警（``unresolved`` 各一条）。
    """
    active_rules = rules or ConflictRules()
    facts = [fact for bundle in bundles for fact in bundle.facts]

    groups: dict[_GroupKey, list[Fact]] = {}
    for fact in facts:
        groups.setdefault((fact.kind, tuple(fact.key)), []).append(fact)

    merged: list[Fact] = []
    conflicts: list[ConflictRecord] = []
    findings: list[Finding] = []
    for group_key, members in groups.items():
        merged.append(
            _merge_group(group_key, members, priorities, active_rules, conflicts, findings)
        )

    merged.sort(key=lambda fact: (fact.kind, tuple(fact.key)))
    conflicts.sort(key=lambda record: (record.kind, tuple(record.key), record.field))
    source_digests: dict[str, str] = {}
    for bundle in bundles:
        source_digests[bundle.source_id] = bundle.digest

    snapshot = FactSnapshot(
        project_id=project_id,
        merged=merged,
        conflicts=conflicts,
        source_digests=source_digests,
        digest=digest_payload(
            {
                "merged": [fact.model_dump(mode="json") for fact in merged],
                "conflicts": [record.model_dump(mode="json") for record in conflicts],
            }
        ),
    )
    return snapshot, findings


__all__ = [
    "CONFLICT_RULE_ID",
    "ConflictRules",
    "PriorityTable",
    "Resolution",
    "merge",
]
