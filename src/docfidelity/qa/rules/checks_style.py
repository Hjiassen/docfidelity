"""样式类规则：FR-M5-01 样式合规 / FR-M5-02 模板保真 / FR-M5-03 表头规范。

三条规则回答同一个问题的三个侧面——「产物有没有偷偷改动样式层」：

* **FR-M5-01**：样式**引用**是否干净（直设格式、悬挂 styleId、定义偏离契约）。
* **FR-M5-02**：样式**复用**是否干净（模板示例文字有没有漏进产物、段落数与预期是否一致）。
* **FR-M5-03**：**表格**表头的加粗与底纹是否合规。

为什么不合成一条：它们的**修复动作**完全不同——01 改档案/模板包，02 改裁剪区间或
注入逻辑，03 改表格原型。合并只会让报告说"样式有问题"，而使用者需要知道去动哪里。

"受保护 / 非受保护"的判据只有一处
----------------------------------
三条规则都要区分「前置版块（封面 / 修订记录 / 目录）」与「注入内容」。这里统一用
渲染上下文的 ``content_bounds()``——它的定义是"第一个骨架标题段落 → 尾节属性之前"，
与裁剪、注入用的是同一个区间。**不**用块索引里的区域元素去判：区域元素是单个
元素（封面块可能登记成某一个段落），按它排除会漏掉封面里的其余段落，于是
"封面的文字被判成模板示例残留"，而这类误报会让人把整条规则关掉。
"""

from __future__ import annotations

from typing import Any, Final

from docfidelity.contracts.qa import Finding
from docfidelity.contracts.template import StyleContract
from docfidelity.ooxml import (
    Element,
    attr,
    detect_direct_formatting,
    find_all,
    find_first,
    iter_elements,
    localname,
    normalized_element,
    paragraph_text,
    parse,
    read_styles,
    validate_style_refs,
)

from ..engine import QaContext, RuleSpec, register_check
from ._support import content_units, finding, render_view, tables_of

__all__ = ["check_header_style", "check_style_compliance", "check_template_fidelity"]

#: 判定"定义偏离契约"时比对的字段（DD-10.2：字体字号 + 结构性指针）。
_DEVIATION_FIELDS: Final[tuple[str, ...]] = (
    "type",
    "based_on",
    "font_east_asian",
    "size_pt",
    "num_id",
)

#: 单次判定最多列出的条目数（超出只报总数，避免一份真实文档刷出上百条）。
_MAX_LISTED: Final = 10

#: 表样式引用标签（**必须带命名空间前缀**：``ooxml.doc.qn`` 拒绝裸标签，
#: 且裸标签会被 Word 当作未知扩展丢弃）。
_TABLE_STYLE_REF: Final = "w:tblStyle"

#: 前置版块里的表格（模板自带，必须原样复用）。
_TABLE_LOCAL: Final = "tbl"


# ------------------------------------------------------------------ FR-M5-01
def check_style_compliance(spec: RuleSpec, context: QaContext) -> list[Finding]:
    """FR-M5-01：直设格式残留 + 悬挂 styleId + 样式定义偏离契约。"""
    findings: list[Finding] = list(detect_direct_formatting(context.pkg))

    missing = sorted(
        set(validate_style_refs(context.pkg))
        | set(validate_style_refs(context.pkg, context.template.styles))
    )
    if missing:
        findings.append(
            finding(
                spec,
                "blocker",
                f"产物引用了 {len(missing)} 个未定义的样式 ID，Word 会静默回退这些样式",
                evidence=", ".join(missing[:_MAX_LISTED]),
                suggestion="重新入库模板以补齐样式定义，或修正档案里引用的样式 ID",
            )
        )
    findings.extend(_definition_deviations(spec, context))
    legacy = [style.style_id for style in context.template.styles if style.raw_xml is None]
    if legacy:
        findings.append(
            finding(
                spec,
                "minor",
                f"{len(legacy)} 个旧契约样式缺少原始 XML，检查范围受限：仅比对已建模字段，未验证颜色、间距与边框",
                part="word/styles.xml",
                evidence=", ".join(legacy[:_MAX_LISTED]),
                suggestion="重新入库模板以补齐原始样式定义，再执行完整样式验证",
            )
        )
    return findings


