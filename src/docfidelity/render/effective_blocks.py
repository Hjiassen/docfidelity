"""冻结原型到 OOXML 内容块；不采集文档样式或原型。"""

from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import Final

from docfidelity.contracts.errors import EngineError, ErrorCode
from docfidelity.contracts.imports import FrozenPrototype, PrototypeKind, TemplateVersion
from docfidelity.contracts.ir import ContentUnit, FigureData, TableData
from docfidelity.ooxml.doc import (
    Element,
    deep_copy,
    ensure_child_in_order,
    ensure_paragraph_properties,
    find_first,
    new_tree,
    parse,
    qn,
    set_on_off,
    sub,
    xpath,
)
from docfidelity.ooxml.geometry import normalize_table_width, pixels_to_emu, twips_to_emu
from docfidelity.ooxml.package import DocxPackage
from docfidelity.ooxml.pictures import place_picture

from .effective_numbering import create_list_numbering
from .order import P_PR_ORDER, TBL_PR_ORDER, TC_PR_ORDER, TR_PR_ORDER
from .variables import fill_paragraph


class FrozenBlocks:
    """只读取绑定指向的 sidecar，生成单元并采用插入位置所属节的尺寸。"""

    def __init__(
        self,
        directory: Path,
        version: TemplateVersion,
        package: DocxPackage,
        insertion: Element,
        assets: Path,
    ) -> None:
        self.directory = directory
        self.version = version
        self.package = package
        self.insertion = insertion
        self.assets = assets
        self.prototypes = {p.prototype_id: p for p in version.prototypes}
        if len(self.prototypes) != len(version.prototypes):
            raise EngineError(ErrorCode.PACKAGE_INCOMPLETE, "冻结原型标识重复")
        self.list_ids: dict[str, str] = {}

    def build(self, unit: ContentUnit) -> list[Element]:
        """分派封闭 IR 类型；不支持的能力明确失败。"""
        handler = _HANDLERS.get(unit.type)
        if handler is None:
            raise EngineError(ErrorCode.SCHEMA_INVALID, "不支持的内容单元类型")
        return handler(self, unit)

    def prototype(
        self, identifier: str | None, kind: PrototypeKind
    ) -> tuple[FrozenPrototype, Element]:
        """按冻结绑定加载原型，不在 DOCX 中寻找替代格式。"""
        prototype = self.prototypes.get(identifier or "")
        if prototype is None or prototype.kind != kind:
            raise EngineError(ErrorCode.SCHEMA_INVALID, f"缺少所需冻结原型：{kind}")
        element = parse((self.directory / prototype.path).read_bytes())
        if any(text.text for text in xpath(element, ".//w:t")):
            raise EngineError(ErrorCode.PACKAGE_INCOMPLETE, "冻结原型含非空旧文字")
        return prototype, element

    def paragraph(self, identifier: str | None, kind: PrototypeKind, text: str) -> Element:
        """复制空原型，填充新文字并保留直接格式。"""
        _, paragraph = self.prototype(identifier, kind)
        fill_paragraph(paragraph, text)
        return paragraph

    def body(self, text: str) -> Element:
        return self.paragraph(self.version.format_bindings.body, "body", text)

    def geometry(self) -> tuple[int, int]:
        """读取已冻结插入位置之后的节属性，禁止回读源文件。"""
        seen = False
        section: Element | None = None
        for element in self.insertion.getroottree().getroot().iter():
            if element is self.insertion:
                seen = True
            if seen and element.tag == qn("w:sectPr"):
                section = element
                break
        if section is None:
            raise EngineError(ErrorCode.PACKAGE_INCOMPLETE, "插入位置缺少冻结节属性")
        size, margins = find_first(section, "w:pgSz"), find_first(section, "w:pgMar")
        if size is None or margins is None:
            raise EngineError(ErrorCode.PACKAGE_INCOMPLETE, "冻结节缺少页尺寸或页边距")
        width = _integer(size, "w:w") - _integer(margins, "w:left") - _integer(margins, "w:right")
        height = _integer(size, "w:h") - _integer(margins, "w:top") - _integer(margins, "w:bottom")
        width -= int(margins.get(qn("w:gutter"), "0"))
        if width <= 0 or height <= 0:
            raise EngineError(ErrorCode.PACKAGE_INCOMPLETE, "冻结节可用尺寸非法")
        return width, height


