"""xlsx 表格事实接入（FR-M1-08 / DD-5.2）。

输入：变更点表（多 sheet、合并区、偏移表头）。
抽取：sheet 清单、**无损**单元格文本、合并区、表头行位置。

零第三方依赖
------------
``.xlsx`` 本身就是一个 zip：``workbook.xml`` + ``sharedStrings.xml`` +
``worksheets/sheetN.xml``。用 ``zipfile`` + ``lxml``（已是 coredeps）自解析即可，
不必为"读一张表"引入整套表格栈——内核依赖表是刻意收窄的（``pyproject.toml``）。

"无损"的边界
------------
单元格**文本**原样保留（含公式的缓存结果、布尔、错误值），坐标按 ``A1`` 形式给出——
因此被合并的单元格、空行、错位的表头都能在事实里还原出来，而不是被"整齐化"成一张
规整表格。若在这里做对齐，接入层丢掉的位移信息在渲染层再也找不回来。

表头行是**启发式**（前 N 行里非空单元格最多的一行），N 由 ``options.header_scan_rows``
给出。启发式结果会作为 ``header_row`` 出现在事实里，因此人工可以在编排期覆盖它——
没有出口的启发式等于一个看不见的错误。

逐行产出（``options.row_shape``）
--------------------------------
默认只产"一张表一条事实"（``shape = "sheet"``）。但**以表为单位的粒度表达不了行级对象**：
把"每一行是一道工序 / 一条评估项"这类事实源接入时，需要的是"一行一个领域对象"。
故给出 ``row_shape``：给了它，就在 sheet 事实之外**按数据行**逐条产出（表头行作列名，
表头到首个非空行之间的偏移行不作数据），``locator`` 精确到 ``文件#sheet!行号``。

取值有两种形态，因为"一个工作簿里一种形态"是个不成立的前提：

====================  ==========================================================================
``"<形态>"``           所有 sheet 都按这个形态逐行产出
``{"<sheet 名>": "<形态>"}``  按表指定；``"*"`` 这个键表示"其余表的默认形态"，未列出的表**不逐行产出**
====================  ==========================================================================

一份工作簿里"接口清单"与"作业步骤"两张表天然属于两种领域类型。只给一个标量形态时，
接住其中一张就必须给另一张绑一个假类型——而假类型产生的事实会一路漂到 `merge` 里，
和真事实抢同一个 ``kind``。故按表指定是必需的，不是便利。

三条边界，都是"不这样写就会静默出错"的地方：

* **没有表头行就不逐行产出**（改为记一条告警）。没有列名，行取值就只能按列序号取——
  列序在真实表格里会变，而变化的后果是"字段串位"，从产物上完全看不出来。
* **表头重名只取首个并告警**。重名时"后者覆盖前者"与"前者胜出"都是猜测，
  但**必须有一条确定的口径**，否则同一份输入两次构建会得到不同结果。
* **取值一律是文本**（与"无损"口径一致）。故领域模型里按数值声明的字段接不住它们
  ——那是**声明**该改，不是让接入层偷偷转型。
"""

from __future__ import annotations

import posixpath
import re
import zipfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any, ClassVar

from lxml import etree

from docfidelity.contracts.errors import ConfigurationError
from docfidelity.contracts.fact import FactBundle, SourceConfig
from docfidelity.platform.fs import relative_posix

from ..emitter import FactEmitter
from ..protocol import ProbeResult
from ._support import option, root_path, source_files

MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"

#: 默认的表头探测扫描行数。
DEFAULT_HEADER_SCAN_ROWS: int = 10

#: 逐行产出所用的形态名**由配置给出**（``options.row_shape``）。缺省为空串 =
#: **不逐行产出**：现存配置的行为一字不变，新能力要显式打开。
DEFAULT_ROW_SHAPE: str = ""

#: ``row_shape`` 取值是对象时，"其余表"用的默认形态键。
FALLBACK_SHEET: str = "*"

_COORD_RE = re.compile(r"^([A-Z]+)(\d+)$")
_SHEET_XML_SUFFIX: str = ".xlsx"


def _column_index(letters: str) -> int:
    """``"AB"`` → 27（0 基）。"""
    index = 0
    for char in letters:
        index = index * 26 + (ord(char) - ord("A") + 1)
    return index - 1