def _definition_deviations(spec: RuleSpec, context: QaContext) -> list[Finding]:
    """产物里的样式定义与模板契约是否一致。

    有原始定义时比较归一化 XML 结构，覆盖颜色、间距、边框等未建模属性；
    缺原始定义的旧契约仍比较结构字段。属性顺序与易变修订标识不参与判等。
    """
    contract = {style.style_id: style for style in context.template.styles}
    deviations: list[Finding] = []
    actual_styles = read_styles(context.pkg)
    missing = sorted(set(contract) - {style.style_id for style in actual_styles})
    if missing:
        deviations.append(
            finding(
                spec,
                "major",
                f"产物缺少模板契约中的样式定义（{len(missing)} 个）",
                part="word/styles.xml",
                evidence=", ".join(missing[:_MAX_LISTED]),
                suggestion="检查样式复用步骤，恢复模板契约声明的全部样式定义",
            )
        )
    for actual in actual_styles:
        expected = contract.get(actual.style_id)
        if expected is None:
            continue
        changed = [
            name for name in _DEVIATION_FIELDS if getattr(expected, name) != getattr(actual, name)
        ]
        if (
            expected.raw_xml is not None
            and actual.raw_xml is not None
            and normalized_element(parse(expected.raw_xml))
            != normalized_element(parse(actual.raw_xml))
        ):
            changed.append("XML结构")
        if not changed:
            continue
        deviations.append(
            finding(
                spec,
                "major",
                f"样式 {actual.style_id} 的定义偏离模板契约（字段：{'、'.join(changed)}）",
                part="word/styles.xml",
                evidence=f"契约={_describe(expected)} vs 产物={_describe(actual)}",
                suggestion="检查样式对齐步骤是否漏掉了该样式，或模板契约是否需要重新入库",
            )
        )
        if len(deviations) >= _MAX_LISTED:
            break
    return deviations


def _describe(style: StyleContract) -> str:
    values = ", ".join(f"{name}={getattr(style, name)!r}" for name in _DEVIATION_FIELDS)
    return "{" + values + "}"


# ------------------------------------------------------------------ FR-M5-02
def check_template_fidelity(spec: RuleSpec, context: QaContext) -> list[Finding]:
    """FR-M5-02：模板示例内容不得残留；段落数与预期一致。

    判据只做一个方向：**产物里的段落文本必须来自内容 IR**。反方向的「IR 有而产物
    没有」归 FR-M5-09 / FR-M5-10——两个方向塞进一条规则会让报告说不清"是多了还是
    少了"，而这两件事的修复动作正好相反（多出来 → 裁剪区间错了；少掉 → 注入漏了）。
    """
    view = render_view(context)
    if view is None:
        return []
    expected, soft = _expected_texts(context)
    actual = _content_paragraph_texts(view)
    leaked = [text for text in actual if not _is_expected(text, expected, soft)]
    findings: list[Finding] = []
    if leaked:
        findings.append(
            finding(
                spec,
                "major",
                f"产物里有 {len(leaked)} 段文字不属于内容 IR（模板示例内容疑似残留）",
                part=context.document_part,
                evidence=" | ".join(leaked[:_MAX_LISTED]),
                suggestion="检查模板示例内容区是否被完整裁剪：多出来的文字几乎必然来自原型",
            )
        )
    if len(actual) < len(expected):
        findings.append(
            finding(
                spec,
                "minor",
                f"产物段落数少于内容 IR 预期（产物 {len(actual)} / 预期至少 {len(expected)}）",
                part=context.document_part,
                suggestion="检查注入步骤是否漏掉了部分单元",
            )
        )
    return findings


def _expected_texts(context: QaContext) -> tuple[set[str], set[str]]:
    """``(硬预期, 软预期)``。

    软预期用来容纳**渲染期补写的题注编号**：IR 的题注可能只有正文，渲染层按模板
    要求补上"表 2-1"前缀。要求逐字相等会把这类正常产物判成残留，故题注走
    "后缀命中即算"的宽松口径——宽松只施加在题注上，不施加在段落正文上。
    """
    hard: set[str] = set()
    soft: set[str] = set()
    for unit in content_units(context.ir):
        if unit.text:
            hard.add(unit.text.strip())
            hard.update(line.strip() for line in unit.text.splitlines() if line.strip())
        for key in ("items", "columns"):
            hard.update(_strings_of(unit.fields.get(key)))
        hard.update(_flat_rows(unit.fields.get("rows")))
        caption = unit.fields.get("caption")
        if isinstance(caption, str) and caption.strip():
            hard.add(caption.strip())
            soft.add(caption.strip())
    hard.discard("")
    return hard, soft


def _strings_of(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item.strip() for item in value if isinstance(item, str)]


