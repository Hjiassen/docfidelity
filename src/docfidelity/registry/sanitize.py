"""从选择重建白名单包，不复制源 ZIP。"""

from dataclasses import dataclass

from docfidelity.contracts.errors import EngineError, ErrorCode
from docfidelity.contracts.imports import (
    CleanupReport,
    FrozenToc,
    FrozenVariable,
    ImportDraft,
    TemplateSelection,
)
from docfidelity.ooxml.doc import (
    Element,
    find_first,
    new_tree,
    qn,
    serialize,
    sub,
    xpath,
)
from docfidelity.ooxml.effective_clean import clean_content, clean_format, mark_container
from docfidelity.ooxml.effective_defaults import SourceDefaults
from docfidelity.ooxml.effective_fields import freeze_toc, safe_toc_instruction
from docfidelity.ooxml.effective_formats import (
    add_neutral_styles,
    apply_content_formats,
    clean_numbering_labels,
    clean_theme,
    remove_numbering,
)
from docfidelity.ooxml.effective_index import content_roots, locate_node
from docfidelity.ooxml.effective_sections import reconnect_sections
from docfidelity.ooxml.package import DOCUMENT_PART, DocxPackage, content_type_for_extension

_REL_BASE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/"


@dataclass
class SanitizedTemplate:
    package: DocxPackage
    blocks: dict[str, Element]
    variables: list[FrozenVariable]
    report: CleanupReport
    toc: list[FrozenToc]
    defaults: SourceDefaults | None = None


