"""从选择重建白名单包，不复制源 ZIP。"""

from dataclasses import dataclass

from docfidelity.contracts.errors import EngineError, ErrorCode
from docfidelity.contracts.imports import (
    CleanupReport,
    FrozenVariable,
    ImportDraft,
    TemplateSelection,
)
from docfidelity.ooxml.doc import (
    Element,
    find_first,
    localname,
    new_tree,
    qn,
    serialize,
    sub,
    xpath,
)
from docfidelity.ooxml.effective_clean import clean_content, clean_format, mark_container
from docfidelity.ooxml.effective_index import content_roots, locate_node
from docfidelity.ooxml.package import DOCUMENT_PART, DocxPackage, content_type_for_extension

_REL_BASE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/"


@dataclass
class SanitizedTemplate:
    package: DocxPackage
    blocks: dict[str, Element]
    variables: list[FrozenVariable]
    report: CleanupReport


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
    blocks: dict[str, Element] = {}
    sections: list[Element] = []
    for part, root in roots.items():
        if part != DOCUMENT_PART and part not in selected_parts:
            continue
        rebuilt = new_tree(
            "w:document"
            if part == DOCUMENT_PART
            else ("w:hdr" if root.tag == qn("w:hdr") else "w:ftr")
        )
        destination = sub(rebuilt, "w:body") if part == DOCUMENT_PART else rebuilt
        for i, element in enumerate(root):
            selected_block = (part, i) in selected
            if selected_block:
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
            original_sections = (
                [element] if element.tag == qn("w:sectPr") else xpath(element, ".//w:sectPr")
            )
            for original in original_sections:
                section = clean_format(original)
                assert section is not None
                for reference in original:
                    if reference.tag in {qn("w:headerReference"), qn("w:footerReference")}:
                        mapped = section_rels.get(reference.get(qn("r:id"), ""))
                        if mapped is not None:
                            ref = new_tree(
                                "w:" + localname(reference),
                                {"w:type": reference.get(qn("w:type"), "default"), "r:id": mapped},
                            )
                            section.insert(0, ref)
                sections.append(section)
                if element.tag != qn("w:sectPr"):
                    paragraph = sub(destination, "w:p")
                    sub(paragraph, "w:pPr").append(section)
        if part == DOCUMENT_PART:
            destination.append(mark_container(new_tree("w:p"), "df:insert:content"))
            final = sections[-1] if sections else new_tree("w:sectPr")
            if final.getparent() is None:
                destination.append(final)
            else:
                destination.append(new_tree("w:sectPr"))
        if len(destination) == 0:
            sub(destination, "w:p")
        target.add_xml_part(part, source.content_type_of(part), rebuilt)
    for role, path in (("styles", "word/styles.xml"), ("numbering", "word/numbering.xml")):
        if source.has_part(path):
            format_tree = clean_format(source.part(path).read_tree)
            if format_tree is not None:
                target.add_xml_part(path, source.content_type_of(path), format_tree)
                target.attach_relationship(
                    DOCUMENT_PART, type=_REL_BASE + role, target=path.removeprefix("word/")
                )
    settings = new_tree("w:settings")
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
    )
    return SanitizedTemplate(target, blocks, variables, report)