def _integer(element: Element, key: str) -> int:
    value = element.get(qn(key))
    if value is None:
        raise EngineError(ErrorCode.PACKAGE_INCOMPLETE, "冻结几何属性缺失")
    return int(value)


def _heading(blocks: FrozenBlocks, unit: ContentUnit) -> list[Element]:
    identifier = blocks.version.format_bindings.heading.get(unit.level or 1)
    return [blocks.paragraph(identifier, "heading", unit.text or "")]


def _body(blocks: FrozenBlocks, unit: ContentUnit) -> list[Element]:
    return [blocks.body(unit.text or "")]


def _list(blocks: FrozenBlocks, unit: ContentUnit) -> list[Element]:
    raw = unit.fields.get("items")
    if raw is not None and (not isinstance(raw, list) or any(not isinstance(i, str) for i in raw)):
        raise EngineError(ErrorCode.SCHEMA_INVALID, "列表条目必须为文字列表")
    items = raw if isinstance(raw, list) else (unit.text or "").splitlines()
    items = [item for item in items if item.strip()] or [""]
    paragraphs = [
        blocks.paragraph(blocks.version.format_bindings.list_paragraph, "list", item)
        for item in items
    ]
    num_id = _list_numbering(blocks, unit.type, paragraphs[0])
    for paragraph in paragraphs:
        properties = ensure_paragraph_properties(paragraph)
        num = ensure_child_in_order(properties, "w:numPr", P_PR_ORDER)
        for child in list(num):
            num.remove(child)
        sub(num, "w:ilvl", {"w:val": "0"})
        sub(num, "w:numId", {"w:val": num_id})
    return paragraphs


def _list_numbering(blocks: FrozenBlocks, family: str, paragraph: Element) -> str:
    if family in blocks.list_ids:
        return blocks.list_ids[family]
    num_id = create_list_numbering(
        blocks.package,
        paragraph,
        family,
        neutral=(
            not blocks.version.selection.formats.numbering
            or not blocks.version.selection.formats.body
        ),
    )
    blocks.list_ids[family] = num_id
    return num_id


