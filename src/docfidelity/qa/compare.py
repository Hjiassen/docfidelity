"""稳定化比对器（DD-10.4 / FR-M5-11 / FR-M5-13）。

为什么不能比字节
----------------
OOXML 的字节受**易变标识**支配：``_Toc123456`` 书签 ID 由 Word 每次刷新域重新分配，
关系 ID 依插入顺序重排，``w:rsid*`` 是每次编辑的会话标识，``dcterms:modified`` 是
保存时刻。逐字节比对必然 flaky，而 flaky 的比对结果会让人得出"改了一处、报了全篇"
的结论，最终把比对关掉。故三种模式**一律**先经
:func:`~docfidelity.ooxml.normalize.normalized_parts` 归一化——那是全内核唯一的
易变标识处理实现（DD-4.7），本模块不另写一份。

三种模式
--------
=================  ==================================================================
``structure``      部件集合 + 结构性计数（段落数 / 样式引用 / 表格行列 / 图片 extent /
                   分页点数）。回答"结构变了没有"。
``text``           在结构之上补**行级**差异（对归一化后的 XML 逐行 diff）。
                   回答"哪一行变了"——并且已剔除易变标识造成的假差异。
``contract``       逐**字段**比对 JSON 部件（``contract/*.json``）。回答"契约的哪个
                   字段变了"（模板包版本 diff 用）。
=================  ==================================================================

``SnapshotDiff`` 与其承载不了的"标注"
------------------------------------
契约的 :class:`~docfidelity.contracts.qa.SnapshotDiff` 只有 ``added`` / ``removed`` /
``changed`` / ``normalized`` 四个字段，而 DD-10.4 要求 text 模式**在结果中标注**
"已剔除目录书签 ID 等假差异"。故本模块返回 :class:`DiffResult`：``snapshot`` 是可
序列化、可直接落库的契约模型，``notes`` 承载那类"关于结论本身"的说明。两者分开
而不是往 ``changed`` 里塞一句人类文案——把标注混进节点路径会让下游无法区分
"这一条是差异"与"这一条是说明"。
"""

from __future__ import annotations

import difflib
import json
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any, Final, Literal

from docfidelity.contracts.qa import SnapshotDiff
from docfidelity.ooxml import (
    DOCUMENT_PART,
    DocxPackage,
    attr,
    find_first,
    localname,
    normalized_parts,
)

__all__ = [
    "CONTRACT_MODE_NOTE",
    "DiffMode",
    "DiffResult",
    "STRUCTURE_MODE_NOTE",
    "TEXT_MODE_NOTE",
    "compare",
    "metrics_of",
]

DiffMode = Literal["structure", "text", "contract"]

#: text 模式的标注（DD-10.4 要求"在结果中标注"）。
TEXT_MODE_NOTE: Final = (
    "文本比对已在归一化后进行：目录书签 ID、关系 ID、修订会话 ID、文档 ID 与时间戳"
    "等易变标识已剔除，下列差异不是它们造成的"
)
STRUCTURE_MODE_NOTE: Final = "结构比对只列部件与结构性计数，不比对字节"
CONTRACT_MODE_NOTE: Final = "契约比对逐字段列出差异（JSON 部件）"

#: 单次比对最多列出的差异条目数。超出只报总数：一份长文档的逐行差异可能有上千条，
#: 把它们全量灌进报告会让真正的第一条被埋掉。
MAX_ITEMS: Final = 50


@dataclass
class DiffResult:
    """比对结果：契约模型 + 关于结论本身的说明。"""

    mode: DiffMode
    snapshot: SnapshotDiff
    notes: list[str] = field(default_factory=list)

    @property
    def equal(self) -> bool:
        return not (self.snapshot.added or self.snapshot.removed or self.snapshot.changed)

    def counts(self) -> dict[str, int]:
        return {
            "added": len(self.snapshot.added),
            "removed": len(self.snapshot.removed),
            "changed": len(self.snapshot.changed),
        }

    def summary(self) -> str:
        counts = self.counts()
        return (
            f"新增 {counts['added']} / 删除 {counts['removed']} / 变更 {counts['changed']}"
            f"（{'一致' if self.equal else '有差异'}）"
        )


def compare(
    baseline: DocxPackage,
    candidate: DocxPackage,
    *,
    mode: DiffMode = "text",
    baseline_ref: str = "",
    candidate_ref: str = "",
) -> DiffResult:
    """比对两个包（DD-10.4）。

    ``mode`` 决定差异的**分辨率**：``structure`` 到部件与计数，``text`` 到行，
    ``contract`` 到 JSON 字段。三者都经同一套归一化，故"换模式就换结论"不会发生。
    """
    if mode == "contract":
        added, removed, changed, notes = _diff_json_parts(baseline, candidate)
    else:
        base_parts = normalized_parts(baseline)
        cand_parts = normalized_parts(candidate)
        added = sorted(set(cand_parts) - set(base_parts))
        removed = sorted(set(base_parts) - set(cand_parts))
        changed = sorted(
            name
            for name in set(base_parts) & set(cand_parts)
            if base_parts[name] != cand_parts[name]
        )
        notes = [STRUCTURE_MODE_NOTE]
        if mode == "text":
            notes = [TEXT_MODE_NOTE]
            changed = _line_changes(base_parts, cand_parts, changed)
        changed = _with_metric_changes(changed, baseline, candidate)

    snapshot = SnapshotDiff(
        baseline_ref=baseline_ref,
        candidate_ref=candidate_ref,
        normalized=True,
        added=added[:MAX_ITEMS],
        removed=removed[:MAX_ITEMS],
        changed=changed[:MAX_ITEMS],
    )
    return DiffResult(mode=mode, snapshot=snapshot, notes=notes)


