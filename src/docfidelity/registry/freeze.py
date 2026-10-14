"""不可变有效包：空原型和全部定位在冻结时写入。"""

import shutil
from pathlib import Path

from pydantic import ValidationError

from docfidelity.contracts.errors import EngineError, ErrorCode
from docfidelity.contracts.imports import (
    FormatBindings,
    FrozenPrototype,
    ImportDraft,
    PrototypeKind,
    StructureKind,
    TemplateSelection,
    TemplateVersion,
)
from docfidelity.ooxml.doc import find_first, new_tree, qn, serialize, sub, xpath
from docfidelity.ooxml.effective_bundle import effective_content_hash
from docfidelity.ooxml.effective_bundle import load_template_version as load_template_version
from docfidelity.ooxml.effective_clean import clean_content
from docfidelity.ooxml.effective_formats import apply_content_formats, neutral_paragraph
from docfidelity.ooxml.effective_index import locate_node
from docfidelity.ooxml.package import DocxPackage
from docfidelity.platform.hashing import canonical_json, sha256_file

from .candidates import inspect_template
from .sanitize import SanitizedTemplate, sanitize_template
from .selection import validate_selection


def _write_prototypes(
    draft: ImportDraft,
    selection: TemplateSelection,
    source: DocxPackage,
    sanitized: SanitizedTemplate,
    directory: Path,
) -> tuple[list[FrozenPrototype], FormatBindings]:
    prototypes: list[FrozenPrototype] = []
    bindings = FormatBindings(toc=sanitized.toc)
    directory.mkdir()
    by_id = {n.node_id: n for n in draft.nodes}
    body_positions: set[tuple[str, int]] = set()
    for span in selection.ranges:
        if span.kind == "body":
            start, end = by_id[span.start_node_id], by_id[span.end_node_id]
            body_positions.update(
                (start.part, i) for i in range(start.block_index, end.block_index + 1)
            )
    for node in draft.nodes:
        if (
            not node.top_level
            or node.node_id not in sanitized.blocks
            or (node.part, node.block_index) not in body_positions
        ):
            continue
        original = locate_node(source, node)
        empty = clean_content(original, markers={}, images={})
        if empty is None:
            continue
        apply_content_formats(empty, selection.formats, heading_level=node.heading_level)
        for section in xpath(empty, ".//w:sectPr"):
            parent = section.getparent()
            assert parent is not None
            parent.remove(section)
        prototype_type: PrototypeKind
        if node.kind == StructureKind.TABLE:
            prototype_type = "table"
        elif node.kind == StructureKind.PARAGRAPH:
            prototype_type = (
                "heading"
                if node.heading_level is not None
                else "list"
                if xpath(original, "./w:pPr/w:numPr")
                or "List"
                in " ".join(e.get(qn("w:val"), "") for e in xpath(original, "./w:pPr/w:pStyle"))
                else "body"
            )
        else:
            continue
        if (
            (prototype_type == "body" and bindings.body is not None)
            or (prototype_type == "list" and bindings.list_paragraph is not None)
            or (prototype_type == "heading" and node.heading_level in bindings.heading)
        ):
            continue
        identifier = f"{prototype_type}-{len(prototypes)}"
        relative = f"prototypes/{identifier}.xml"
        (directory.parent / relative).write_bytes(serialize(empty))
        row_path: str | None = None
        columns: int | None = None
        if prototype_type == "table":
            rows = xpath(empty, "./w:tr")
            if not rows:
                raise EngineError(ErrorCode.SCHEMA_INVALID, "所选表格没有可冻结行")
            row_path = f"prototypes/{identifier}-row.xml"
            (directory.parent / row_path).write_bytes(serialize(rows[-1]))
            columns = len(xpath(rows[-1], "./w:tc"))
            bindings.tables.append(identifier)
        elif prototype_type == "heading":
            assert node.heading_level is not None
            bindings.heading[node.heading_level] = identifier
        elif prototype_type == "body":
            bindings.body = identifier
        else:
            bindings.list_paragraph = identifier
        prototypes.append(
            FrozenPrototype(
                prototype_id=identifier,
                kind=prototype_type,
                path=relative,
                source_node_id=node.node_id,
                row_path=row_path,
                columns=columns,
                heading_level=node.heading_level,
            )
        )
    if not selection.formats.heading:
        for level in range(1, 10):
            identifier = f"neutral-heading-{level}"
            relative = f"prototypes/{identifier}.xml"
            paragraph = new_tree("w:p")
            sub(sub(paragraph, "w:r"), "w:t")
            neutral_paragraph(paragraph, heading_level=level)
            (directory.parent / relative).write_bytes(serialize(paragraph))
            prototypes.append(
                FrozenPrototype(
                    prototype_id=identifier, kind="heading", path=relative, heading_level=level
                )
            )
            bindings.heading[level] = identifier
    if not selection.formats.body or not selection.formats.numbering:
        neutral = new_tree("w:p")
        sub(sub(neutral, "w:r"), "w:t")
        neutral_paragraph(neutral)
        if not selection.formats.body:
            identifier = "neutral-body"
            relative = f"prototypes/{identifier}.xml"
            (directory.parent / relative).write_bytes(serialize(neutral))
            prototypes.append(FrozenPrototype(prototype_id=identifier, kind="body", path=relative))
            bindings.body = identifier
        identifier = "neutral-list"
        relative = f"prototypes/{identifier}.xml"
        (directory.parent / relative).write_bytes(serialize(neutral))
        prototypes.append(FrozenPrototype(prototype_id=identifier, kind="list", path=relative))
        bindings.list_paragraph = identifier
    if not selection.formats.tables:
        identifier = "neutral-table"
        relative = f"prototypes/{identifier}.xml"
        row_path = f"prototypes/{identifier}-row.xml"
        section = xpath(sanitized.package.part("word/document.xml").read_tree, "./w:body/w:sectPr")[
            0
        ]
        page_size, margins = find_first(section, "w:pgSz"), find_first(section, "w:pgMar")
        try:
            width = int(page_size.get(qn("w:w"), "11906")) if page_size is not None else 11906
            left = int(margins.get(qn("w:left"), "1440")) if margins is not None else 1440
            right = int(margins.get(qn("w:right"), "1440")) if margins is not None else 1440
            width -= left + right
            if width <= 0:
                raise ValueError("不可用版心")
        except ValueError as exc:
            raise EngineError(ErrorCode.SCHEMA_INVALID, "所选页面尺寸不能承载中性表格") from exc
        table = new_tree("w:tbl")
        properties = sub(table, "w:tblPr")
        sub(properties, "w:tblW", {"w:w": str(width), "w:type": "dxa"})
        sub(properties, "w:tblLayout", {"w:type": "fixed"})
        sub(sub(table, "w:tblGrid"), "w:gridCol", {"w:w": str(width)})
        row = sub(table, "w:tr")
        cell = sub(row, "w:tc")
        sub(sub(cell, "w:tcPr"), "w:tcW", {"w:w": str(width), "w:type": "dxa"})
        paragraph = sub(cell, "w:p")
        sub(sub(paragraph, "w:r"), "w:t")
        neutral_paragraph(paragraph)
        (directory.parent / relative).write_bytes(serialize(table))
        (directory.parent / row_path).write_bytes(serialize(row))
        prototypes.append(
            FrozenPrototype(
                prototype_id=identifier,
                kind="table",
                path=relative,
                row_path=row_path,
                column_policy="flexible",
            )
        )
        bindings.tables = [identifier]
    # 图片的容器采用冻结正文格式，图片本身只由新 IR 或确认素材决定。
    if bindings.body is not None:
        body = next(p for p in prototypes if p.prototype_id == bindings.body)
        identifier = "image-container"
        relative = f"prototypes/{identifier}.xml"
        (directory.parent / relative).write_bytes((directory.parent / body.path).read_bytes())
        prototypes.append(
            FrozenPrototype(
                prototype_id=identifier,
                kind="image",
                path=relative,
                source_node_id=body.source_node_id,
            )
        )
        bindings.image = identifier
    return prototypes, bindings