def _coord_parts(ref: str) -> tuple[int, int] | None:
    match = _COORD_RE.match(ref)
    if match is None:
        return None
    return int(match.group(2)) - 1, _column_index(match.group(1))


def _shared_strings(archive: zipfile.ZipFile) -> list[str]:
    try:
        payload = archive.read("xl/sharedStrings.xml")
    except KeyError:
        return []
    root = etree.fromstring(payload)
    return ["".join(node.itertext()) for node in root.findall(f"{{{MAIN_NS}}}si")]


def _cell_text(cell: etree._Element, shared: list[str]) -> str:
    cell_type = cell.get("t", "")
    if cell_type == "inlineStr":
        inline = cell.find(f"{{{MAIN_NS}}}is")
        return "".join(inline.itertext()) if inline is not None else ""
    value = cell.find(f"{{{MAIN_NS}}}v")
    raw = value.text if value is not None and value.text is not None else ""
    if cell_type == "s":
        try:
            return shared[int(raw)]
        except (ValueError, IndexError):
            return raw
    if cell_type == "b":
        return "true" if raw.strip() == "1" else "false"
    return raw


def _sheet_targets(archive: zipfile.ZipFile) -> list[tuple[str, str]]:
    """返回 (sheet 名, zip 内路径) 序列，顺序即工作簿里的顺序。"""
    try:
        workbook = etree.fromstring(archive.read("xl/workbook.xml"))
    except KeyError as exc:
        raise ConfigurationError(
            "不是有效的 xlsx：缺少 xl/workbook.xml",
            where={"file": "xl/workbook.xml"},
            remediation=["确认上传的是 .xlsx 而非其它格式"],
        ) from exc
    try:
        relationships = etree.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
    except KeyError:
        relationships = None
    targets: dict[str, str] = {}
    if relationships is not None:
        for node in relationships.findall(f"{{{PKG_REL_NS}}}Relationship"):
            target_path = node.get("Target", "")
            if target_path:
                targets[node.get("Id", "")] = target_path

    sheets: list[tuple[str, str]] = []
    container = workbook.find(f"{{{MAIN_NS}}}sheets")
    if container is None:
        return sheets
    for index, node in enumerate(container.findall(f"{{{MAIN_NS}}}sheet")):
        name = node.get("name", "")
        rel_id = node.get(f"{{{REL_NS}}}id", "")
        target = targets.get(rel_id)
        if target is None:
            sheets.append((name, f"xl/worksheets/sheet{index + 1}.xml"))
            continue
        sheets.append((name, posixpath.normpath(posixpath.join("xl", target))))
    return sheets


def _worksheet_cells(
    archive: zipfile.ZipFile, path: str, shared: list[str]
) -> tuple[dict[str, str], list[str], int, int]:
    """返回 (坐标→文本, 合并区, 行数, 列数)。"""
    root = etree.fromstring(archive.read(path))
    cells: dict[str, str] = {}
    max_row = -1
    max_col = -1
    for cell in root.iter(f"{{{MAIN_NS}}}c"):
        ref = cell.get("r")
        if not ref:
            continue
        parts = _coord_parts(ref)
        if parts is None:
            continue
        row, col = parts
        text = _cell_text(cell, shared)
        if text == "":
            continue
        cells[ref] = text
        max_row = max(max_row, row)
        max_col = max(max_col, col)
    merged = [
        node.get("ref", "") for node in root.iter(f"{{{MAIN_NS}}}mergeCell") if node.get("ref")
    ]
    return cells, sorted(merged), max_row + 1, max_col + 1


def _grid(cells: dict[str, str], rows: int, cols: int) -> list[list[str]]:
    grid = [["" for _ in range(max(cols, 0))] for _ in range(max(rows, 0))]
    for ref, text in cells.items():
        parts = _coord_parts(ref)
        if parts is None:  # pragma: no cover - 坐标格式在读取时已校验
            continue
        row, col = parts
        if row < len(grid) and col < len(grid[row]):
            grid[row][col] = text
    return grid