def sanitize_template(
    source: DocxPackage,
    draft: ImportDraft,
    selection: TemplateSelection,
    selected: set[tuple[str, int]],
) -> SanitizedTemplate:
    """消费经过服务端验证的选择并重建内容与关系。"""
    types = new_tree("ct:Types")
    sub(types, "ct:Default").attrib.update(
        {
            "Extension": "rels",
            "ContentType": "application/vnd.openxmlformats-package.relationships+xml",
        }
    )
    target = DocxPackage.from_parts({"[Content_Types].xml": serialize(types)})
    roots = content_roots(source)
    by_id = {n.node_id: n for n in draft.nodes}
    body_positions: set[tuple[str, int]] = set()
    for span in selection.ranges:
        if span.kind == "body":
            start, end = by_id[span.start_node_id], by_id[span.end_node_id]
            body_positions.update(
                (start.part, i) for i in range(start.block_index, end.block_index + 1)
            )
    markers: dict[str, dict[str, str]] = {}
    variables: list[FrozenVariable] = []
    for variable in selection.variables:
        node = by_id[variable.node_id]
        marker = f"df:variable:{variable.key}"
        markers.setdefault(node.part, {})[node.xpath] = marker
        variables.append(
            FrozenVariable(
                key=variable.key, part=node.part, marker=marker, required=variable.required
            )
        )
    images: dict[str, dict[str, str]] = {}
    for asset in selection.fixed_assets:
        node = by_id[asset]
        drawing = locate_node(source, node)
        blips = xpath(drawing, ".//a:blip")
        if len(blips) != 1 or blips[0].get(qn("r:link")):
            raise EngineError(ErrorCode.SCHEMA_INVALID, "仅支持单一内部图片素材")
        rel_id = blips[0].get(qn("r:embed"))
        relationships = [
            r
            for r in source.resolve_rels(node.part, role="image")
            if r.relationship.rel_id == rel_id
            and not r.relationship.is_external
            and r.part is not None
        ]
        if len(relationships) != 1:
            raise EngineError(ErrorCode.SCHEMA_INVALID, "固定图片关联不可用")
        resolved = relationships[0]
        assert resolved.part is not None
        extension = resolved.target_path.rpartition(".")[2].lower()
        if extension not in {"png", "jpg", "jpeg", "gif", "bmp", "tif", "tiff"}:
            raise EngineError(ErrorCode.SCHEMA_INVALID, "固定图片格式不受支持")
        path = f"word/media/fixed-{len(images) + sum(len(m) for m in images.values())}.{extension}"
        target.add_binary_part(
            path, content_type_for_extension(extension), resolved.part.to_bytes()
        )
        relationship = target.attach_relationship(
            node.part, type=_REL_BASE + "image", target="media/" + path.rpartition("/")[2]
        )
        images.setdefault(node.part, {})[node.xpath] = relationship.rel_id
    selected_parts = {part for part, _ in selected}
    section_rels: dict[str, str] = {}
    for resolved in source.resolve_rels(DOCUMENT_PART):
        if (
            resolved.relationship.role in {"header", "footer"}
            and resolved.target_path in selected_parts
        ):
            relationship = target.attach_relationship(
                DOCUMENT_PART,
                type=resolved.relationship.type,
                target=resolved.target_path.removeprefix("word/"),
            )
            section_rels[resolved.relationship.rel_id] = relationship.rel_id
    empty_rels: dict[str, str] = {}

    def empty_relationship(role: str) -> str:
        if role not in empty_rels:
            path = f"word/df-empty-{role}.xml"
            while source.has_part(path) or target.has_part(path):
                path = path.removesuffix(".xml") + "_.xml"
            empty = new_tree("w:hdr" if role == "header" else "w:ftr")
            sub(empty, "w:p")
            target.add_xml_part(
                path,
                f"application/vnd.openxmlformats-officedocument.wordprocessingml.{role}+xml",
                empty,
            )
            empty_rels[role] = target.attach_relationship(
                DOCUMENT_PART, type=_REL_BASE + role, target=path.removeprefix("word/")
            ).rel_id
        return empty_rels[role]

    blocks: dict[str, Element] = {}
    toc: list[FrozenToc] = []
    toc_starts: dict[tuple[str, int], tuple[int, Element]] = {}
    toc_covered: set[tuple[str, int]] = set()
    for span in selection.ranges:
        if span.kind != "toc":
            continue
        start, end = by_id[span.start_node_id], by_id[span.end_node_id]
        originals = list(roots[start.part])[start.block_index : end.block_index + 1]
        instruction = safe_toc_instruction(originals)
        marker = f"df:toc:{len(toc)}"
        toc.append(FrozenToc(part=start.part, marker=marker, instruction=instruction))
        wrapper = freeze_toc(
            originals,
            marker,
            instruction,
            markers=markers.get(start.part, {}),
            images=images.get(start.part, {}),
        )
        toc_starts[(start.part, start.block_index)] = (end.block_index, wrapper)
        toc_covered.update((start.part, i) for i in range(start.block_index, end.block_index + 1))
    for part, root in roots.items():
        if part != DOCUMENT_PART and part not in selected_parts:
            continue
        rebuilt = new_tree(
            "w:document"
            if part == DOCUMENT_PART
            else ("w:hdr" if root.tag == qn("w:hdr") else "w:ftr")
        )
        destination = sub(rebuilt, "w:body") if part == DOCUMENT_PART else rebuilt
        breaks, final_section = (
            reconnect_sections(
                root,
                {i for selected_part, i in selected if selected_part == part},
                section_rels,
                selection.formats,
                empty_relationship,
            )
            if part == DOCUMENT_PART
            else ({}, new_tree("w:sectPr"))
        )
        for i, element in enumerate(root):
            if (part, i) in toc_starts:
                destination.append(toc_starts[(part, i)][1])
            selected_block = (part, i) in selected
            if selected_block and (part, i) not in toc_covered:
                cleaned = clean_content(
                    element, markers=markers.get(part, {}), images=images.get(part, {})
                )
                if cleaned is not None:
                    # 分节引用由下面统一重建，避免保留源关系 ID。
                    for old_section in xpath(cleaned, ".//w:sectPr"):
                        parent = old_section.getparent()
                        assert parent is not None
                        parent.remove(old_section)
                    if (
                        (part, i) not in body_positions
                        or cleaned.tag == qn("w:sdt")
                        or xpath(cleaned, ".//w:sdt | .//w:drawing | .//w:fldSimple")
                    ):
                        if cleaned.tag == qn("w:sdtContent"):
                            destination.extend(list(cleaned))
                        else:
                            destination.append(cleaned)
                    matching = next(
                        (
                            n
                            for n in draft.nodes
                            if n.part == part and n.block_index == i and n.top_level
                        ),
                        None,
                    )
                    if matching is not None:
                        blocks[matching.node_id] = cleaned
                        apply_content_formats(
                            cleaned, selection.formats, heading_level=matching.heading_level
                        )
            if i in breaks:
                paragraph = sub(destination, "w:p")
                sub(paragraph, "w:pPr").append(breaks[i])
        if part == DOCUMENT_PART:
            destination.append(mark_container(new_tree("w:p"), "df:insert:content"))
            destination.append(final_section)
        if len(destination) == 0:
            sub(destination, "w:p")
        target.add_xml_part(part, source.content_type_of(part), rebuilt)
    cleared_numbering_labels = 0
    source_defaults: SourceDefaults | None = None
    for role, path in (("styles", "word/styles.xml"), ("numbering", "word/numbering.xml")):
        if source.has_part(path):
            format_tree = clean_format(source.part(path).read_tree)
            if format_tree is not None:
                if role == "styles" and not (
                    selection.formats.body
                    and selection.formats.heading
                    and selection.formats.tables
                    and selection.formats.numbering
                ):
                    source_defaults = SourceDefaults(format_tree, selection.formats)
                    add_neutral_styles(format_tree)
                if role == "numbering":
                    if not selection.formats.numbering:
                        format_tree = new_tree("w:numbering")
                    else:
                        cleared_numbering_labels = clean_numbering_labels(format_tree)
                if not selection.formats.numbering:
                    remove_numbering(format_tree)
                target.add_xml_part(path, source.content_type_of(path), format_tree)
                target.attach_relationship(
                    DOCUMENT_PART, type=_REL_BASE + role, target=path.removeprefix("word/")
                )
    if not target.has_part("word/styles.xml"):
        styles = new_tree("w:styles")
        source_defaults = SourceDefaults(styles, selection.formats)
        add_neutral_styles(styles)
        target.add_xml_part(
            "word/styles.xml",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml",
            styles,
        )
        target.attach_relationship(DOCUMENT_PART, type=_REL_BASE + "styles", target="styles.xml")
    if not target.has_part("word/numbering.xml"):
        target.add_xml_part(
            "word/numbering.xml",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.numbering+xml",
            new_tree("w:numbering"),
        )
        target.attach_relationship(
            DOCUMENT_PART, type=_REL_BASE + "numbering", target="numbering.xml"
        )
    if source_defaults is not None:
        numbering = target.part("word/numbering.xml").read_tree
        for block in blocks.values():
            source_defaults.apply(block, numbering)
        for part in roots:
            if target.has_part(part):
                source_defaults.apply(target.part(part).tree, numbering)
    for resolved in source.resolve_rels(DOCUMENT_PART, role="theme"):
        if resolved.part is not None and not resolved.relationship.is_external:
            theme = clean_theme(resolved.part.read_tree)
            if theme is not None:
                target.add_xml_part(
                    "word/theme/theme1.xml", source.content_type_of(resolved.target_path), theme
                )
                target.attach_relationship(
                    DOCUMENT_PART, type=_REL_BASE + "theme", target="theme/theme1.xml"
                )
                break
    settings = new_tree("w:settings")
    if toc:
        sub(settings, "w:updateFields", {"w:val": "true"})
    if (
        source.has_part("word/settings.xml")
        and find_first(source.part("word/settings.xml").read_tree, "w:evenAndOddHeaders")
        is not None
    ):
        sub(settings, "w:evenAndOddHeaders")
    target.add_xml_part(
        "word/settings.xml",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.settings+xml",
        settings,
    )
    target.attach_relationship(DOCUMENT_PART, type=_REL_BASE + "settings", target="settings.xml")
    target.attach_relationship("", type=_REL_BASE + "officeDocument", target=DOCUMENT_PART)
    for registered in target.parts():
        if registered.path.endswith(".xml") and registered.path != "[Content_Types].xml":
            target.ensure_override_type(registered.path, registered.content_type)
    for frozen_variable in variables:
        matches = xpath(
            target.part(frozen_variable.part).read_tree,
            f'.//w:sdt[w:sdtPr/w:tag[@w:val="{frozen_variable.marker}"]]',
        )
        if len(matches) != 1:
            raise EngineError(ErrorCode.SCHEMA_INVALID, "变量容器所在结构不受支持")
    retained = target.part_names()
    report = CleanupReport(
        removed_parts=sorted(set(source.part_names()) - set(retained)),
        retained_parts=sorted(retained),
        cleared_text_containers=sum(n.kind == "paragraph" for n in draft.nodes),
        excluded_objects=sum(n.kind == "object" for n in draft.nodes),
        fixed_assets=selection.fixed_assets,
        cleared_numbering_labels=cleared_numbering_labels,
    )
    return SanitizedTemplate(target, blocks, variables, report, toc, source_defaults)