# ------------------------------------------------------------------ 结构计数
def metrics_of(pkg: DocxPackage) -> dict[str, int]:
    """结构性计数（DD-10.4 structure 模式列出的那几项）。"""
    paragraphs = 0
    tables = 0
    rows = 0
    images = 0
    style_refs: set[str] = set()
    for part in pkg.parts(scope="content"):
        tree = part.read_tree
        for element in tree.iter():
            name = localname(element)
            if name == "p":
                paragraphs += 1
            elif name == "tbl":
                tables += 1
            elif name == "tr":
                rows += 1
            elif name == "extent":
                images += 1
            elif name in _STYLE_REF_LOCALS:
                value = attr(element, "w:val")
                if value:
                    style_refs.add(value)
    return {
        "paragraphs": paragraphs,
        "style_refs": len(style_refs),
        "tables": tables,
        "table_rows": rows,
        "images": images,
        "page_breaks": _page_breaks(pkg),
    }


_STYLE_REF_LOCALS: Final[tuple[str, ...]] = ("pStyle", "tblStyle", "rStyle")


def _page_breaks(pkg: DocxPackage) -> int:
    """分页点数。**复用**渲染层同一实现，口径不可能漂移（``w:br[type=page]`` +
    ``w:pageBreakBefore``，且都按"归属段落"计）。"""
    from docfidelity.render.pagination import count_page_breaks

    part = pkg.find_part(DOCUMENT_PART)
    if part is None:
        return 0
    body = find_first(part.read_tree, "w:body")
    return 0 if body is None else count_page_breaks(body)


def _with_metric_changes(
    changed: list[str], baseline: DocxPackage, candidate: DocxPackage
) -> list[str]:
    base = metrics_of(baseline)
    cand = metrics_of(candidate)
    entries = list(changed)
    for name in sorted(base):
        if base[name] != cand.get(name):
            entries.append(f"metric:{name}: {base[name]} -> {cand.get(name)}")
    return entries


# ------------------------------------------------------------------ 行级 diff
def _line_changes(
    base_parts: Mapping[str, bytes], cand_parts: Mapping[str, bytes], changed: list[str]
) -> list[str]:
    """对已变更的部件做行级 diff，产出 ``<部件>#<行号>: <文本>`` 条目。"""
    entries: list[str] = []
    for name in changed:
        base_lines = _lines(base_parts[name])
        cand_lines = _lines(cand_parts[name])
        diff = difflib.unified_diff(base_lines, cand_lines, lineterm="", n=0)
        for raw in diff:
            if raw.startswith(("---", "+++", "@@")):
                continue
            marker = raw[:1]
            if marker not in {"+", "-"}:
                continue
            text = raw[1:]
            if not text.strip():
                continue
            entries.append(f"{name}: {marker}{text.strip()[:200]}")
            if len(entries) >= MAX_ITEMS:
                return entries
    return entries


def _lines(payload: bytes) -> list[str]:
    text = payload.decode("utf-8", errors="replace")
    return [line for line in text.splitlines() if line.strip()]


# ------------------------------------------------------------------ 契约 diff
def _diff_json_parts(
    baseline: DocxPackage, candidate: DocxPackage
) -> tuple[list[str], list[str], list[str], list[str]]:
    """逐字段比对两边都有的 JSON 部件，返回 ``(added, removed, changed, notes)``。"""
    base_json = dict(_json_parts(baseline))
    cand_json = dict(_json_parts(candidate))
    added = sorted(set(cand_json) - set(base_json))
    removed = sorted(set(base_json) - set(cand_json))
    changed: list[str] = []
    for name in sorted(set(base_json) & set(cand_json)):
        left = _flatten(base_json[name])
        right = _flatten(cand_json[name])
        for path in sorted(set(left) | set(right)):
            if left.get(path) == right.get(path):
                continue
            changed.append(f"{name}:{path}: {left.get(path)!r} -> {right.get(path)!r}")
    return added, removed, changed, [CONTRACT_MODE_NOTE]


def _json_parts(pkg: DocxPackage) -> Iterator[tuple[str, Any]]:
    for part in pkg.parts(scope="all"):
        if not part.path.endswith(".json"):
            continue
        try:
            yield part.path, json.loads(part.to_bytes().decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            continue


def _flatten(node: Any, prefix: str = "") -> dict[str, str]:
    """把嵌套 JSON 摊平成 ``字段路径 -> 取值``。"""
    flat: dict[str, str] = {}
    if isinstance(node, dict):
        for key, value in node.items():
            flat.update(_flatten(value, f"{prefix}.{key}" if prefix else str(key)))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            flat.update(_flatten(value, f"{prefix}[{index}]"))
    else:
        flat[prefix] = repr(node)
    return flat