def _flat_rows(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    flat: list[str] = []
    for row in value:
        flat.extend(_strings_of(row))
    return flat


def _is_expected(text: str, hard: set[str], soft: set[str]) -> bool:
    if text in hard:
        return True
    return any(text.endswith(item) for item in soft)


def _content_paragraph_texts(view: Any) -> list[str]:
    """注入内容区（骨架标题之后、尾节属性之前）的段落文本。

    区间内的**表格**要下钻取单元格段落：注入表格的文字同样是"内容"，只数 body 的
    直接子段落会把整张表的文字漏掉——于是"IR 里明明写了、产物里也确实有"的行被
    判成缺失（段落数对不上），而单元格里真正的模板残留文字又检不出来。两个方向
    各错一半，且都只在带表格的产物上出现。
    """
    start, end = view.content_bounds()
    children = list(view.body)
    texts: list[str] = []
    for child in children[start:end]:
        if localname(child) == "p":
            text = paragraph_text(child).strip()
            if text:
                texts.append(text)
        elif localname(child) == _TABLE_LOCAL:
            texts.extend(
                text
                for text in (paragraph_text(p).strip() for p in iter_elements(child, "w:p"))
                if text
            )
    return texts


# ------------------------------------------------------------------ FR-M5-03
def check_header_style(spec: RuleSpec, context: QaContext) -> list[Finding]:
    """FR-M5-03：表头加粗缺失数、数据行误加粗数、底纹残留数 == 0。

    **只判注入的表格**：模板自带的表格（修订记录等）属于前置版块，按模板保真的要求
    必须原样复用——去改它的表头加粗会同时违反 FR-M5-02。判据是"这张表是否落在
    内容区之前"，与上面两条用的是同一个区间定义。
    """
    from docfidelity.render.tables import residual_shading_count

    view = render_view(context)
    if view is None:
        return []
    front = _front_matter_tables(view)

    header_missing = 0
    data_bold = 0
    shading = 0
    samples: list[str] = []
    for part, table in tables_of(context.pkg):
        if any(table == item for item in front):
            continue
        rows = find_all(table, "w:tr")
        style_bold = _style_provides_header_bold(table, context)
        for index, row in enumerate(rows):
            for cell in find_all(row, "w:tc"):
                bold = _cell_is_bold(cell)
                if index == 0:
                    if not style_bold and not bold:
                        header_missing += 1
                        if len(samples) < _MAX_LISTED:
                            samples.append(f"{part}:{paragraph_text(cell).strip() or '<空>'}")
                elif bold:
                    data_bold += 1
        shading += residual_shading_count(table)

    findings: list[Finding] = []
    if header_missing:
        findings.append(
            finding(
                spec,
                "major",
                f"表头加粗缺失 {header_missing} 处（表样式未提供首行加粗，表头单元格也没有直设加粗）",
                evidence=", ".join(samples),
                suggestion="为该表格的原型样式声明 firstRow 加粗，或在表头单元格上显式加粗",
            )
        )
    if data_bold:
        findings.append(
            finding(
                spec,
                "major",
                f"数据行误加粗 {data_bold} 处（数据行不应带直设加粗）",
                suggestion="清除数据行单元格上的直设加粗，加粗只应出现在表头",
            )
        )
    if shading:
        findings.append(
            finding(
                spec,
                "major",
                f"表内残留直设底纹 {shading} 处（底纹应来自表样式的条件格式）",
                suggestion="清除单元格上的 w:shd，改由表样式 firstRow / band1Horz 提供底纹",
            )
        )
    return findings


def _front_matter_tables(view: Any) -> list[Element]:
    start, _end = view.content_bounds()
    return [child for child in list(view.body)[:start] if localname(child) == _TABLE_LOCAL]


def _style_provides_header_bold(table: Element, context: QaContext) -> bool:
    """该表的表样式是否用 ``firstRow`` 条件格式提供加粗。"""
    root = find_first(table, "w:tblPr")
    style_id = attr(find_first(root, _TABLE_STYLE_REF), "w:val")
    if not style_id:
        return False
    contract = context.template.style_by_id(style_id)
    if contract is None:
        return False
    return any(item.type == "firstRow" and item.bold for item in contract.tbl_style_pr)


def _cell_is_bold(cell: Element) -> bool:
    """单元格里是否出现了直设加粗（``w:b`` 且未被显式关闭）。

    只看单元格内**第一个**带 ``w:b`` 的运行：一格被手工加粗通常就是一处，
    逐运行统计会把一处加粗数成 3 处（段里每个 run 各一条）。
    """
    for paragraph in find_all(cell, "w:p"):
        for run in find_all(paragraph, "w:r"):
            bold = find_first(find_first(run, "w:rPr"), "w:b")
            if bold is None:
                continue
            return attr(bold, "w:val") not in {"0", "false"}
    return False


register_check("style_compliance", check_style_compliance)
register_check("template_fidelity", check_template_fidelity)
register_check("header_style", check_header_style)