def _table(blocks: FrozenBlocks, unit: ContentUnit) -> list[Element]:
    fields = unit.fields
    data = TableData.model_validate(
        {key: fields[key] for key in TableData.model_fields if key in fields}
    )
    if data.caption_slot is not None:
        raise EngineError(ErrorCode.SCHEMA_INVALID, "冻结模板未声明题注编号协议")
    if not data.columns or any(len(row) != len(data.columns) for row in data.rows):
        raise EngineError(ErrorCode.SCHEMA_INVALID, "表格列数与数据行不一致")
    bindings = blocks.version.format_bindings.tables
    requested = fields.get("prototype_id")
    if requested is not None and (not isinstance(requested, str) or requested not in bindings):
        raise EngineError(ErrorCode.SCHEMA_INVALID, "表格引用未绑定冻结原型")
    identifier: str | None = requested if isinstance(requested, str) else None
    if identifier is None:
        for key in bindings:
            if key in blocks.prototypes and (
                blocks.prototypes[key].columns == len(data.columns)
                or blocks.prototypes[key].column_policy == "flexible"
            ):
                identifier = key
                break
    metadata, table = blocks.prototype(identifier, "table")
    flexible = metadata.column_policy == "flexible"
    if flexible and (
        blocks.version.selection.formats.tables or metadata.source_node_id is not None
    ):
        raise EngineError(ErrorCode.PACKAGE_INCOMPLETE, "弹性列策略必须来自明确选择的中性原型")
    if (not flexible and metadata.columns != len(data.columns)) or metadata.row_path is None:
        raise EngineError(ErrorCode.SCHEMA_INVALID, "表格列数与冻结原型不匹配")
    if table.tag != qn("w:tbl"):
        raise EngineError(ErrorCode.PACKAGE_INCOMPLETE, "冻结表格原型结构非法")
    rows = xpath(table, "./w:tr")
    row_prototype = parse((blocks.directory / metadata.row_path).read_bytes())
    grid = xpath(table, "./w:tblGrid/w:gridCol")
    physical_columns = 1 if flexible else metadata.columns
    if not rows or len(grid) != physical_columns or row_prototype.tag != qn("w:tr"):
        raise EngineError(ErrorCode.PACKAGE_INCOMPLETE, "冻结表格缺少同表行或列宽")
    for row in [*rows, row_prototype]:
        if len(xpath(row, "./w:tc")) != physical_columns or xpath(
            row, ".//w:gridSpan | .//w:vMerge"
        ):
            raise EngineError(ErrorCode.SCHEMA_INVALID, "当前表格不支持合并单元格或不规则行")
    original_widths = [_integer(column, "w:w") for column in grid]
    if any(width <= 0 for width in original_widths):
        raise EngineError(ErrorCode.PACKAGE_INCOMPLETE, "冻结表格列宽非法")
    available, _ = blocks.geometry()
    properties = find_first(table, "w:tblPr")
    if properties is None:
        raise EngineError(ErrorCode.PACKAGE_INCOMPLETE, "冻结表格缺少属性")
    indent = find_first(properties, "w:tblInd")
    if indent is not None:
        available -= max(0, _integer(indent, "w:w"))
    if flexible:
        widths = normalize_table_width(
            [1] * len(data.columns), min(sum(original_widths), available)
        )
        grid_container = find_first(table, "w:tblGrid")
        assert grid_container is not None
        for column in grid:
            grid_container.remove(column)
        grid = [sub(grid_container, "w:gridCol") for _ in widths]
    else:
        widths = (
            original_widths
            if sum(original_widths) <= available
            else normalize_table_width(original_widths, available)
        )
        # 几何助手的可读性下限可能增宽极窄列；冻结渲染须遵守逐列只缩不放。
        widths = [
            min(original, fitted) for original, fitted in zip(original_widths, widths, strict=True)
        ]
    for column, width in zip(grid, widths, strict=True):
        column.set(qn("w:w"), str(width))
    table_width = ensure_child_in_order(properties, "w:tblW", TBL_PR_ORDER)
    table_width.attrib.update({qn("w:w"): str(sum(widths)), qn("w:type"): "dxa"})
    for row in rows:
        table.remove(row)
    for index, values in enumerate([data.columns, *data.rows]):
        row = deep_copy(rows[0] if index == 0 else row_prototype)
        if flexible:
            # 新列只能复制该中性表自己的单元格，整表宽度以上面的冻结基准为上限。
            cell = xpath(row, "./w:tc")[0]
            for _ in range(len(data.columns) - 1):
                row.append(deep_copy(cell))
        row_properties = find_first(row, "w:trPr")
        if row_properties is None:
            row_properties = new_tree("w:trPr")
            row.insert(0, row_properties)
        set_on_off(row_properties, "w:tblHeader", index == 0, order=TR_PR_ORDER)
        for cell, value, width in zip(xpath(row, "./w:tc"), values, widths, strict=True):
            paragraphs = xpath(cell, "./w:p")
            if not paragraphs or xpath(cell, "./w:tbl | ./w:sdt"):
                raise EngineError(ErrorCode.SCHEMA_INVALID, "冻结行单元格不是简单文字容器")
            for paragraph in paragraphs:
                fill_paragraph(paragraph, value if paragraph is paragraphs[0] else "")
            cell_properties = find_first(cell, "w:tcPr")
            if cell_properties is None:
                cell_properties = new_tree("w:tcPr")
                cell.insert(0, cell_properties)
            cell_width = ensure_child_in_order(cell_properties, "w:tcW", TC_PR_ORDER)
            cell_width.attrib.update({qn("w:w"): str(width), qn("w:type"): "dxa"})
        table.append(row)
    return ([blocks.body(data.caption)] if data.caption else []) + [table]