def freeze_template(
    source: Path,
    selection: TemplateSelection,
    target_dir: Path,
    *,
    template_id: str,
    version: int,
    name: str,
) -> TemplateVersion:
    """校验选择，在同目录临时目录组装并原子发布不可变包。"""
    if version < 1 or not template_id.strip() or not name.strip():
        raise EngineError(ErrorCode.SCHEMA_INVALID, "模板标识、版本和名称不可为空")
    if target_dir.exists():
        raise EngineError(ErrorCode.VERSION_CONFLICT, "冻结版本已存在")
    if source.resolve() == target_dir.resolve() or source.resolve().is_relative_to(
        target_dir.resolve()
    ):
        raise EngineError(ErrorCode.PATH_ESCAPE, "冻结目录不能包含原件")
    draft = inspect_template(source, import_id=selection.import_id)
    selected = validate_selection(draft, selection)
    package = DocxPackage.open(source)
    sanitized = sanitize_template(package, draft, selection, selected)
    temp = target_dir.with_name(target_dir.name + ".dftmp")
    if temp.exists():
        raise EngineError(ErrorCode.VERSION_CONFLICT, "冻结临时目录已存在")
    created = False
    try:
        temp.mkdir(parents=True)
        created = True
        sanitized.package.save(temp / "effective.docx")
        (temp / "selection.json").write_bytes(canonical_json(selection.model_dump(mode="json")))
        (temp / "cleanup-report.json").write_bytes(
            canonical_json(sanitized.report.model_dump(mode="json"))
        )
        prototypes, bindings = _write_prototypes(
            draft, selection, package, sanitized, temp / "prototypes"
        )
        files = {
            p.relative_to(temp).as_posix(): sha256_file(p) for p in temp.rglob("*") if p.is_file()
        }
        result = TemplateVersion(
            template_id=template_id,
            version=version,
            name=name,
            source_sha256=draft.source_sha256,
            selection=selection,
            variables=sanitized.variables,
            prototypes=prototypes,
            format_bindings=bindings,
            cleanup_report=sanitized.report,
            file_hashes=files,
            content_hash="",
        )
        result.content_hash = effective_content_hash(result)
        (temp / "contract.json").write_bytes(canonical_json(result.model_dump(mode="json")))
        temp.rename(target_dir)
        return result
    except FileExistsError as exc:
        raise EngineError(ErrorCode.VERSION_CONFLICT, "冻结版本或临时目录已存在") from exc
    except (OSError, ValidationError) as exc:
        raise EngineError(ErrorCode.INTERNAL, "冻结包写入失败") from exc
    finally:
        if created and temp.exists():
            shutil.rmtree(temp)
