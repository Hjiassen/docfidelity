"""未绑定投影的候选落位；模型调用由上层注入。"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Mapping, Sequence

from docfidelity.contracts.ir import ContentUnit
from docfidelity.contracts.profile import ChapterSkeletonItem

PlacementResolver = Callable[[ContentUnit, Mapping[str, list[str]]], str | None]


def place_unbound(
    units: Sequence[ContentUnit],
    chapters: Sequence[ChapterSkeletonItem],
    resolver: PlacementResolver,
    by_slot: dict[str, list[ContentUnit]],
    *,
    initial_level: int,
) -> None:
    """仅在可唯一寻址且类型兼容的槽位中落位，缺结论取骨架中的首个候选。"""
    titles = Counter(chapter.title for chapter in chapters)
    slots = Counter(slot.slot_id for chapter in chapters for slot in chapter.slots)
    for unit in units:
        destinations = {}
        for chapter in chapters:
            compatible = [
                slot
                for slot in chapter.slots
                if (not slot.unit_types or unit.type in slot.unit_types)
                and slots[slot.slot_id] == 1
            ]
            if titles[chapter.title] == 1 and len(compatible) == 1:
                destinations[chapter.title] = (chapter, compatible[0])
        # 候选上下文有界；顺序来自档案，规则兜底可复现。
        destinations = dict(list(destinations.items())[:32])
        candidates = {title: [title] for title in destinations}
        selected = resolver(unit, candidates)
        if not destinations:
            by_slot.setdefault("default", []).append(unit)
            continue
        if selected not in destinations:
            selected = next(iter(destinations))
        chapter, slot = destinations[selected]
        offset = chapter.level + 1 - initial_level
        for item in unit.walk():
            if item.level is not None:
                item.level += offset
        by_slot.setdefault(slot.slot_id, []).append(unit)