def _image(blocks: FrozenBlocks, unit: ContentUnit) -> list[Element]:
    data = FigureData.model_validate(
        {key: unit.fields[key] for key in FigureData.model_fields if key in unit.fields}
    )
    if data.caption_slot is not None:
        raise EngineError(ErrorCode.SCHEMA_INVALID, "冻结模板未声明题注编号协议")
    relative = PurePosixPath(data.image_ref)
    if (
        not data.image_ref
        or relative.is_absolute()
        or ".." in relative.parts
        or "\\" in data.image_ref
        or ":" in data.image_ref
    ):
        raise EngineError(ErrorCode.PATH_ESCAPE, "图片逻辑名必须为资源目录内的相对路径")
    source = blocks.assets / relative
    if not source.resolve().is_relative_to(blocks.assets.resolve()):
        raise EngineError(ErrorCode.PATH_ESCAPE, "图片路径越界")
    if not source.is_file():
        raise EngineError(ErrorCode.SCHEMA_INVALID, "图片资源不存在")
    if source.suffix.lower() not in {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tif", ".tiff"}:
        raise EngineError(ErrorCode.SCHEMA_INVALID, "图片格式不受支持")
    if (data.width_px is None) != (data.height_px is None) or any(
        v is not None and v <= 0 for v in [data.width_px, data.height_px]
    ):
        raise EngineError(ErrorCode.SCHEMA_INVALID, "图片像素尺寸必须同时为正数或同时省略")
    paragraph = blocks.paragraph(blocks.version.format_bindings.image, "image", "")
    width, height = blocks.geometry()
    place_picture(
        blocks.package,
        paragraph,
        source.read_bytes(),
        filename=relative.name,
        avail_w_emu=twips_to_emu(width),
        avail_h_emu=twips_to_emu(height),
        owner=blocks.version.format_bindings.insertion_part,
        cx_emu=pixels_to_emu(data.width_px, data.dpi) if data.width_px is not None else None,
        cy_emu=pixels_to_emu(data.height_px, data.dpi) if data.height_px is not None else None,
        alt_text=data.caption,
    )
    return [paragraph] + ([blocks.body(data.caption)] if data.caption else [])


def _trace(blocks: FrozenBlocks, unit: ContentUnit) -> list[Element]:
    lines = (unit.text or "\n".join(unit.trace)).splitlines() or [""]
    return [blocks.body(line) for line in lines]


def _pagebreak(blocks: FrozenBlocks, unit: ContentUnit) -> list[Element]:
    paragraph = blocks.body("")
    set_on_off(ensure_paragraph_properties(paragraph), "w:pageBreakBefore", True, order=P_PR_ORDER)
    return [paragraph]


def _unsupported(blocks: FrozenBlocks, unit: ContentUnit) -> list[Element]:
    raise EngineError(ErrorCode.SCHEMA_INVALID, "有效模板契约未声明可复用块绑定")


_HANDLERS: Final[dict[str, Callable[[FrozenBlocks, ContentUnit], list[Element]]]] = {
    "heading": _heading,
    "para": _body,
    "bullets": _list,
    "numbered": _list,
    "table": _table,
    "figure": _image,
    "screenshot": _image,
    "trace": _trace,
    "pagebreak": _pagebreak,
    "block_ref": _unsupported,
}
