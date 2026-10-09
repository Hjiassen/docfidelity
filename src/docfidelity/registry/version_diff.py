"""模板版本 diff 与影响面推导（DD-7.4，验收 14）。

验收 14 要的是"能输出样式层 / 结构 / 表原型 / 页面设置的差异清单与影响面"，
所以输出分两层：

* **分区增量** :class:`SectionDelta` —— 哪些分区变了、影响面是什么、是否阻断。
  这是给人看的"该去检查什么"。
* **字段级变更** :class:`FieldChange` —— 具体哪一条字段从什么变成了什么。
  这是给人看的"到底改了什么"。

三层口径的分工
--------------
1. :func:`docfidelity.registry.fingerprint.compute_fingerprint` 给出摘要，
   回答"变没变"；
2. :func:`diff_fingerprints` 只比摘要，回答"哪个分区变了"——P04 页面列版本时
   用它，不必把两个版本的契约全量加载进来；
3. :func:`diff_packages` 做字段级比较，回答"改了什么、影响什么"。

三层的**分区集合与分区内容**都取自 :func:`fingerprint.section_payloads`，
不另起一套。另起一套的后果是"指纹说 styles 变了、diff 说 styles 没事"，
而使用者只会相信其中一个。

带 id 的列表按 id 匹配，不按索引
--------------------------------
按索引比会把"在样式列表中间插入一条"报成"后面全部样式都变了"；更要命的是
``block_id`` / ``slot_id`` 消失——档案与生成期引用的是 id，不是位置。因此凡是
条目里带稳定 id 的列表（styles / slots / blocks / num / abstractNum / heading
绑定）一律按 id 建路径，于是"某个 block_id 没了"表现为一条 ``removed``，
而不是一串看不出重点的错位。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final, Literal

from docfidelity.contracts.template import TemplateFingerprint, TemplatePackage
from docfidelity.platform.hashing import canonical_json, digest_payload

from .fingerprint import (
    ALL_SECTIONS,
    FINGERPRINT_SECTIONS,
    SOURCE_SECTION,
    section_payloads,
)

ChangeKind = Literal["added", "removed", "changed"]

#: 变更类别。取值就是验收 14 的四类，外加一个"原始件"。
CATEGORY_STYLE_LAYER: Final = "样式层"
CATEGORY_TABLE_PROTOTYPE: Final = "表原型"
CATEGORY_STRUCTURE: Final = "结构"
CATEGORY_PAGE_SETUP: Final = "页面设置"
CATEGORY_SOURCE: Final = "原始件"

#: 分区 → 归属类别。``numbering`` / ``used_styles`` 都归"样式层"：它们改变的
#: 是标题外观与可引用样式面，属于同一类判断。
_CATEGORY_BY_SECTION: Final[dict[str, str]] = {
    "styles": CATEGORY_STYLE_LAYER,
    "page_setup": CATEGORY_PAGE_SETUP,
    "numbering": CATEGORY_STYLE_LAYER,
    "used_styles": CATEGORY_STYLE_LAYER,
    "outline": CATEGORY_STRUCTURE,
    "blocks": CATEGORY_STRUCTURE,
    "placeholders": CATEGORY_STRUCTURE,
    SOURCE_SECTION: CATEGORY_SOURCE,
}

#: 表原型的判据：表格条件格式（``w:tblStylePr``）是"表头底色掉了"那类问题的所在。
_TABLE_PROTOTYPE_MARKER: Final = "tbl_style_pr"

#: 带稳定 id 的列表：``分区内相对路径 -> id 字段名``。
_KEYED_LISTS: Final[dict[str, str]] = {
    "styles": "style_id",
    "used_styles.p_style": "style_id",
    "used_styles.tbl_style": "style_id",
    "numbering.abstract_num": "abstract_num_id",
    "numbering.num": "num_id",
    "numbering.heading_bindings": "style_id",
    "outline.slots": "slot_id",
    "blocks.index.blocks": "block_id",
}

#: 骨架树里的 ``slots`` 是扁平索引的挂载副本（``outline.slots`` 才是权威）。
#: 不排除它的话，同一个槽位变更会在 diff 里出现两次，读的人会以为改了两处。
_CHAPTER_TREE_KEY: Final = "chapter_tree"
_DUPLICATE_KEYS_UNDER_TREE: Final[frozenset[str]] = frozenset({"slots"})


@dataclass(frozen=True)
class ImpactSpec:
    """一个分区的影响面：给人看的说明 + 什么情况下算**阻断**。"""

    text: str
    blocking_removals: tuple[str, ...] = ()


#: 分区 → 影响面（DD-7.4 表格的落地）。``blocking_removals`` 里放的是分区内
#: 相对路径前缀，只有 ``removed`` 且命中前缀才算阻断——"改了一条样式"不该阻断
#: 构建，"档案引用的 block_id 没了"必须阻断。
SECTION_IMPACTS: Final[dict[str, ImpactSpec]] = {
    "styles": ImpactSpec("受影响的版式决策（标题样式、表格列宽）与历史产物的样式合规性"),
    "page_setup": ImpactSpec("版心宽 → 表格列宽归一、图片缩放结果"),
    "numbering": ImpactSpec("编号连续性与标题外观"),
    "used_styles": ImpactSpec(
        "生成期可引用的样式面（FR-M2-13：新注入内容只允许引用这里出现过的样式）"
    ),
    "outline": ImpactSpec(
        "章节骨架兼容性 → 档案可满足性",
        blocking_removals=("slots[slot_id=",),
    ),
    "blocks": ImpactSpec(
        "被引用 block_id 是否仍存在",
        # ``assets`` 是「文件名 → 摘要」的字典，条目路径形如 ``blocks.assets.cover.png``；
        # ``index.blocks`` 是列表，条目路径形如 ``blocks.index.blocks[block_id=X]``。
        # 两种形态的路径拼法不同，前缀必须分别写。
        blocking_removals=("index.blocks[block_id=", "assets."),
    ),
    "placeholders": ImpactSpec("生成期必须被替换或显式清除的模板文本集合"),
    SOURCE_SECTION: ImpactSpec(
        "原件已改动，但未落到任何契约分区——需人工确认是噪声还是未建模的改动"
    ),
}


@dataclass(frozen=True)
class FieldChange:
    """一条字段级变更。``before`` / ``after`` 都是确定性 JSON 文本。"""

    path: str
    section: str
    category: str
    kind: ChangeKind
    before: str | None = None
    after: str | None = None

    def describe(self) -> str:
        if self.kind == "added":
            return f"[{self.category}] + {self.path} = {self.after}"
        if self.kind == "removed":
            return f"[{self.category}] - {self.path}（原 {self.before}）"
        return f"[{self.category}] ~ {self.path}: {self.before} → {self.after}"


@dataclass(frozen=True)
class SectionDelta:
    """一个分区的增量。"""

    section: str
    digest_before: str
    digest_after: str
    changes: tuple[FieldChange, ...]
    impact: str
    blocking: bool

    @property
    def changed(self) -> bool:
        return bool(self.changes)


@dataclass(frozen=True)
class TemplateVersionDiff:
    """两个模板版本之间的差异。"""

    before_ref: str
    after_ref: str
    deltas: tuple[SectionDelta, ...]

    @property
    def changed_sections(self) -> tuple[str, ...]:
        return tuple(delta.section for delta in self.deltas if delta.changed)

    @property
    def blocking_deltas(self) -> tuple[SectionDelta, ...]:
        return tuple(delta for delta in self.deltas if delta.blocking)

    @property
    def identical(self) -> bool:
        return not self.changed_sections

    @property
    def ok(self) -> bool:
        """没有硬阻断即 ``True``。构建前预检据此决定是否放行。"""
        return not self.blocking_deltas

    def changelog(self, *, limit: int | None = None) -> tuple[str, ...]:
        """人读的差异清单，按分区顺序、字段路径顺序排列。"""
        lines: list[str] = []
        for delta in self.deltas:
            for change in delta.changes:
                lines.append(change.describe())
        return tuple(lines if limit is None else lines[:limit])

    def summary(self) -> str:
        if self.identical:
            return f"{self.before_ref} → {self.after_ref}：无差异"
        parts = [f"{name}（{_count(self, name)} 处）" for name in self.changed_sections]
        head = f"{self.before_ref} → {self.after_ref}：{'、'.join(parts)}"
        if self.blocking_deltas:
            blocking = "、".join(delta.section for delta in self.blocking_deltas)
            return head + f"；**阻断项**：{blocking}"
        return head


def diff_fingerprints(before: TemplateFingerprint, after: TemplateFingerprint) -> tuple[str, ...]:
    """只比摘要，返回变化的分区名。

    P04 页面列出"与上一版本的差异入口"时用这条通路：两个版本的契约不必都加载。
    """
    names = [name for name in ALL_SECTIONS if name in before.sections or name in after.sections]
    return tuple(name for name in names if before.sections.get(name) != after.sections.get(name))


def diff_packages(
    before: TemplatePackage,
    after: TemplatePackage,
    *,
    block_assets_before: Mapping[str, bytes] | None = None,
    block_assets_after: Mapping[str, bytes] | None = None,
) -> TemplateVersionDiff:
    """两个模板包的字段级差异。

    ``source`` 分区从两个包各自的指纹里取（包目录不含原件，无法重算）；
    任一侧没有指纹就跳过该分区，而不是报成"原件变了"——把"没算过"当成
    "变了"会让每一次升级都伴着一条假告警。
    """
    payloads_before = section_payloads(before, block_assets=block_assets_before)
    payloads_after = section_payloads(after, block_assets=block_assets_after)

    deltas: list[SectionDelta] = []
    for section in FINGERPRINT_SECTIONS:
        deltas.append(
            _delta_for(
                section,
                payloads_before.get(section),
                payloads_after.get(section),
            )
        )
    source_delta = _delta_for_source(before, after)
    if source_delta is not None:
        deltas.append(source_delta)

    return TemplateVersionDiff(
        before_ref=before.manifest.ref,
        after_ref=after.manifest.ref,
        deltas=tuple(deltas),
    )


def flatten(payload: Any, *, prefix: str = "") -> dict[str, str]:
    """把分区载荷折成「点分路径 → 确定性 JSON 文本」。

    路径**带分区前缀**（``page_setup.margins.left_twips``）：变更清单是给人读的，
    而 ``left_twips`` 单独出现时看不出说的是版心还是表格列宽。阻断判据的前缀
    匹配也依赖这个前缀，两处用同一套拼法才不会一边算出一边算不出。

    折成文本而不是保留原值：Python 里 ``True == 1``、``1 == 1.0``，直接比值会
    把"布尔改成了整数"这类真实变更判成没变。
    """
    return dict(_walk(payload, prefix, frozenset()))


def _walk(node: Any, prefix: str, ignore: frozenset[str]) -> list[tuple[str, str]]:
    if isinstance(node, dict):
        pairs: list[tuple[str, str]] = []
        for key in sorted(node):
            if key in ignore:
                continue
            child = f"{prefix}.{key}" if prefix else key
            child_ignore = (
                ignore | _DUPLICATE_KEYS_UNDER_TREE if key == _CHAPTER_TREE_KEY else ignore
            )
            pairs.extend(_walk(node[key], child, child_ignore))
        return pairs
    if isinstance(node, list):
        key_field = _KEYED_LISTS.get(prefix)
        pairs = []
        for index, item in enumerate(node):
            if key_field is not None and isinstance(item, dict) and key_field in item:
                label = f"[{key_field}={item[key_field]}]"
            else:
                label = f"[{index}]"
            pairs.extend(_walk(item, f"{prefix}{label}", ignore))
        return pairs
    return [(prefix, _render(node))]


def _render(value: Any) -> str:
    return canonical_json(value).decode("utf-8")


def _delta_for(section: str, before: Any, after: Any) -> SectionDelta:
    spec = SECTION_IMPACTS[section]
    before_flat = flatten(before, prefix=section)
    after_flat = flatten(after, prefix=section)
    changes: list[FieldChange] = []
    for path in sorted(set(before_flat) | set(after_flat)):
        old = before_flat.get(path)
        new = after_flat.get(path)
        if old == new:
            continue
        if old is None:
            kind: ChangeKind = "added"
        elif new is None:
            kind = "removed"
        else:
            kind = "changed"
        changes.append(
            FieldChange(
                path=path,
                section=section,
                category=_category_of(section, path),
                kind=kind,
                before=old,
                after=new,
            )
        )
    prefixes = tuple(f"{section}.{item}" for item in spec.blocking_removals)
    blocking = any(
        change.kind == "removed" and change.path.startswith(prefix)
        for change in changes
        for prefix in prefixes
    )
    return SectionDelta(
        section=section,
        digest_before=digest_payload(before),
        digest_after=digest_payload(after),
        changes=tuple(changes),
        impact=spec.text,
        blocking=blocking,
    )


def _delta_for_source(before: TemplatePackage, after: TemplatePackage) -> SectionDelta | None:
    old = before.fingerprint.sections.get(SOURCE_SECTION) if before.fingerprint else None
    new = after.fingerprint.sections.get(SOURCE_SECTION) if after.fingerprint else None
    if old is None or new is None:
        return None
    return _delta_for(SOURCE_SECTION, {SOURCE_SECTION: old}, {SOURCE_SECTION: new})


def _category_of(section: str, path: str) -> str:
    base = _CATEGORY_BY_SECTION[section]
    if section == "styles" and _TABLE_PROTOTYPE_MARKER in path:
        return CATEGORY_TABLE_PROTOTYPE
    return base


def _count(diff: TemplateVersionDiff, section: str) -> int:
    for delta in diff.deltas:
        if delta.section == section:
            return len(delta.changes)
    return 0


__all__: list[str] = [
    "CATEGORY_PAGE_SETUP",
    "CATEGORY_SOURCE",
    "CATEGORY_STRUCTURE",
    "CATEGORY_STYLE_LAYER",
    "CATEGORY_TABLE_PROTOTYPE",
    "SECTION_IMPACTS",
    "ChangeKind",
    "FieldChange",
    "ImpactSpec",
    "SectionDelta",
    "TemplateVersionDiff",
    "diff_fingerprints",
    "diff_packages",
    "flatten",
]