def detect_header_row(rows: list[list[str]], *, scan: int = DEFAULT_HEADER_SCAN_ROWS) -> int | None:
    """启发式探测表头行，返回 0 基行号；判断不出来返回 ``None``。

    取前 ``scan`` 行里非空单元格最多的一行（并列取最早）。判定不出来的情形包括
    "整表全空"与"最多的一行也不到 2 个非空单元格"——这两种情况返回 ``None``，
    而不是勉强给个 0：错位的表头会让后面每一列的取值都错一格，且完全看不出来。
    """
    best: int | None = None
    best_count = 0
    for index, row in enumerate(rows[:scan]):
        count = sum(1 for value in row if value.strip())
        if count > best_count:
            best, best_count = index, count
    return best if best_count >= 2 else None


def row_shapes(cfg: SourceConfig) -> dict[str, str]:
    """解析 ``options.row_shape`` 成 ``{sheet 名: 形态}``；未启用时返回空表。

    不用 ``_support.option``：它按默认值的类型做校验，而本项**刻意是联合类型**
    （字符串 = 全部表；对象 = 按表指定）。硬塞进单类型取值器就得先猜一种，
    然后"给了对象却当成字符串"会静默退化成"形态名是 ``{'a': 'b'}``"这种谁也不会
    去看的字符串。
    """
    raw: Any = cfg.options.get("row_shape", DEFAULT_ROW_SHAPE)
    if raw is None or raw == "":
        return {}
    if isinstance(raw, str):
        return {FALLBACK_SHEET: raw}
    if isinstance(raw, Mapping):
        shapes: dict[str, str] = {}
        for name, value in raw.items():
            sheet_name = str(name)
            shape = str(value)
            if not sheet_name.strip():
                raise ConfigurationError(
                    f"事实源 {cfg.source_id} 的 options.row_shape 里有空的 sheet 名",
                    where={"source": cfg.source_id, "option": "row_shape"},
                    remediation=["把该键改为真实的 sheet 名，或删掉它"],
                )
            if shape:  # 取值是空串 = 明确声明"这张表不逐行产出"
                shapes[sheet_name] = shape
        return shapes
    raise ConfigurationError(
        f"事实源 {cfg.source_id} 的 options.row_shape 类型不符："
        f"期望 str 或 {{sheet 名: 形态}}，实际 {type(raw).__name__}",
        where={"source": cfg.source_id, "option": "row_shape", "actual": type(raw).__name__},
        remediation=[
            '改为字符串（所有表同一形态），或改为对象如 {"作业步骤": "procedure_row"}',
        ],
    )


def shape_for(shapes: Mapping[str, str], sheet: str) -> str:
    """某张表该用的逐行形态；未指定返回空串（= 这张表不逐行产出）。"""
    explicit = shapes.get(sheet)
    if explicit:
        return explicit
    return shapes.get(FALLBACK_SHEET, "")


def row_payloads(sheet: dict[str, Any]) -> tuple[list[tuple[int, dict[str, str]]], list[str]]:
    """按数据行产出 ``(Excel 行号, {列名: 文本})``；同时给出表头重名清单。

    行号是**1 起的 Excel 行号**而不是 0 基下标：定位符是写给人看的，
    而人在表格软件里数出来的就是这个数。

    表头行之后**全空的行直接跳过**（不产空事实）。判定"全空"用的是"所有列都空"，
    而不是"标识列都空"——后者需要知道哪一列是键，而键属于领域配置，接入层不知道。
    """
    header = sheet.get("header_row")
    if header is None:
        return [], []
    grid = _grid(
        sheet.get("cells") or {},
        int(sheet.get("row_count") or 0),
        int(sheet.get("col_count") or 0),
    )
    if header >= len(grid):
        return [], []

    labels: dict[int, str] = {}
    duplicates: list[str] = []
    for column, raw in enumerate(grid[header]):
        label = raw.strip()
        if not label:
            continue
        if label in labels.values():
            duplicates.append(label)
            continue
        labels[column] = label
    if not labels:
        return [], []

    records: list[tuple[int, dict[str, str]]] = []
    for offset, row in enumerate(grid[header + 1 :], start=header + 1):
        values = {
            label: (row[column] if column < len(row) else "")
            for column, label in sorted(labels.items())
        }
        if not any(value.strip() for value in values.values()):
            continue
        records.append((offset + 1, values))
    return records, duplicates


