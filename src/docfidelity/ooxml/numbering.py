"""编号体系：读取、标题绑定、去重合并与双重编号检测（DD-4.3）。

为什么"合并等价 abstractNum"是高风险操作
----------------------------------------
``w:num`` 是实例、``w:abstractNum`` 是定义。多个 ``w:num`` 指向同一份定义是常态，
而"两份定义看起来一样、实际在 Word 中因 ``w:lvlOverride`` 或后续手动调整而不同"
也很常见。因此本模块的合并只做**定义签名严格相等**的合并，且永不删除仍被引用的
``w:num``；合并结果一律标记 ``requires_visual_check=True``，由 qa 的抽页目检兜底
（第 13 章风险项）。
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field

from docfidelity.contracts.ir import ContentIR, ContentUnit
from docfidelity.contracts.qa import Finding, Location
from docfidelity.contracts.template import (
    AbstractNum,
    ChapterNode,
    HeadingBinding,
    NumberingContract,
    NumberLevel,
    NumInstance,
    OutlineSkeleton,
)

from .doc import Element, attr, child_of, find_all, find_first, localname, qn, set_attr
from .package import NUMBERING_PART, STYLES_PART, DocxPackage

#: 标题文本里手写编号的形态。仅用于识别"双重编号"，不用于解析编号语义。
#: 刻意要求"编号 + 分隔符"，否则"2024 年度总结"这类正常标题会被误判。
_MANUAL_NUMBER_RE = re.compile(
    r"^\s*(?:"
    r"\d+(?:\.\d+)+"  # 1.1 / 2.3.1
    r"|\d+\s*[、.．]"  # 1、 / 1.
    r"|[（(]\s*\d+\s*[)）]"  # (1)
    r"|[一二三四五六七八九十]+\s*[、.．]"  # 一、
    r")"
)


@dataclass
class UnifyReport:
    """编号合并结果。"""

    merged_definitions: int = 0
    repointed_instances: int = 0
    removed_instances: list[int] = field(default_factory=list)
    removed_definitions: list[int] = field(default_factory=list)
    requires_visual_check: bool = True
    warnings: list[str] = field(default_factory=list)


# --------------------------------------------------------------------- 读取
def read_numbering(pkg: DocxPackage) -> NumberingContract:
    """读取编号契约。包内没有 ``numbering.xml`` 时返回空契约而非报错。"""
    part = pkg.find_part(NUMBERING_PART)
    if part is None:
        return NumberingContract()

    root = part.read_tree
    definitions: list[AbstractNum] = []
    for element in find_all(root, "w:abstractNum"):
        definition_id = _int_or_none(attr(element, "w:abstractNumId"))
        if definition_id is None:
            continue
        definitions.append(AbstractNum(abstract_num_id=definition_id, levels=_read_levels(element)))

    instances: list[NumInstance] = []
    for element in find_all(root, "w:num"):
        instance_id = _int_or_none(attr(element, "w:numId"))
        abstract_id = _int_or_none(attr(find_first(element, "w:abstractNumId"), "w:val"))
        if instance_id is None or abstract_id is None:
            continue
        instances.append(NumInstance(num_id=instance_id, abstract_num_id=abstract_id))

    bindings = heading_num_bindings(pkg)
    return NumberingContract(
        abstract_num=definitions,
        num=instances,
        heading_bindings=[
            HeadingBinding(style_id=style_id, num_id=num_id, ilvl=ilvl)
            for style_id, (num_id, ilvl) in bindings.items()
        ],
    )


def _read_levels(abstract: Element) -> list[NumberLevel]:
    levels: list[NumberLevel] = []
    for index, element in enumerate(find_all(abstract, "w:lvl")):
        declared_ilvl = _int_or_none(attr(element, "w:ilvl"))
        indent = child_of(child_of(element, "w:pPr"), "w:ind")
        fonts = child_of(child_of(element, "w:rPr"), "w:rFonts")
        levels.append(
            NumberLevel(
                ilvl=declared_ilvl if declared_ilvl is not None else index,
                num_fmt=attr(find_first(element, "w:numFmt"), "w:val") or "decimal",
                lvl_text=attr(find_first(element, "w:lvlText"), "w:val") or "",
                start=_int_or_none(attr(find_first(element, "w:start"), "w:val")) or 1,
                indent_left_twips=_int_or_none(attr(indent, "w:left")),
                indent_hanging_twips=_int_or_none(
                    attr(indent, "w:hanging") or attr(indent, "w:firstLine")
                ),
                font_east_asian=attr(fonts, "w:eastAsia"),
            )
        )
    return levels


def heading_num_bindings(pkg: DocxPackage) -> dict[str, tuple[int, int]]:
    """标题样式 -> ``(numId, ilvl)``。仅统计段落样式上的 ``w:numPr``。"""
    styles_part = pkg.find_part(STYLES_PART)
    if styles_part is None:
        return {}
    bindings: dict[str, tuple[int, int]] = {}
    for element in find_all(styles_part.read_tree, "w:style"):
        if (element.get(qn("w:type")) or "paragraph") != "paragraph":
            continue
        style_id = element.get(qn("w:styleId"))
        num_element = child_of(child_of(element, "w:pPr"), "w:numPr")
        if style_id is None or num_element is None:
            continue
        num_id = _int_or_none(attr(find_first(num_element, "w:numId"), "w:val"))
        if num_id is None:
            continue
        ilvl = _int_or_none(attr(find_first(num_element, "w:ilvl"), "w:val"))
        bindings[style_id] = (num_id, ilvl if ilvl is not None else 0)
    return bindings


def _int_or_none(raw: str | None) -> int | None:
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


# --------------------------------------------------------------------- 合并
def unify_numbering(pkg: DocxPackage, contract: NumberingContract | None = None) -> UnifyReport:
    """合并等价 ``abstractNum``、压缩 ``num``，**保留被引用者**（DD-4.3）。

    安全边界（三条，缺一条都可能删掉在用编号）：

    1. 只合并**层级签名完全相等**的定义；
    2. 只删除"在正文部件与样式中都查不到引用"的 ``w:num``；
    3. 引用集合为空时**整体跳过**并给出警告 —— 一个引用都没找到，比找到一堆更
       可能是扫描口径出了问题，此时不动比动更安全。
    """
    report = UnifyReport()
    part = pkg.find_part(NUMBERING_PART)
    if part is None:
        return report

    contract = contract if contract is not None else read_numbering(pkg)
    root = part.tree
    referenced = _collect_num_ids(pkg)
    if not referenced and contract.num:
        report.warnings.append("未在正文与样式中找到任何编号引用，为避免误删已跳过压缩")
        return report

    canonical = _canonical_definitions(root)
    report.merged_definitions = len({value for key, value in canonical.items() if value != key})
    report.repointed_instances = _repoint_instances(root, canonical)
    kept_abstract = _prune_instances(root, referenced, report)
    _prune_definitions(root, kept_abstract, report)

    part.mark_dirty()
    if report.merged_definitions or report.removed_instances or report.removed_definitions:
        report.warnings.append(
            "编号结构已变更：请在 qa 阶段对该文档做抽页目检比对，确认编号视觉表现未变"
        )
    return report


def _canonical_definitions(root: Element) -> dict[str, str]:
    """``abstractNumId -> 规范 id``；签名相同的定义归并到最小 id。"""
    groups: dict[tuple[tuple[str | None, ...], ...], list[str]] = {}
    for element in find_all(root, "w:abstractNum"):
        raw_id = attr(element, "w:abstractNumId")
        if raw_id is None:
            continue
        groups.setdefault(_definition_signature(element), []).append(raw_id)

    mapping: dict[str, str] = {}
    for ids in groups.values():
        canonical = min(ids, key=lambda value: _int_or_none(value) or 0)
        for raw_id in ids:
            mapping[raw_id] = canonical
    return mapping


def _definition_signature(element: Element) -> tuple[tuple[str | None, ...], ...]:
    """定义签名：层级编号格式 + 模板文本 + 起始值 + 缩进。

    刻意**不含** ``w:nsid``：它是 Word 生成的内部标识，两份内容相同的定义 nsid
    必然不同，把它算进签名会让合并永远不发生。
    """
    signature: list[tuple[str | None, ...]] = []
    for level in find_all(element, "w:lvl"):
        indent = child_of(child_of(level, "w:pPr"), "w:ind")
        signature.append(
            (
                attr(level, "w:ilvl"),
                attr(find_first(level, "w:numFmt"), "w:val"),
                attr(find_first(level, "w:lvlText"), "w:val"),
                attr(find_first(level, "w:start"), "w:val"),
                attr(indent, "w:left"),
                attr(indent, "w:hanging") or attr(indent, "w:firstLine"),
            )
        )
    return tuple(signature)


def _repoint_instances(root: Element, canonical: Mapping[str, str]) -> int:
    """把 ``w:num`` 指向规范定义；返回改动的实例数。"""
    changed = 0
    for instance in find_all(root, "w:num"):
        pointer = find_first(instance, "w:abstractNumId")
        if pointer is None:
            continue
        current = attr(pointer, "w:val")
        if current is None:
            continue
        target = canonical.get(current)
        if target is None or target == current:
            continue
        set_attr(pointer, "w:val", target)
        changed += 1
    return changed


def _collect_num_ids(pkg: DocxPackage) -> set[str]:
    """收集正文与样式中引用到的所有 ``numId``。"""
    referenced: set[str] = set()
    parts = list(pkg.parts(scope="content"))
    styles_part = pkg.find_part(STYLES_PART)
    if styles_part is not None:
        parts.append(styles_part)
    for part in parts:
        for element in part.read_tree.iter():
            if localname(element) != "numPr":
                continue
            raw_id = attr(find_first(element, "w:numId"), "w:val")
            if raw_id is not None:
                referenced.add(raw_id)
    return referenced


def _prune_instances(root: Element, referenced: set[str], report: UnifyReport) -> set[str]:
    """删除未被引用的 ``w:num``，返回仍在用的 ``abstractNumId`` 集合。"""
    kept: set[str] = set()
    for instance in list(find_all(root, "w:num")):
        raw_id = attr(instance, "w:numId")
        if raw_id is not None and raw_id in referenced:
            abstract_id = attr(find_first(instance, "w:abstractNumId"), "w:val")
            if abstract_id is not None:
                kept.add(abstract_id)
            continue
        removed = _int_or_none(raw_id)
        if removed is not None:
            report.removed_instances.append(removed)
        root.remove(instance)
    return kept


def _prune_definitions(root: Element, kept: set[str], report: UnifyReport) -> None:
    for definition in list(find_all(root, "w:abstractNum")):
        raw_id = attr(definition, "w:abstractNumId")
        if raw_id is None or raw_id in kept:
            continue
        removed = _int_or_none(raw_id)
        if removed is not None:
            report.removed_definitions.append(removed)
        root.remove(definition)


# ------------------------------------------------------------- 双重编号检测
def assert_no_text_numbering(
    ir: ContentIR,
    outline: OutlineSkeleton,
    bindings: Mapping[str, tuple[int, int]],
) -> list[Finding]:
    """检测标题文本内手写了编号、而样式又自带自动编号（FR-M4-04）。

    ``bindings`` 是必须显式传入的第三个参数，而不是从 ``ir`` 或 ``outline`` 里猜：
    "这一级是否自动编号"只有模板契约知道。靠标题文本像不像编号来猜，会把
    "2024 年度总结"这类正常标题判成违规；反过来漏判则会让产物出现
    "1.1 1.1 概述"这种双重编号，而这种错误在评审稿里几乎不会被发现。
    """
    level_styles = _level_style_map(outline)
    findings: list[Finding] = []
    for unit, chapter_path in _walk_with_path(ir.units):
        if unit.type != "heading" or unit.level is None:
            continue
        style_id = level_styles.get(unit.level)
        if style_id is None or style_id not in bindings:
            continue
        text = (unit.text or "").strip()
        if _MANUAL_NUMBER_RE.match(text) is None:
            continue
        findings.append(
            Finding(
                rule_id="FR-M4-04",
                severity="major",
                message=(
                    f"标题样式 {style_id} 已由模板自动编号"
                    f"（numId={bindings[style_id][0]}），标题文本内不应再手写编号"
                ),
                location=Location(unit_id=unit.unit_id, chapter_path=list(chapter_path)),
                evidence=text[:80],
                suggestion="删除标题文本开头的编号，编号由渲染层按模板编号体系统一生成",
            )
        )
    return findings


def level_style_map(outline: OutlineSkeleton) -> dict[int, str]:
    """``层级 -> 该级标题样式``（同层多节点取首个非空声明）。

    公开的理由：**渲染期的每一步都要用这个映射**——注入按它选标题原型、裁剪按它
    认内容区起点、文本编号检查按它判"这一级是否自动编号"。三处各写一遍遍历，
    只要有一处把"同层多节点"的处理写成"最后一个胜出"，产物就会在不同的地方
    用不同的样式。故只留这一份实现。
    """
    mapping: dict[int, str] = {}

    def visit(nodes: Sequence[ChapterNode]) -> None:
        for node in nodes:
            if node.style_id and node.level not in mapping:
                mapping[node.level] = node.style_id
            visit(node.children)

    visit(outline.chapter_tree)
    return mapping


def _level_style_map(outline: OutlineSkeleton) -> dict[int, str]:
    """内部别名（保持既有调用点不变）。"""
    return level_style_map(outline)


def _walk_with_path(
    units: Sequence[ContentUnit], prefix: tuple[str, ...] = ()
) -> Iterator[tuple[ContentUnit, tuple[str, ...]]]:
    """遍历单元并携带章节路径（``Location.chapter_path`` 的来源）。"""
    path = prefix
    for unit in units:
        if unit.type == "heading" and unit.text:
            path = (*prefix, unit.text)
        yield unit, path
        yield from _walk_with_path(unit.children, path)


__all__ = [
    "UnifyReport",
    "assert_no_text_numbering",
    "heading_num_bindings",
    "level_style_map",
    "read_numbering",
    "unify_numbering",
]
