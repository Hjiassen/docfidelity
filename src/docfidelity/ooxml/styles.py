"""样式层：读取、克隆、ID 重映射、直设格式检测（DD-4.2）。

本模块承载「样式层零改动」承诺里最容易破功的一环。
**跨文档克隆样式必须同时处理 ``w:pStyle`` 与 ``w:tblStyle``**（1.2 第 8 条教训）：
只改段落样式引用时，产物的表格会"看起来样式还在、实际表头掉色"，且因为
``w:tblStyle`` 仍指向旧的 styleId，Word 不会报错、只会静默回退到默认表格样式。

因此 :func:`remap_style_ids` 返回的 :class:`RemapReport` 带一条**硬校验**：
调用方声明了表格类样式映射，但 ``tbl_style_hits == 0`` 时报告里会出现
``表格样式映射命中 0 次`` 的警告。这条检查挡的是"只测段落样式的假通过"。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final, cast

from docfidelity.contracts.errors import EngineError, ErrorCode
from docfidelity.contracts.qa import Finding, Location
from docfidelity.contracts.template import (
    StyleContract,
    StyleType,
    TableStylePr,
    TblStylePrType,
    UsedStyle,
    UsedStylesContract,
)

from .doc import (
    Element,
    attr,
    child_of,
    deep_copy,
    find_all,
    find_first,
    localname,
    parse,
    qn,
    serialize_text,
    set_attr,
)
from .package import STYLES_PART, DocxPackage, Part

#: 引用样式的标签。``tblStyle`` 出现在这里而不是被漏掉，是本模块的核心教训。
_STYLE_REF_TAGS: dict[str, str] = {"pStyle": "p_style", "tblStyle": "tbl_style"}

#: 样式定义内部的指针标签。
_DEFINITION_POINTERS = ("basedOn", "next", "link")

#: ``pPr`` 中属于结构性声明、不算"直设格式"的子元素。
_STRUCTURAL_PPR = frozenset({"pStyle", "numPr", "sectPr", "pPrChange", "rPr", "tblPrEx"})

#: ``pPr`` 中属于分页 / 大纲控制、由专门规则负责的子元素。
_PAGINATION_PPR = frozenset(
    {"keepNext", "keepLines", "pageBreakBefore", "widowControl", "outlineLvl"}
)

#: ``rPr`` 中不算"直设格式"的子元素。
_STRUCTURAL_RPR = frozenset({"rStyle", "rPrChange"})

#: 单元格段落上由**渲染层**写入的缩进归零（FR-M4-08）判定用到的标签。
_CELL_LOCAL: Final = "tc"
_INDENT_LOCAL: Final = "ind"

_VALID_STYLE_TYPES: frozenset[str] = frozenset(
    {"paragraph", "character", "table", "numbering", "linked"}
)

_TBL_STYLE_PR_TYPES: frozenset[str] = frozenset(
    {
        "firstRow",
        "lastRow",
        "firstCol",
        "lastCol",
        "band1Vert",
        "band1Horz",
        "band2Vert",
        "band2Horz",
        "neCell",
        "nwCell",
        "seCell",
        "swCell",
    }
)


@dataclass
class RemapReport:
    """样式 ID 重映射结果。"""

    p_style_hits: int = 0
    tbl_style_hits: int = 0
    definition_pointers_updated: int = 0
    resolved_styles: dict[str, str] = field(default_factory=dict)
    unresolved_mappings: list[tuple[str, str]] = field(default_factory=list)
    missing_refs: list[str] = field(default_factory=list)
    parts_touched: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def total_hits(self) -> int:
        return self.p_style_hits + self.tbl_style_hits


@dataclass
class CloneResult:
    """样式克隆结果。``created=False`` 表示命中了"复用优先于新建"。"""

    style_id: str
    created: bool
    reason: str
    renamed_from: str | None = None
    dangling_pointers: list[str] = field(default_factory=list)


@dataclass
class AlignReport:
    """样式定义同步结果（保留 styleId，仅同步定义内容）。"""

    updated: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    missing_in_target: list[str] = field(default_factory=list)


@dataclass
class AdoptReport:
    """把外部样式体系并入本包的完整结果。"""

    clones: list[CloneResult] = field(default_factory=list)
    remap: RemapReport = field(default_factory=RemapReport)

    @property
    def created_style_ids(self) -> list[str]:
        return [item.style_id for item in self.clones if item.created]


# --------------------------------------------------------------------- 读取
def style_element_index(pkg: DocxPackage) -> dict[str, Element]:
    """``styleId -> w:style 元素``。"""
    root = pkg.part(STYLES_PART).read_tree
    index: dict[str, Element] = {}
    for element in find_all(root, "w:style"):
        style_id = element.get(qn("w:styleId"))
        if style_id:
            index[style_id] = element
    return index


def read_styles(pkg: DocxPackage) -> list[StyleContract]:
    """全量读取样式定义，含 ``tblStylePr`` 条件格式与东亚字体。

    ``raw_xml`` 保存 **整个 ``w:style`` 元素的原始序列化**。这看似冗余，但它是
    保真的唯一可靠来源：本模块没有建模的每个属性（``w:themeFont``、``w:u``、
    ``w:kern``、修订痕迹……）都靠它原样还原。回写时以原件为准，不以模型为准。
    """
    root = pkg.part(STYLES_PART).read_tree
    styles: list[StyleContract] = []
    for element in find_all(root, "w:style"):
        style_id = element.get(qn("w:styleId"))
        if not style_id:
            raise EngineError(
                ErrorCode.TEMPLATE_UNREADABLE,
                "样式定义缺少 w:styleId，无法建立引用索引",
                where={"part": STYLES_PART},
                remediation=["用 Word 另存模板原件以修复样式表"],
            )
        styles.append(_parse_style(element, style_id))
    return styles


def _parse_style(element: Element, style_id: str) -> StyleContract:
    raw_type = element.get(qn("w:type")) or "paragraph"
    style_type = cast(StyleType, raw_type if raw_type in _VALID_STYLE_TYPES else "paragraph")
    name_element = find_first(element, "w:name")
    name = attr(name_element, "w:val") if name_element is not None else None

    run_props = find_first(element, "w:rPr")
    para_props = find_first(element, "w:pPr")

    fonts = _child_of(run_props, "w:rFonts")
    spacing = _child_of(para_props, "w:spacing")
    indent = _child_of(para_props, "w:ind")
    num_element = _child_of(para_props, "w:numPr")

    return StyleContract(
        name=name or style_id,
        style_id=style_id,
        type=style_type,
        based_on=_val(find_first(element, "w:basedOn")),
        next=_val(find_first(element, "w:next")),
        link=_val(find_first(element, "w:link")),
        font_latin=_attr_of(fonts, "w:ascii"),
        font_east_asian=_attr_of(fonts, "w:eastAsia"),
        size_pt=_half_point_size(_child_of(run_props, "w:sz")),
        bold=_on_off(_child_of(run_props, "w:b")),
        italic=_on_off(_child_of(run_props, "w:i")),
        line_spacing=_line_multiple(spacing),
        space_before_pt=_twips_to_pt(_attr_of(spacing, "w:before")),
        space_after_pt=_twips_to_pt(_attr_of(spacing, "w:after")),
        indent_left_twips=_int_attr(indent, "w:left") or _int_attr(indent, "w:start"),
        indent_first_line_twips=_indent_first_line(indent),
        num_id=_int_attr(_child_of(num_element, "w:numId"), "w:val"),
        tbl_style_pr=_parse_tbl_style_pr(element),
        raw_xml=serialize_text(element),
    )


def _parse_tbl_style_pr(style_element: Element) -> list[TableStylePr]:
    """解析 ``w:tblStylePr`` 条件格式。

    漏掉它会直接导致跨文档克隆后"表头掉色"（1.2 第 8 条），因此即便只建模了
    少数属性，也**必须**逐条保留进来。
    """
    result: list[TableStylePr] = []
    for element in find_all(style_element, "w:tblStylePr"):
        pr_type = element.get(qn("w:type"))
        if pr_type not in _TBL_STYLE_PR_TYPES:
            continue
        run_props = find_first(element, "w:rPr")
        shading = _child_of(run_props, "w:shd")
        color = _child_of(run_props, "w:color")
        result.append(
            TableStylePr(
                type=cast(TblStylePrType, pr_type),
                bold=_on_off(_child_of(run_props, "w:b")),
                italic=_on_off(_child_of(run_props, "w:i")),
                shading_fill=_attr_of(shading, "w:fill"),
                font_color=_attr_of(color, "w:val"),
                raw_xml=serialize_text(element),
            )
        )
    return result


def _child_of(parent: Element | None, tag: str) -> Element | None:
    """``parent`` 为 ``None`` 时返回 ``None``，否则查直接子元素。

    不能写成 ``find_first(parent, tag) if parent else None``：lxml 元素的真值
    取决于子节点数，空 ``<w:rPr/>`` 会被判为假，于是属性静默丢失。
    """
    return child_of(parent, tag)


def _val(element: Element | None) -> str | None:
    return attr(element, "w:val") if element is not None else None


def _attr_of(element: Element | None, tag: str) -> str | None:
    return attr(element, tag) if element is not None else None


def _int_attr(element: Element | None, tag: str) -> int | None:
    raw = _attr_of(element, tag)
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _twips_to_pt(raw: str | None) -> float | None:
    if raw is None:
        return None
    try:
        return round(int(raw) / 20, 2)
    except ValueError:
        return None


def _half_point_size(element: Element | None) -> float | None:
    """``w:sz`` 以半磅为单位。"""
    raw = _val(element)
    if raw is None:
        return None
    try:
        return round(float(raw) / 2, 2)
    except ValueError:
        return None


def _on_off(element: Element | None) -> bool | None:
    """``<w:b/>`` 表示开；``w:val`` 显式给 0/false/off 表示关。"""
    if element is None:
        return None
    raw = attr(element, "w:val")
    if raw is None:
        return True
    return raw.strip().lower() not in {"0", "false", "off"}


def _line_multiple(spacing: Element | None) -> float | None:
    """行距倍数。

    只有 ``lineRule="auto"``（或缺省）时 ``w:line`` 才是 1/240 行的倍数；
    ``exact`` / ``atLeast`` 下它是 twips 固定值，无法表达为倍数 —— 此时返回
    ``None``，由 ``raw_xml`` 保证不丢。
    """
    if spacing is None:
        return None
    rule = attr(spacing, "w:lineRule")
    if rule is not None and rule != "auto":
        return None
    raw = attr(spacing, "w:line")
    if raw is None:
        return None
    try:
        return round(int(raw) / 240, 4)
    except ValueError:
        return None


def _indent_first_line(indent: Element | None) -> int | None:
    """首行缩进；``w:hanging`` 以负值表达。"""
    first_line = _int_attr(indent, "w:firstLine")
    if first_line is not None:
        return first_line
    hanging = _int_attr(indent, "w:hanging")
    return -hanging if hanging is not None else None


# ------------------------------------------------------------------ 使用统计
def used_styles(part: Part) -> UsedStylesContract:
    """统计单个部件中 ``w:pStyle`` / ``w:tblStyle`` 的引用次数（FR-M2-13）。

    两类分开计数不是形式主义：模板里"表样式被引用但段落样式没被引用"很常见，
    合并成一个计数会让"裁剪冗余时不能删"的判断失效。
    """
    counts: dict[str, dict[str, int]] = {"p_style": {}, "tbl_style": {}}
    for element in part.read_tree.iter():
        tag = localname(element)
        bucket = _STYLE_REF_TAGS.get(tag)
        if bucket is None:
            continue
        value = attr(element, "w:val")
        if not value:
            continue
        counts[bucket][value] = counts[bucket].get(value, 0) + 1
    return UsedStylesContract(
        p_style=[UsedStyle(style_id=key, count=value) for key, value in counts["p_style"].items()],
        tbl_style=[
            UsedStyle(style_id=key, count=value) for key, value in counts["tbl_style"].items()
        ],
    )


def used_styles_in_package(pkg: DocxPackage) -> UsedStylesContract:
    """聚合全包正文部件的样式使用情况，并补上样式显示名。"""
    merged: dict[str, dict[str, int]] = {"p_style": {}, "tbl_style": {}}
    for part in pkg.parts(scope="content"):
        single = used_styles(part)
        for key, bucket in (("p_style", single.p_style), ("tbl_style", single.tbl_style)):
            for item in bucket:
                merged[key][item.style_id] = merged[key].get(item.style_id, 0) + item.count

    names = {style.style_id: style.name for style in read_styles(pkg)}
    return UsedStylesContract(
        p_style=[
            UsedStyle(style_id=key, name=names.get(key), count=value)
            for key, value in sorted(merged["p_style"].items())
        ],
        tbl_style=[
            UsedStyle(style_id=key, name=names.get(key), count=value)
            for key, value in sorted(merged["tbl_style"].items())
        ],
    )


# ---------------------------------------------------------------- 直设格式
def detect_direct_formatting(pkg: DocxPackage) -> list[Finding]:
    """检测段落上的直设格式（FR-M5-01）。

    判定口径：段落 ``pPr`` 与段落标记 ``rPr`` 中出现了**未经样式**的属性。
    结构性声明（``pStyle`` / ``numPr`` / ``sectPr``）、分页控制（``keepNext``
    等，由专门规则负责）、我们自己在图注绑定时会写的 ``keepNext``、以及渲染层
    在单元格上做的缩进归零（``w:ind`` 全零，见 :func:`_is_cell_indent_normalization`），
    都不计入——否则规则会在自己的产物上误报。

    每个段落最多产出两条判定（``pPr`` 一条、``rPr`` 一条），避免一份真实文档
    刷出上百条把有效信号淹掉。
    """
    findings: list[Finding] = []
    for part in pkg.parts(scope="content"):
        tree = part.read_tree
        for index, paragraph in enumerate(tree.iter(qn("w:p"))):
            xpath = tree.getroottree().getpath(paragraph)
            para_props = find_first(paragraph, "w:pPr")
            if para_props is not None:
                direct = _direct_children(para_props, _STRUCTURAL_PPR | _PAGINATION_PPR, paragraph)
                if direct:
                    findings.append(_direct_finding(part.path, xpath, index, "段落属性", direct))
                mark_props = find_first(para_props, "w:rPr")
                if mark_props is not None:
                    direct_rpr = _direct_children(mark_props, _STRUCTURAL_RPR, paragraph)
                    if direct_rpr:
                        findings.append(
                            _direct_finding(part.path, xpath, index, "段落标记字符属性", direct_rpr)
                        )
            for run in find_all(paragraph, "w:r"):
                run_props = find_first(run, "w:rPr")
                if run_props is None:
                    continue
                direct_run = _direct_children(run_props, _STRUCTURAL_RPR, paragraph)
                if direct_run:
                    findings.append(
                        _direct_finding(part.path, xpath, index, "文本运行字符属性", direct_run)
                    )
                    break
    return findings


def _direct_children(properties: Element, allowed: frozenset[str], paragraph: Element) -> list[str]:
    return [
        localname(child)
        for child in properties
        if localname(child) not in allowed and not _is_cell_indent_normalization(child, paragraph)
    ]


def _is_cell_indent_normalization(element: Element, paragraph: Element) -> bool:
    """``w:ind`` 是否属于**渲染层的单元格版式归一**（FR-M4-08）。

    ``render.tables.fix_cell_layout`` 把单元格段落的继承缩进清零
    （``w:ind/@w:left="0"``、``@w:firstLine="0"``，并摘掉 ``@w:hanging``）。
    那是引擎**声明过**的版式操作（DD-9.4），不是"没下沉到样式的残留"。把它计入
    直设格式，会让每一份带注入表格的产物都刷出 blocker，而报告指向的是引擎自己
    的正确行为——这种"总在误报"的规则等于不存在。

    判据刻意收紧到两条**同时**成立：段落位于表格单元格内，且 ``w:ind`` 的每个
    已设属性都是零值。正文段落上的零值缩进不在此列——那里它照样表达"覆盖样式
    给出的首行缩进"，是真实偏离；单元格里的非零缩进同样照报。
    """
    if localname(element) != _INDENT_LOCAL:
        return False
    if not any(localname(node) == _CELL_LOCAL for node in paragraph.iterancestors()):
        return False
    # 一个属性都没有的 w:ind 什么也没覆盖，不构成直设格式。
    return all(_is_zero_value(value) for value in element.attrib.values())


def _is_zero_value(value: str) -> bool:
    """属性值是否为数值零（``"0"`` / ``"0.0"``）；非数值一律判非零。"""
    try:
        return float(value) == 0.0
    except ValueError:
        return False


def _direct_finding(
    part: str, xpath: str, paragraph_index: int, scope_name: str, properties: list[str]
) -> Finding:
    shown = "、".join(dict.fromkeys(properties))
    return Finding(
        rule_id="FR-M5-01",
        severity="major",
        message=f"段落第 {paragraph_index + 1} 个存在未通过样式的{scope_name}直设格式",
        location=Location(part=part, xpath=xpath),
        evidence=shown,
        suggestion="把该属性下沉到段落样式，或为这类段落新建样式；确认无误可在门禁中豁免本条",
    )


# ---------------------------------------------------------------- 引用校验
def collect_style_references(pkg: DocxPackage, *, include_definitions: bool = True) -> list[str]:
    """收集正文中引用到的 styleId（可选含样式定义间的 ``basedOn`` / ``next`` / ``link``）。"""
    referenced: list[str] = []
    for part in pkg.parts(scope="content"):
        for element in part.read_tree.iter():
            tag = localname(element)
            if tag in _STYLE_REF_TAGS:
                value = attr(element, "w:val")
                if value:
                    referenced.append(value)
    if include_definitions:
        root = pkg.part(STYLES_PART).read_tree
        for style in find_all(root, "w:style"):
            for pointer in _DEFINITION_POINTERS:
                value = _val(find_first(style, f"w:{pointer}"))
                if value:
                    referenced.append(value)
    return referenced


def validate_style_refs(
    pkg: DocxPackage, styles: Sequence[StyleContract] | None = None
) -> list[str]:
    """返回"被引用但未定义"的 styleId（去重升序）。

    这是样式保真最基本的完整性断言：命中非空即意味着产物在 Word 中会静默回退样式。
    """
    defined = {style.style_id for style in (styles or read_styles(pkg))}
    missing = {ref for ref in collect_style_references(pkg) if ref not in defined}
    return sorted(missing)


# ---------------------------------------------------------------- ID 重映射
def remap_style_ids(
    pkg: DocxPackage,
    mapping: Mapping[tuple[str, str], str],
    *,
    styles: Sequence[StyleContract] | None = None,
    scope_parts: Iterable[str] | None = None,
) -> RemapReport:
    """按 ``(样式名, 样式类型) -> 目标 styleId`` 重映射全部引用（DD-4.2.1）。

    处理面必须同时覆盖四类：正文部件中的 ``w:pStyle``、``w:tblStyle``，
    样式定义内部的 ``w:basedOn`` / ``w:next`` / ``w:link``。
    ``scope_parts`` 用于限定重映射的部件范围（默认只动正文部件 + 样式表）。
    """
    report = RemapReport()
    known = list(styles) if styles is not None else read_styles(pkg)
    by_name_type: dict[tuple[str, str], str] = {
        (style.name, style.type): style.style_id for style in known
    }
    for key, target in mapping.items():
        source_id = by_name_type.get(key)
        if source_id is None:
            report.unresolved_mappings.append(key)
            continue
        report.resolved_styles[source_id] = target

    if not report.resolved_styles:
        if mapping:
            report.warnings.append("样式映射全部未命中目标模板中的样式，未做任何替换")
        return report

    wanted = set(scope_parts) if scope_parts is not None else None
    for part in pkg.parts(scope="content"):
        if wanted is not None and part.path not in wanted:
            continue
        hits = _remap_refs_in_tree(part.tree, report.resolved_styles)
        if hits[0] or hits[1]:
            report.p_style_hits += hits[0]
            report.tbl_style_hits += hits[1]
            report.parts_touched.append(part.path)

    if wanted is None or STYLES_PART in wanted:
        report.definition_pointers_updated = _remap_definition_pointers(pkg, report.resolved_styles)

    report.missing_refs = validate_style_refs(pkg, known)

    declares_table_mapping = any(
        style_type in {"table", "linked"}
        and by_name_type.get((name, style_type)) in report.resolved_styles
        for name, style_type in mapping
    )
    if declares_table_mapping and report.tbl_style_hits == 0:
        report.warnings.append(
            "表格样式映射命中 0 次：目标模板中不存在被引用的表格样式，"
            "或本包正文里确实没有表格。请先确认前者——只验证段落样式的重映射会假通过。"
        )
    return report


def _remap_refs_in_tree(tree: Element, resolved: Mapping[str, str]) -> tuple[int, int]:
    """替换树中的 ``pStyle`` / ``tblStyle`` 引用，返回两类命中数。"""
    p_hits = 0
    tbl_hits = 0
    for element in tree.iter():
        tag = localname(element)
        bucket = _STYLE_REF_TAGS.get(tag)
        if bucket is None:
            continue
        current = attr(element, "w:val")
        if not current:
            continue
        replacement = resolved.get(current)
        if replacement is None or replacement == current:
            continue
        set_attr(element, "w:val", replacement)
        if bucket == "tbl_style":
            tbl_hits += 1
        else:
            p_hits += 1
    return p_hits, tbl_hits


def _remap_definition_pointers(pkg: DocxPackage, resolved: Mapping[str, str]) -> int:
    """同步样式定义之间的指针（``basedOn`` / ``next`` / ``link``）。"""
    root = pkg.part(STYLES_PART).tree
    updated = 0
    for style in find_all(root, "w:style"):
        for pointer in _DEFINITION_POINTERS:
            element = find_first(style, f"w:{pointer}")
            if element is None:
                continue
            current = attr(element, "w:val")
            if not current:
                continue
            replacement = resolved.get(current)
            if replacement is None or replacement == current:
                continue
            set_attr(element, "w:val", replacement)
            updated += 1
    return updated


def clone_style_if_missing(pkg: DocxPackage, style: StyleContract) -> CloneResult:
    """把外部样式并入本包，**复用优先于新建**（1.4 原则）。

    判定顺序（三种"已存在"要区分开）：

    1. **同 id 且同名同类型** —— 真重复，直接复用；
    2. **同 id 但不同名 / 不同型** —— 该 id 被别的样式占着，必须换 id 新建，
       否则会把对方覆盖掉，正文里所有引用它的段落会集体变样；
    3. **同名同类型但不同 id** —— 同一份体例在不同模板里 styleId 常常不同
       （``Heading1`` / ``1`` / ``标题 1``），显示名才是稳定标识，复用现有的。

    新建时若 id 与既有样式冲突，分配 ``<id>_df<n>`` 并回报 ``renamed_from``。
    """
    if style.raw_xml is None:
        raise EngineError(
            ErrorCode.SCHEMA_INVALID,
            f"样式缺少原件 XML，无法克隆：{style.style_id}",
            where={"style_id": style.style_id},
            remediation=["重新解析模板以填充 raw_xml"],
        )

    index = style_element_index(pkg)
    same_id = index.get(style.style_id)
    if same_id is not None and _is_same_style(same_id, style):
        return CloneResult(
            style_id=style.style_id,
            created=False,
            reason="本包已存在同 styleId、同名、同类型的样式，复用",
        )
    for existing_id, element in index.items():
        if existing_id == style.style_id:
            continue
        if _is_same_style(element, style):
            return CloneResult(
                style_id=existing_id,
                created=False,
                reason=(
                    f"样式名与类型一致（{style.name} / {style.type}），" f"复用现有 {existing_id}"
                ),
            )

    new_id = style.style_id
    suffix = 1
    while new_id in index:
        new_id = f"{style.style_id}_df{suffix}"
        suffix += 1

    root = pkg.part(STYLES_PART).tree
    element = deep_copy(parse(style.raw_xml))
    set_attr(element, "w:styleId", new_id)
    root.append(element)

    dangling: list[str] = []
    for pointer in _DEFINITION_POINTERS:
        target_id = _val(find_first(element, f"w:{pointer}"))
        if target_id is not None and target_id not in index:
            dangling.append(f"{pointer}->{target_id}")
    reason = "本包无同 id 同名的同类型样式，已按原件新建"
    if new_id != style.style_id:
        reason = f"原 styleId {style.style_id} 已被其它样式占用，改用 {new_id} 新建"
    return CloneResult(
        style_id=new_id,
        created=True,
        reason=reason,
        renamed_from=style.style_id if new_id != style.style_id else None,
        dangling_pointers=dangling,
    )


def _is_same_style(element: Element, style: StyleContract) -> bool:
    """元素是否为 `style` 的等价样式：显示名与类型都相同。"""
    name = _val(find_first(element, "w:name")) or ""
    style_type = element.get(qn("w:type")) or "paragraph"
    return name == style.name and style_type == style.type


def align_style_definitions(
    target: DocxPackage,
    reference: DocxPackage | Sequence[StyleContract],
) -> AlignReport:
    """**保留 styleId**，仅同步样式定义内容（FR-M2-08）。

    同步的是定义、不是身份：正文里已有的 ``w:pStyle`` 引用一律不动，因此
    "换样式定义"不会波及内容层。反过来若连 styleId 一起换，正文引用会集体失效。
    """
    source_styles = (
        read_styles(reference) if isinstance(reference, DocxPackage) else list(reference)
    )
    index = style_element_index(target)
    report = AlignReport()
    for style in source_styles:
        element = index.get(style.style_id)
        if element is None:
            report.missing_in_target.append(style.style_id)
            continue
        if style.raw_xml is None:
            report.unchanged.append(style.style_id)
            continue
        source_element = parse(style.raw_xml)
        if _children_signature(element) == _children_signature(source_element):
            report.unchanged.append(style.style_id)
            continue
        for child in list(element):
            element.remove(child)
        for child in source_element:
            element.append(deep_copy(child))
        report.updated.append(style.style_id)
    if report.updated:
        target.part(STYLES_PART).mark_dirty()
    return report


def _children_signature(element: Element) -> str:
    return "|".join(serialize_text(child) for child in element)


# ---------------------------------------------------------------- 编排入口
def adopt_styles(
    pkg: DocxPackage,
    source_styles: Sequence[StyleContract],
    mapping: Mapping[tuple[str, str], str],
) -> AdoptReport:
    """把外部样式体系并入本包并按映射改写引用。

    **顺序不可颠倒**：先补齐样式定义（克隆），再改写引用（重映射），最后校验。
    反过来会出现"引用已改、定义还没建"的中间态，一旦中途失败就会留下一个
    每个样式都指向不存在的 styleId 的产物——它仍能被 Word 打开，只是样式全丢。
    """
    report = AdoptReport()
    realized: dict[str, str] = {}
    for style in source_styles:
        clone = clone_style_if_missing(pkg, style)
        report.clones.append(clone)
        realized[style.style_id] = clone.style_id

    remap: dict[tuple[str, str], str] = {}
    for key, target_id in mapping.items():
        remap[key] = realized.get(target_id, target_id)
    report.remap = remap_style_ids(pkg, remap, styles=read_styles(pkg))
    return report


def style_ids_by_name(pkg: DocxPackage) -> dict[str, str]:
    """``样式名 -> styleId`` 便捷索引（供档案绑定与界面呈现）。"""
    return {style.name: style.style_id for style in read_styles(pkg)}


__all__ = [
    "AdoptReport",
    "AlignReport",
    "CloneResult",
    "RemapReport",
    "adopt_styles",
    "align_style_definitions",
    "clone_style_if_missing",
    "collect_style_references",
    "detect_direct_formatting",
    "read_styles",
    "remap_style_ids",
    "style_element_index",
    "style_ids_by_name",
    "used_styles",
    "used_styles_in_package",
    "validate_style_refs",
]
