"""表格注入与规范化（DD-9.4 / FR-M6-05 / FR-M5-03 / FR-M4-08）。

表格是最容易"看起来对、实际全错"的一类内容：列宽差 3 twips、表头掉色、
数据行继承表头加粗、单元格留着模板的缩进——这些在屏幕上几乎看不出来，
在导 PDF 或换字号时集体暴露。故 DD-9.4 的五个步骤全在这一个模块里。

两条硬规则
----------
1. **行模板取同表自己的 ``w:tr``**（FR-M6-05）。从别的表克隆 ``tr`` 会让新行的
   单元格数与 ``w:tblGrid`` 对不上，Word 会按自己的规则重新分配列宽——于是
   "程序算的列宽"与"Word 渲染的列宽"永远不一致。
2. **表头加粗不写直设格式**。DD-9.4 写的是"表头单元格加粗"，但同一套门禁里的
   FR-M5-01（样式合规）把直设格式判为 blocker。二者的唯一自洽解释是：加粗由
   表样式的 ``firstRow`` 条件格式提供，渲染层只负责把 ``w:tblLook`` 的
   ``firstRow`` 打开、并给首行挂 ``w:tblHeader``。模板没有声明 ``firstRow``
   条件格式时报告一条 FR-M5-03 ——那是模板能力不足，不是引擎该偷偷补的。

``w:tblPr`` / ``w:trPr`` / ``w:tcPr`` 的子元素顺序是规范约束：新增子元素一律走
``ensure_child_in_order``。顺序错的产物 Word 可能能打开，但"跨页表头不重复"
这类症状完全指不到"顺序"上。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Final

from docfidelity.contracts.errors import EngineError
from docfidelity.contracts.ir import ContentUnit, TableData
from docfidelity.ooxml import (
    Element,
    attr,
    deep_copy,
    drop_attr,
    ensure_child_in_order,
    ensure_leading_child,
    ensure_paragraph_properties,
    find_all,
    find_first,
    localname,
    normalize_table_width,
    qn,
    set_attr,
    set_on_off,
    set_paragraph_text,
    sub,
)

from .context import (
    ROW_TEMPLATE_RULE_ID,
    TABLE_HEADER_RULE_ID,
    TABLE_WIDTH_RULE_ID,
    TEMPLATE_FIDELITY_RULE_ID,
    RenderContext,
)
from .order import (
    FALSE_ATTR,
    P_PR_ORDER,
    TBL_ORDER,
    TBL_PR_ORDER,
    TC_MAR_ORDER,
    TC_PR_ORDER,
    TR_ORDER,
    TR_PR_ORDER,
    TRUE_ATTR,
)

__all__ = [
    "DEFAULT_CELL_MARGIN_TWIPS",
    "FIRST_ROW_CONDITION",
    "TABLE_CAPTION_POSITIONS",
    "TablePolicy",
    "TableReport",
    "build_table",
    "cell_margin_of",
    "clear_residual_shading",
    "fix_cell_layout",
    "normalize_injected_tables",
    "normalize_table",
    "residual_shading_count",
    "table_data_of",
    "table_grid",
]

#: 单元格默认内边距（twips）。DD-9.4 说中文文档常用 0 或 108；
#: 默认取 0 并做成策略参数——"要不要留白"是体例决定，不该由引擎猜。
DEFAULT_CELL_MARGIN_TWIPS: Final = 0

#: 首行条件格式的类型名（``w:tblStylePr/@w:type``）。
FIRST_ROW_CONDITION: Final = "firstRow"

#: 表注位置：中文文档惯例是"表题在表上方、图题在图下方"（GB/T 7713）。
#: 做成策略参数而不是写死：惯例是体例，不是引擎不变量。
TABLE_CAPTION_POSITIONS: Final[tuple[str, ...]] = ("above", "below")

#: ``w:tblW`` / ``w:tcW`` / ``w:tcMar`` 的度量单位：二十分之一磅。
DXA: Final = "dxa"

#: 四个内边距方向（``w:tcMar`` 的子元素）与它们在规范序里的名字一致。
_CELL_MARGIN_SIDES: Final[tuple[str, ...]] = ("top", "left", "bottom", "right")

_RUN_PROPERTIES: Final = qn("w:rPr")
_SHADING_LOCAL: Final = "shd"


@dataclass(frozen=True)
class TablePolicy:
    """表格规范化策略。DD-9.4 的五步各自可关，关掉哪一项都是显式决定。"""

    cell_margin_twips: int = DEFAULT_CELL_MARGIN_TWIPS
    zero_cell_indent: bool = True
    clear_shading: bool = True
    repeat_header_row: bool = True
    normalize_width: bool = True
    caption_position: str = "above"


@dataclass
class TableReport:
    """一张表的规范化结果（进 ``StepResult.checkpoint_outputs`` 的就是这些数字）。"""

    unit_id: str = ""
    columns: int = 0
    data_rows: int = 0
    widths: list[int] = field(default_factory=list)
    shading_cleared: int = 0
    paragraphs_fixed: int = 0
    header_repeated: bool = False
    normalized: bool = False

    def as_payload(self) -> dict[str, Any]:
        return {
            "unit_id": self.unit_id,
            "columns": self.columns,
            "data_rows": self.data_rows,
            "widths": list(self.widths),
            "shading_cleared": self.shading_cleared,
            "paragraphs_fixed": self.paragraphs_fixed,
            "header_repeated": self.header_repeated,
            "normalized": self.normalized,
        }


def table_data_of(unit: ContentUnit) -> TableData:
    """把单元的 ``fields`` 读成 :class:`TableData`。

    **显式逐字段读，不用 ``model_validate``**：``FormatModel`` 是 ``extra="forbid"``
    的，而 ``ContentUnit.fields`` 的契约是"体例声明的字段"（允许附加键）。
    用严格校验读，会让一个无关的自定义键把整张表判成不合法。
    """
    fields = unit.fields
    return TableData(
        columns=[str(item) for item in _as_list(fields.get("columns"))],
        rows=[[str(cell) for cell in _as_list(row)] for row in _as_list(fields.get("rows"))],
        caption=_as_text(fields.get("caption")),
        caption_slot=_as_slot(fields.get("caption_slot")),
    )


def _as_list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list | tuple) else []


def _as_text(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _as_slot(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


# --------------------------------------------------------------------- 构建
def build_table(ctx: RenderContext, unit: ContentUnit) -> Element | None:
    """按原型建出一张表（**只造结构，不做规范化**）；造不出来返回 ``None``。

    没有原型时不"自己拼一个 ``w:tbl``"：``w:tblPr`` 里任何一个属性的默认值都
    可能与模板体例冲突，拼出来的表在 Word 里就是另一个样子。返回 ``None`` 让
    调用方跳过该单元并留下一条判定，比造一张"看起来能开"的表诚实得多。

    规范化刻意不在这里做——DD-9.1 把"表格规范化"列为**独立可调**的第 7 步，
    建表属第 6 步（章节注入）。两件事混在一起，第 7 步就永远没有自己的产物。
    """
    data = table_data_of(unit)
    columns = data.columns or [""]
    prototype = ctx.prototypes.table_clone()
    if prototype is None:
        ctx.note_once(
            TEMPLATE_FIDELITY_RULE_ID,
            "major",
            "no-table-prototype",
            "模板里没有任何表格，无法为表格单元复用行原型；该表已被跳过",
            unit=unit,
            suggestion="在模板中放一张示例表（表头行 + 至少一行数据），或从档案中移除表格投影",
        )
        return None

    row_prototypes = find_all(prototype, "w:tr")
    if not row_prototypes:
        ctx.note_once(
            ROW_TEMPLATE_RULE_ID,
            "major",
            "no-row-prototype",
            "模板的表格原型里没有 w:tr，取不到行模板（禁止跨表克隆 tr）",
            unit=unit,
            suggestion="在模板表格里保留至少一行，作为注入行的原型",
        )
        return None

    header_prototype = row_prototypes[0]
    data_prototype = row_prototypes[1] if len(row_prototypes) > 1 else row_prototypes[0]
    for row in list(row_prototypes):
        prototype.remove(row)

    prototype.append(_fill_row(header_prototype, columns, header=True))
    for values in data.rows:
        prototype.append(_fill_row(data_prototype, _pad(values, len(columns)), header=False))
    return prototype


def normalize_injected_tables(ctx: RenderContext, *, policy: TablePolicy) -> list[TableReport]:
    """DD-9.1 第 7 步：对注入区内的每张表做五步规范化。

    幂等——同一张表规范化两次结果相同。这让"注入时已顺手建好的表"与
    "``block_ref`` 复用进来、从未规范化过的表"走同一条路径，不必分两种处理。
    """
    reports: list[TableReport] = []
    for unit_id, table in ctx.injected_tables:
        report = normalize_table(ctx, table, columns=_column_count(table), policy=policy)
        report.unit_id = unit_id
        reports.append(report)
    return reports


def _column_count(table: Element) -> int:
    """列数 = 首行单元格数（表头行的单元格数就是列数，这是唯一可信来源）。"""
    rows = find_all(table, "w:tr")
    if not rows:
        return max(1, len(table_grid(table)))
    return max(1, len(find_all(rows[0], "w:tc")))


def _pad(values: list[str], width: int) -> list[str]:
    """把一行补齐 / 截到列数。缺列补空串，多列截断。

    不补不截的后果是行宽与表头不一致，Word 会按第一行重排——视觉上像"列错位"。
    """
    if len(values) >= width:
        return values[:width]
    return [*values, *([""] * (width - len(values)))]


def _fill_row(row_prototype: Element, values: list[str], *, header: bool) -> Element:
    """用**本表自己的行原型**造一行。

    数据行一律剥掉直设加粗（``_strip_direct_bold``）：原型行若是表头行就带着
    ``w:b``，不剥会让"数据行不得继承加粗"当场失效，而屏幕上只是"整张表看着都挺粗"。

    ``w:tblHeader`` **不在这里设**：跨页重复表头是 ``TablePolicy.repeat_header_row``
    管的（第 7 步 ``_apply_header``）。两处都设会让"关掉重复表头"这个开关失效，
    而那种"开关没反应"的缺陷最难定位。
    """
    row = deep_copy(row_prototype)
    cell_prototypes = find_all(row, "w:tc")
    for cell in list(find_all(row, "w:tc")):
        row.remove(cell)
    if not cell_prototypes:
        return row

    for index, value in enumerate(values):
        source = cell_prototypes[min(index, len(cell_prototypes) - 1)]
        cell = deep_copy(source)
        if not header:
            _strip_direct_bold(cell)
        _set_cell_text(cell, value)
        row.append(cell)
    return row


def _strip_direct_bold(cell: Element) -> int:
    """清掉单元格里运行的直设加粗（``w:rPr/w:b``），返回清掉几处。

    数据行从原型行克隆而来，若原型是表头行，它就带着 ``w:b``——于是"数据行不得
    继承加粗"当场失效，而屏幕上只是"整张表看着都挺粗"。加粗应当来自表样式的
    条件格式，故直设的那一份必须去掉。
    """
    removed = 0
    for properties in cell.iter(_RUN_PROPERTIES):
        bold = find_first(properties, "w:b")
        if bold is not None:
            properties.remove(bold)
            removed += 1
    return removed


def _set_cell_text(cell: Element, value: str) -> None:
    """把单元格内容换成单段单运行；保留 ``w:pPr``，删掉多余段落。"""
    paragraphs = find_all(cell, "w:p")
    if not paragraphs:
        paragraphs = [sub(cell, "w:p")]
    set_paragraph_text(paragraphs[0], value)
    for extra in paragraphs[1:]:
        cell.remove(extra)


# ----------------------------------------------------------------- 规范化
def normalize_table(
    ctx: RenderContext,
    table: Element,
    *,
    columns: int,
    policy: TablePolicy,
) -> TableReport:
    """按 DD-9.4 的五步规范化一张表。"""
    report = TableReport(columns=columns)
    report.data_rows = max(0, len(find_all(table, "w:tr")) - 1)

    if policy.normalize_width:
        report.widths, report.normalized = _apply_widths(ctx, table, columns=columns)
    if policy.clear_shading:
        report.shading_cleared = clear_residual_shading(table)
    report.header_repeated = _apply_header(ctx, table, policy=policy)
    for cell in _cells_of(table):
        report.paragraphs_fixed += fix_cell_layout(cell, policy=policy)
    return report


def _cells_of(table: Element) -> list[Element]:
    return [cell for row in find_all(table, "w:tr") for cell in find_all(row, "w:tc")]


def _apply_widths(ctx: RenderContext, table: Element, *, columns: int) -> tuple[list[int], bool]:
    """列宽归一 + 回写 ``w:tblGrid`` / ``w:tblW`` / 每格 ``w:tcW``。"""
    grid = table_grid(table)
    if len(grid) != columns:
        grid = [0] * max(1, columns)
    try:
        widths = normalize_table_width(grid, ctx.usable_width_twips())
    except EngineError as error:
        ctx.note(
            TABLE_WIDTH_RULE_ID,
            "blocker",
            f"表格列宽无法归一到版心：{error.message}",
            part=ctx.document_part,
            element=table,
            evidence=f"columns={columns}, usable_width={ctx.usable_width_twips()}",
            suggestion="减少列数、把该表放入横向分节，或调小页边距",
        )
        return grid, False

    _write_grid(table, widths)
    table_width = ensure_child_in_order(
        ensure_leading_child(table, "w:tblPr"), "w:tblW", TBL_PR_ORDER
    )
    set_attr(table_width, "w:w", str(sum(widths)))
    set_attr(table_width, "w:type", DXA)

    for row in find_all(table, "w:tr"):
        for index, cell in enumerate(find_all(row, "w:tc")):
            if index >= len(widths):
                break
            cell_width = ensure_child_in_order(
                ensure_leading_child(cell, "w:tcPr"), "w:tcW", TC_PR_ORDER
            )
            set_attr(cell_width, "w:w", str(widths[index]))
            set_attr(cell_width, "w:type", DXA)
    return widths, True


def table_grid(table: Element) -> list[int]:
    """读 ``w:tblGrid`` 的列宽（twips）。缺格或读不到值按 0 记。"""
    grid_element = find_first(table, "w:tblGrid")
    if grid_element is None:
        return []
    return [_int_of(attr(column, "w:w")) for column in find_all(grid_element, "w:gridCol")]


def _write_grid(table: Element, widths: list[int]) -> None:
    """回写 ``w:tblGrid``：缺格补、多格删、逐格写宽度。"""
    grid_element = ensure_child_in_order(table, "w:tblGrid", TBL_ORDER)
    columns = find_all(grid_element, "w:gridCol")
    while len(columns) < len(widths):
        columns.append(sub(grid_element, "w:gridCol"))
    for extra in columns[len(widths) :]:
        grid_element.remove(extra)
    for column, width in zip(columns[: len(widths)], widths, strict=True):
        set_attr(column, "w:w", str(width))


def _int_of(raw: str | None) -> int:
    try:
        return int(raw or 0)
    except ValueError:
        return 0


def _apply_header(ctx: RenderContext, table: Element, *, policy: TablePolicy) -> bool:
    """表头：``w:tblLook/@w:firstRow`` + 首行 ``w:tblHeader``。

    ``w:tblHeader`` 是 Word 里"跨页重复表头"的**唯一**正确手段（复制一行表头不是：
    复制出来的行在编辑时会与真表头不一致）。它同时让 Word 对首行套用表样式的
    ``firstRow`` 条件格式，于是"表头加粗"由样式提供，不需要直设格式。
    """
    rows = find_all(table, "w:tr")
    if not rows:
        return False
    if policy.repeat_header_row:
        set_on_off(
            ensure_child_in_order(rows[0], "w:trPr", TR_ORDER),
            "w:tblHeader",
            True,
            order=TR_PR_ORDER,
        )

    table_properties = ensure_leading_child(table, "w:tblPr")
    look = ensure_child_in_order(table_properties, "w:tblLook", TBL_PR_ORDER)
    set_attr(look, "w:firstRow", TRUE_ATTR)
    set_attr(look, "w:lastRow", FALSE_ATTR)
    set_attr(look, "w:firstColumn", FALSE_ATTR)
    set_attr(look, "w:lastColumn", FALSE_ATTR)

    if not _has_first_row_condition(ctx, table):
        ctx.note_once(
            TABLE_HEADER_RULE_ID,
            "minor",
            "no-first-row-condition",
            "表样式未声明 firstRow 条件格式，表头将不会加粗"
            "（引擎不补直设格式：直设加粗会被样式合规规则判为违规）",
            part=ctx.document_part,
            element=table,
            suggestion="在模板的表样式里补 firstRow 条件格式（加粗 + 底纹）",
        )
    return policy.repeat_header_row


def _has_first_row_condition(ctx: RenderContext, table: Element) -> bool:
    """表样式是否声明了 ``firstRow`` 条件格式。"""
    style_id = attr(find_first(find_first(table, "w:tblPr"), "w:tblStyle"), "w:val")
    if style_id is None:
        return False
    contract = ctx.find_style(style_id)
    if contract is None:
        return False
    return any(item.type == FIRST_ROW_CONDITION for item in contract.tbl_style_pr)


# ----------------------------------------------------------------- 五步明细
def clear_residual_shading(table: Element) -> int:
    """清除表内残留的直设底纹（``w:shd``），返回清除数（FR-M5-03 要求为 0）。

    表头底色应当来自表样式的 ``firstRow`` 条件格式。直接写在单元格上的 ``w:shd``
    是模板作者手工刷上去的，它既不随体例变，也会在换模板时留下来。
    """
    removed = 0
    for element in list(table.iter()):
        if element is table or localname(element) != _SHADING_LOCAL:
            continue
        parent = element.getparent()
        if parent is not None:
            parent.remove(element)
            removed += 1
    return removed


def residual_shading_count(table: Element) -> int:
    """表内残留直设底纹数（供门禁与测试断言使用）。"""
    return sum(
        1
        for element in table.iter()
        if element is not table and localname(element) == _SHADING_LOCAL
    )


def fix_cell_layout(cell: Element, *, policy: TablePolicy) -> int:
    """固定单元格版式：``w:tcMar`` 归零 + 清零继承缩进（FR-M4-08）。

    返回改动过的段落数——这是这一步唯一可断言的量：产物表面看不出差别，
    但换字号时缩进会整片跑偏，而"跑偏"从来不指向某个单元格。
    """
    cell_properties = ensure_leading_child(cell, "w:tcPr")
    margins = ensure_child_in_order(cell_properties, "w:tcMar", TC_PR_ORDER)
    for side in _CELL_MARGIN_SIDES:
        margin = ensure_child_in_order(margins, f"w:{side}", TC_MAR_ORDER)
        set_attr(margin, "w:w", str(policy.cell_margin_twips))
        set_attr(margin, "w:type", DXA)

    if not policy.zero_cell_indent:
        return 0
    touched = 0
    for paragraph in find_all(cell, "w:p"):
        indentation = ensure_child_in_order(
            ensure_paragraph_properties(paragraph), "w:ind", P_PR_ORDER
        )
        set_attr(indentation, "w:left", "0")
        set_attr(indentation, "w:firstLine", "0")
        drop_attr(indentation, "w:hanging")
        touched += 1
    return touched


def cell_margin_of(cell: Element) -> int | None:
    """读单元格 ``w:tcMar/@w:left/@w:w``（诊断与测试用）。"""
    margins = find_first(find_first(cell, "w:tcPr"), "w:tcMar")
    return None if margins is None else _int_of(attr(find_first(margins, "w:left"), "w:w"))