class XlsxConnector:
    """xlsx 表格接入器。"""

    name: ClassVar[str] = "xlsx"

    def _files(self, cfg: SourceConfig) -> list[Path]:
        return source_files(cfg, (_SHEET_XML_SUFFIX,))

    def _sheets(self, path: Path, scan: int) -> list[dict[str, Any]]:
        with zipfile.ZipFile(path) as archive:
            shared = _shared_strings(archive)
            sheets: list[dict[str, Any]] = []
            for index, (name, target) in enumerate(_sheet_targets(archive)):
                try:
                    cells, merged, rows, cols = _worksheet_cells(archive, target, shared)
                except KeyError:
                    cells, merged, rows, cols = {}, [], 0, 0
                grid = _grid(cells, rows, cols)
                sheets.append(
                    {
                        "sheet": name,
                        "index": index + 1,
                        "row_count": rows,
                        "col_count": cols,
                        "header_row": detect_header_row(grid, scan=scan),
                        "merged": merged,
                        "cells": cells,
                    }
                )
        return sheets

    def probe(self, cfg: SourceConfig) -> ProbeResult:
        files = self._files(cfg)
        if not files:
            return ProbeResult(ok=False, detail="目录下没有 .xlsx 文件")
        scan = option(cfg, "header_scan_rows", DEFAULT_HEADER_SCAN_ROWS)
        shapes = row_shapes(cfg)
        sheets = 0
        rows = 0
        headless = 0
        row_sheets = 0
        for path in files:
            for sheet in self._sheets(path, scan):
                sheets += 1
                if not shape_for(shapes, str(sheet["sheet"])):
                    continue
                row_sheets += 1
                if sheet.get("header_row") is None:
                    headless += 1
                rows += len(row_payloads(sheet)[0])
        if not shapes:
            return ProbeResult(
                ok=True, detail=f"{len(files)} 个工作簿，共 {sheets} 张 sheet", item_count=sheets
            )
        # 逐行形态下"条数"是**行数**而不是表数：试接入要让使用者看到与构建同量级的规模，
        # 否则界面上"3 项"而构建产出 40 条事实，差值归因会落在错误的地方。
        detail = (
            f"{len(files)} 个工作簿，共 {sheets} 张 sheet；"
            f"{row_sheets} 张表按逐行形态接入，可产出 {rows} 条"
        )
        if headless:
            detail += f"（{headless} 张表未识别出表头行，不会逐行产出）"
        return ProbeResult(ok=True, detail=detail, item_count=rows)

    def load(self, cfg: SourceConfig) -> FactBundle:
        root = root_path(cfg)
        scan = option(cfg, "header_scan_rows", DEFAULT_HEADER_SCAN_ROWS)
        shapes = row_shapes(cfg)
        emitter = FactEmitter(cfg)
        files = self._files(cfg)
        if not files:
            raise ConfigurationError(
                f"事实源 {cfg.source_id} 下没有 .xlsx 文件",
                where={"source": cfg.source_id, "root": cfg.root},
                remediation=["指向变更点表文件或其所在目录"],
            )
        for path in files:
            relative = relative_posix(path, root) if root.is_dir() else path.name
            for sheet in self._sheets(path, scan):
                payload = dict(sheet)
                payload["source_file"] = relative
                emitter.emit("sheet", payload, locator=f"{relative}#{sheet['sheet']}")
                row_shape = shape_for(shapes, str(sheet["sheet"]))
                if row_shape:
                    _emit_rows(emitter, sheet, relative=relative, row_shape=row_shape)
        return emitter.bundle()


def _emit_rows(
    emitter: FactEmitter, sheet: dict[str, Any], *, relative: str, row_shape: str
) -> None:
    """把一张表按数据行逐条产事实（未绑定该形态时由 ``FactEmitter`` 留告警）。"""
    where = f"{relative}#{sheet['sheet']}"
    if sheet.get("header_row") is None:
        emitter.warn("未识别出表头行，该表未逐行产出", locator=where)
        return
    records, duplicates = row_payloads(sheet)
    if duplicates:
        emitter.warn(f"表头存在重名列，已取首个：{sorted(set(duplicates))}", locator=where)
    for number, values in records:
        emitter.emit(row_shape, values, locator=f"{where}!{number}")


__all__ = [
    "DEFAULT_HEADER_SCAN_ROWS",
    "DEFAULT_ROW_SHAPE",
    "FALLBACK_SHEET",
    "XlsxConnector",
    "detect_header_row",
    "row_payloads",
    "row_shapes",
    "shape_for",
]
