"""冻结原型渲染的结构性回归，不依赖上传材料。"""

import json
from pathlib import Path

import pytest

from docfidelity.contracts.errors import EngineError
from docfidelity.contracts.imports import TemplateVersion, VariableMapping
from docfidelity.contracts.ir import ContentIR, ContentUnit
from docfidelity.ooxml.doc import new_tree, qn, sub, xpath
from docfidelity.ooxml.effective_bundle import effective_content_hash
from docfidelity.ooxml.normalize import normalized_digest
from docfidelity.ooxml.package import DocxPackage
from docfidelity.registry.candidates import inspect_template
from docfidelity.registry.freeze import freeze_template
from docfidelity.registry.selection import default_selection
from docfidelity.render.effective import render_effective
from tests.fixtures.effective_template import build_effective_source

pytestmark = pytest.mark.unit


def _bundle(
    tmp_path: Path, *, variable: bool = False, formats: dict[str, bool] | None = None
) -> tuple[Path, TemplateVersion]:
    source = build_effective_source(tmp_path / "source.docx", cover=False)
    draft = inspect_template(source, import_id="draft")
    selection = default_selection(draft)
    if formats is not None:
        from docfidelity.contracts.imports import FormatSelection

        selection.formats = FormatSelection(**formats)
    selection.ranges = [r for r in selection.ranges if r.kind != "header"]
    if variable:
        node = next(n for n in draft.nodes if n.text_preview == "OLD_BODY")
        selection.variables = [VariableMapping(key="title", node_id=node.node_id)]
    directory = tmp_path / "bundle"
    version = freeze_template(source, selection, directory, template_id="t", version=1, name="T")
    return directory, version


def _ir(*units: ContentUnit, template_ref: str = "t@1") -> ContentIR:
    return ContentIR(project_id="p", doc_key="d", template_ref=template_ref, units=list(units))


def _save_contract(directory: Path, version: TemplateVersion) -> None:
    version.content_hash = effective_content_hash(version)
    (directory / "contract.json").write_text(version.model_dump_json(), encoding="utf-8")


def _tree(path: Path):
    return DocxPackage.open(path).part("word/document.xml").read_tree


def test_empty_prototypes_keep_direct_format_and_replace_variable(tmp_path: Path) -> None:
    directory, _ = _bundle(tmp_path, variable=True)
    output = tmp_path / "new.docx"
    content = _ir(
        ContentUnit(unit_id="h", type="heading", level=1, text="New heading"),
        ContentUnit(unit_id="p", type="para", text="New body\nSecond line\tColumn"),
    )
    assert render_effective(directory, content, {"title": "New variable"}, output) == output
    tree = _tree(output)
    assert (
        xpath(tree, './/w:p[w:r/w:t="New heading"]/w:pPr/w:pStyle')[0].get(qn("w:val"))
        == "Heading1"
    )
    paragraph = xpath(tree, './/w:p[w:r/w:t="New body"]')[0]
    assert xpath(paragraph, "./w:r/w:rPr/w:b")
    assert xpath(paragraph, "./w:r/w:rPr/w:rFonts")[0].get(qn("w:ascii")) == "Arial"
    assert len(xpath(paragraph, "./w:r/w:br")) == 1
    assert len(xpath(paragraph, "./w:r/w:tab")) == 1
    variable = xpath(tree, './/w:sdt[w:sdtPr/w:tag[@w:val="df:variable:title"]]')[0]
    assert xpath(variable, ".//w:t")[0].text == "New variable"
    assert xpath(variable, ".//w:rPr/w:b")
    assert not xpath(tree, ".//w:tbl")
    assert not any("OLD_" in (t.text or "") for t in xpath(tree, ".//w:t"))


def test_source_deleted_and_registry_disabled_with_three_identical_results(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory, _ = _bundle(tmp_path)
    (tmp_path / "source.docx").unlink()

    def forbidden(*args, **kwargs):
        raise AssertionError("生成期调用了入库解析器")

    monkeypatch.setattr("docfidelity.registry.candidates.inspect_template", forbidden)
    monkeypatch.setattr("docfidelity.registry.pipeline.parse_template", forbidden)
    monkeypatch.setattr("docfidelity.registry.freeze.inspect_template", forbidden)
    content = _ir(ContentUnit(unit_id="p", type="para", text="Fresh"))
    digests = []
    for index in range(3):
        output = render_effective(directory, content, {}, tmp_path / f"new-{index}.docx")
        package = DocxPackage.open(output)
        digests.append(normalized_digest(package))
        assert not any("header" in name for name in package.part_names())
        assert not xpath(package.part("word/document.xml").read_tree, './/w:pStyle[@w:val="Title"]')
    assert len(set(digests)) == 1


@pytest.mark.parametrize("values", [{}, {"title": "ok", "unknown": "bad"}])
def test_required_and_unknown_variables_fail(tmp_path: Path, values: dict[str, str]) -> None:
    directory, _ = _bundle(tmp_path, variable=True)
    with pytest.raises(EngineError, match="E_SCHEMA_INVALID"):
        render_effective(directory, _ir(), values, tmp_path / "new.docx")
    assert not (tmp_path / "new.docx").exists()


def test_optional_variable_can_stay_empty(tmp_path: Path) -> None:
    directory, version = _bundle(tmp_path, variable=True)
    version.variables[0].required = False
    _save_contract(directory, version)
    render_effective(directory, _ir(), {}, tmp_path / "new.docx")


@pytest.mark.parametrize("case", ["tamper", "version", "reference", "missing_body", "heading"])
def test_invalid_bundle_and_missing_prototypes_fail(tmp_path: Path, case: str) -> None:
    directory, version = _bundle(tmp_path)
    content = _ir(ContentUnit(unit_id="p", type="para", text="Fresh"))
    if case == "tamper":
        (directory / "effective.docx").write_bytes(b"broken")
    elif case == "version":
        payload = json.loads((directory / "contract.json").read_text("utf-8"))
        payload["format_version"] = "9.0.0"
        (directory / "contract.json").write_text(json.dumps(payload), encoding="utf-8")
    elif case == "reference":
        content.template_ref = "other@2"
    elif case == "missing_body":
        version.format_bindings.body = None
        _save_contract(directory, version)
    else:
        content.units = [ContentUnit(unit_id="h", type="heading", level=2, text="Absent")]
    with pytest.raises(EngineError):
        render_effective(directory, content, {}, tmp_path / "new.docx")
    assert not (tmp_path / "new.docx").exists()


def test_lists_trace_pagebreak_and_nested_children_are_rendered(tmp_path: Path) -> None:
    directory, _ = _bundle(tmp_path)
    content = _ir(
        ContentUnit(
            unit_id="h",
            type="heading",
            level=1,
            text="Nested",
            children=[ContentUnit(unit_id="p", type="para", text="Child")],
        ),
        ContentUnit(unit_id="b", type="bullets", fields={"items": ["Bullet A", "Bullet B"]}),
        ContentUnit(unit_id="n", type="numbered", text="Number A\nNumber B"),
        ContentUnit(unit_id="t", type="trace", trace=["Source A", "Source B"]),
        ContentUnit(unit_id="break", type="pagebreak"),
    )
    output = render_effective(directory, content, {}, tmp_path / "new.docx")
    tree = _tree(output)
    texts = [t.text for t in xpath(tree, ".//w:t") if t.text]
    assert texts == [
        "Nested",
        "Child",
        "Bullet A",
        "Bullet B",
        "Number A",
        "Number B",
        "Source A",
        "Source B",
    ]
    assert xpath(tree, './/w:p[w:r/w:t="Child"]/preceding-sibling::w:p[w:r/w:t="Nested"]')
    assert xpath(tree, ".//w:pageBreakBefore")
    package = DocxPackage.open(output)
    numbering = package.part("word/numbering.xml").read_tree
    for label, wanted in [("Bullet A", "bullet"), ("Number A", "decimal")]:
        num_id = xpath(tree, f'.//w:p[w:r/w:t="{label}"]/w:pPr/w:numPr/w:numId')[0].get(qn("w:val"))
        abstract_id = xpath(numbering, f'./w:num[@w:numId="{num_id}"]/w:abstractNumId')[0].get(
            qn("w:val")
        )
        assert (
            xpath(numbering, f'./w:abstractNum[@w:abstractNumId="{abstract_id}"]/w:lvl/w:numFmt')[
                0
            ].get(qn("w:val"))
            == wanted
        )


def test_table_rows_come_from_selected_same_table_and_never_enlarge(tmp_path: Path) -> None:
    directory, version = _bundle(tmp_path)
    selected = version.format_bindings.tables[1]
    content = _ir(
        ContentUnit(
            unit_id="grid",
            type="table",
            fields={
                "prototype_id": selected,
                "columns": ["Left", "Right"],
                "rows": [["A", "B"], ["C", "D"]],
                "caption": "Fresh caption",
            },
        )
    )
    tree = _tree(render_effective(directory, content, {}, tmp_path / "new.docx"))
    tables = xpath(tree, ".//w:tbl")
    assert len(tables) == 1
    assert len(xpath(tables[0], "./w:tr")) == 3
    assert xpath(tables[0], "./w:tr[2]/w:tc[1]//w:i")[0].get(qn("w:val")) == "0"
    assert sum(int(c.get(qn("w:w"))) for c in xpath(tables[0], "./w:tblGrid/w:gridCol")) <= 8640
    assert [t.text for t in xpath(tables[0], ".//w:t")] == ["Left", "Right", "A", "B", "C", "D"]
    assert xpath(tree, './/w:p[w:r/w:t="Fresh caption"]')


@pytest.mark.parametrize(
    "fields",
    [
        {"columns": ["A"], "rows": [["B"]]},
        {"columns": ["A", "B"], "rows": [["C"]]},
        {"columns": ["A", "B"], "rows": [], "prototype_id": "unknown"},
    ],
)
def test_table_mismatch_is_explicit(tmp_path: Path, fields: dict) -> None:
    directory, _ = _bundle(tmp_path)
    with pytest.raises(EngineError, match="E_SCHEMA_INVALID"):
        render_effective(
            directory,
            _ir(ContentUnit(unit_id="g", type="table", fields=fields)),
            {},
            tmp_path / "new.docx",
        )


@pytest.mark.parametrize("kind", ["figure", "screenshot"])
def test_image_and_caption_use_frozen_container_and_shrink_only(tmp_path: Path, kind: str) -> None:
    from base64 import b64decode

    directory, _ = _bundle(tmp_path)
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "pixel.png").write_bytes(
        b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a9xkAAAAASUVORK5CYII="
        )
    )
    content = _ir(
        ContentUnit(
            unit_id="small",
            type=kind,
            fields={"image_ref": "pixel.png", "width_px": 10, "height_px": 20, "caption": "Small"},
        ),
        ContentUnit(
            unit_id="large",
            type=kind,
            fields={"image_ref": "pixel.png", "width_px": 10000, "height_px": 10000},
        ),
    )
    tree = _tree(render_effective(directory, content, {}, tmp_path / "new.docx"))
    extents = xpath(tree, ".//wp:extent")
    assert (int(extents[0].get("cx")), int(extents[0].get("cy"))) == (95250, 190500)
    assert int(extents[1].get("cx")) <= 5486400
    assert xpath(tree, './/w:p[w:r/w:t="Small"]')
    assert len(xpath(tree, ".//w:r/w:drawing")) == 2


@pytest.mark.parametrize(
    "reference",
    ["../private.png", "C:/private.png", "/private.png", "..\\private.png", "missing.png"],
)
def test_image_reference_cannot_read_arbitrary_paths(tmp_path: Path, reference: str) -> None:
    directory, _ = _bundle(tmp_path)
    with pytest.raises(EngineError):
        render_effective(
            directory,
            _ir(ContentUnit(unit_id="f", type="figure", fields={"image_ref": reference})),
            {},
            tmp_path / "new.docx",
        )


def test_block_reference_without_frozen_binding_is_explicit(tmp_path: Path) -> None:
    directory, _ = _bundle(tmp_path)
    with pytest.raises(EngineError, match="E_SCHEMA_INVALID"):
        render_effective(
            directory,
            _ir(ContentUnit(unit_id="r", type="block_ref", fields={"block_id": "old"})),
            {},
            tmp_path / "new.docx",
        )


def test_nested_insertion_control_preserves_section_geometry(tmp_path: Path) -> None:
    from docfidelity.platform.hashing import sha256_file

    directory, version = _bundle(tmp_path)
    package = DocxPackage.open(directory / "effective.docx", read_only=False)
    tree = package.part("word/document.xml").tree
    marker = xpath(tree, './/w:sdt[w:sdtPr/w:tag[@w:val="df:insert:content"]]')[0]
    parent = marker.getparent()
    parent.remove(marker)
    wrapper = new_tree("w:sdt")
    sub(wrapper, "w:sdtContent").append(marker)
    parent.insert(len(parent) - 1, wrapper)
    section = xpath(tree, ".//w:sectPr")[-1]
    xpath(section, "./w:pgSz")[0].set(qn("w:w"), "6000")
    margins = xpath(section, "./w:pgMar")[0]
    margins.set(qn("w:left"), "1000")
    margins.set(qn("w:right"), "1000")
    package.save(directory / "effective.docx")
    version.file_hashes["effective.docx"] = sha256_file(directory / "effective.docx")
    _save_contract(directory, version)
    content = _ir(
        ContentUnit(unit_id="g", type="table", fields={"columns": ["A", "B"], "rows": [["C", "D"]]})
    )
    tree = _tree(render_effective(directory, content, {}, tmp_path / "new.docx"))
    assert xpath(tree, ".//w:sdtContent/w:tbl")
    assert sum(int(c.get(qn("w:w"))) for c in xpath(tree, ".//w:tblGrid/w:gridCol")) == 4000


def test_render_cannot_overwrite_frozen_bundle(tmp_path: Path) -> None:
    directory, _ = _bundle(tmp_path)
    with pytest.raises(EngineError, match="E_PATH_ESCAPE"):
        render_effective(directory, _ir(), {}, directory / "effective.docx")


@pytest.mark.parametrize("case", ["insertion", "variable", "duplicate_insertion"])
def test_missing_or_duplicate_frozen_anchor_fails(tmp_path: Path, case: str) -> None:
    from docfidelity.ooxml.doc import deep_copy
    from docfidelity.platform.hashing import sha256_file

    directory, version = _bundle(tmp_path, variable=True)
    package = DocxPackage.open(directory / "effective.docx", read_only=False)
    tree = package.part("word/document.xml").tree
    marker = "df:variable:title" if case == "variable" else "df:insert:content"
    control = xpath(tree, f'.//w:sdt[w:sdtPr/w:tag[@w:val="{marker}"]]')[0]
    if case == "duplicate_insertion":
        control.addprevious(deep_copy(control))
    else:
        control.getparent().remove(control)
    package.save(directory / "effective.docx")
    version.file_hashes["effective.docx"] = sha256_file(directory / "effective.docx")
    _save_contract(directory, version)
    with pytest.raises(EngineError, match="E_PACKAGE_INCOMPLETE"):
        render_effective(directory, _ir(), {"title": "Fresh"}, tmp_path / "new.docx")
    assert not (tmp_path / "new.docx").exists()


@pytest.mark.parametrize(
    "fields",
    [
        {"image_ref": "pixel.png", "width_px": 0, "height_px": 2},
        {"image_ref": "pixel.png", "width_px": 1},
        {"image_ref": "pixel.png", "dpi": 0},
    ],
)
def test_invalid_image_dimensions_fail(tmp_path: Path, fields: dict) -> None:
    directory, _ = _bundle(tmp_path)
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "pixel.png").write_bytes(b"not-read-for-invalid-dimensions")
    with pytest.raises(EngineError):
        render_effective(
            directory,
            _ir(ContentUnit(unit_id="f", type="figure", fields=fields)),
            {},
            tmp_path / "new.docx",
        )


def test_smaller_table_grid_keeps_original_width(tmp_path: Path) -> None:
    from docfidelity.ooxml.doc import parse, serialize
    from docfidelity.platform.hashing import sha256_file

    directory, version = _bundle(tmp_path)
    metadata = next(
        p for p in version.prototypes if p.prototype_id == version.format_bindings.tables[0]
    )
    path = directory / metadata.path
    table = parse(path.read_bytes())
    for column in xpath(table, "./w:tblGrid/w:gridCol"):
        column.set(qn("w:w"), "1000")
    path.write_bytes(serialize(table))
    version.file_hashes[metadata.path] = sha256_file(path)
    _save_contract(directory, version)
    content = _ir(
        ContentUnit(unit_id="g", type="table", fields={"columns": ["A", "B"], "rows": []})
    )
    tree = _tree(render_effective(directory, content, {}, tmp_path / "new.docx"))
    assert [int(c.get(qn("w:w"))) for c in xpath(tree, ".//w:tblGrid/w:gridCol")] == [1000, 1000]


def test_shrinking_table_does_not_enlarge_a_narrow_column(tmp_path: Path) -> None:
    from docfidelity.ooxml.doc import parse, serialize
    from docfidelity.platform.hashing import sha256_file

    directory, version = _bundle(tmp_path)
    metadata = next(
        p for p in version.prototypes if p.prototype_id == version.format_bindings.tables[0]
    )
    path = directory / metadata.path
    table = parse(path.read_bytes())
    for column, width in zip(xpath(table, "./w:tblGrid/w:gridCol"), [100, 20000], strict=True):
        column.set(qn("w:w"), str(width))
    path.write_bytes(serialize(table))
    version.file_hashes[metadata.path] = sha256_file(path)
    _save_contract(directory, version)
    content = _ir(
        ContentUnit(unit_id="g", type="table", fields={"columns": ["A", "B"], "rows": []})
    )
    tree = _tree(render_effective(directory, content, {}, tmp_path / "new.docx"))
    widths = [int(c.get(qn("w:w"))) for c in xpath(tree, ".//w:tblGrid/w:gridCol")]
    assert widths[0] <= 100
    assert widths[1] <= 20000
    assert sum(widths) <= 8640


def _rendered_list_level(package: DocxPackage, text: str):
    paragraph = xpath(package.part("word/document.xml").read_tree, f'.//w:p[w:r/w:t="{text}"]')[0]
    num_id = xpath(paragraph, "./w:pPr/w:numPr/w:numId")[0].get(qn("w:val"))
    numbering = package.part("word/numbering.xml").read_tree
    abstract_id = xpath(numbering, f'./w:num[@w:numId="{num_id}"]/w:abstractNumId')[0].get(
        qn("w:val")
    )
    return xpath(numbering, f'./w:abstractNum[@w:abstractNumId="{abstract_id}"]/w:lvl')[0]


def test_bullet_list_consumes_frozen_numbering_level_format(tmp_path: Path) -> None:
    directory, _ = _bundle(tmp_path)
    package = DocxPackage.open(
        render_effective(
            directory,
            _ir(ContentUnit(unit_id="b", type="bullets", text="Fresh bullet")),
            {},
            tmp_path / "new.docx",
        )
    )
    level = _rendered_list_level(package, "Fresh bullet")
    indent = xpath(level, "./w:pPr/w:ind")[0]
    assert indent.get(qn("w:left")) == "360"
    assert indent.get(qn("w:hanging")) == "360"
    tab = xpath(level, "./w:pPr/w:tabs/w:tab")[0]
    assert tab.get(qn("w:val")) == "num"
    assert tab.get(qn("w:pos")) == "360"
    fonts = xpath(level, "./w:rPr/w:rFonts")[0]
    assert fonts.get(qn("w:ascii")) == "Symbol"
    assert xpath(level, "./w:lvlText")[0].get(qn("w:val")) == "\uf0b7"


@pytest.mark.parametrize("kind", ["table", "figure", "screenshot"])
def test_caption_slot_is_rejected_without_frozen_numbering_protocol(
    tmp_path: Path, kind: str
) -> None:
    directory, _ = _bundle(tmp_path)
    fields = {"caption": "Caption", "caption_slot": 7}
    if kind == "table":
        fields.update({"columns": ["A", "B"], "rows": []})
    else:
        fields.update({"image_ref": "pixel.png", "width_px": 10, "height_px": 10})
        assets = tmp_path / "assets"
        assets.mkdir()
        (assets / "pixel.png").write_bytes(b"test")
    with pytest.raises(EngineError, match="E_SCHEMA_INVALID.*题注编号"):
        render_effective(
            directory,
            _ir(ContentUnit(unit_id="c", type=kind, fields=fields)),
            {},
            tmp_path / "new.docx",
        )
    assert not (tmp_path / "new.docx").exists()


def test_missing_declared_numbering_level_fails(tmp_path: Path) -> None:
    from docfidelity.platform.hashing import sha256_file

    directory, version = _bundle(tmp_path)
    package = DocxPackage.open(directory / "effective.docx", read_only=False)
    numbering = package.part("word/numbering.xml").tree
    for child in list(numbering):
        numbering.remove(child)
    package.save(directory / "effective.docx")
    version.file_hashes["effective.docx"] = sha256_file(directory / "effective.docx")
    _save_contract(directory, version)
    with pytest.raises(EngineError, match="E_PACKAGE_INCOMPLETE"):
        render_effective(
            directory,
            _ir(ContentUnit(unit_id="b", type="bullets", text="Fresh")),
            {},
            tmp_path / "new.docx",
        )


@pytest.mark.parametrize("formats", [{"numbering": False}, {"body": False}])
def test_neutral_list_selection_can_render_new_lists(
    tmp_path: Path, formats: dict[str, bool]
) -> None:
    directory, version = _bundle(tmp_path, formats=formats)
    if not version.selection.formats.numbering:
        numbering = (
            DocxPackage.open(directory / "effective.docx").part("word/numbering.xml").read_tree
        )
        assert not xpath(numbering, "./w:abstractNum | ./w:num")
    (tmp_path / "source.docx").unlink()
    content = _ir(
        ContentUnit(unit_id="b", type="bullets", text="Neutral bullet"),
        ContentUnit(unit_id="n", type="numbered", text="Neutral number"),
    )
    package = DocxPackage.open(render_effective(directory, content, {}, tmp_path / "new.docx"))
    for text, fmt, label in [
        ("Neutral bullet", "bullet", "\u2022"),
        ("Neutral number", "decimal", "%1."),
    ]:
        level = _rendered_list_level(package, text)
        assert xpath(level, "./w:numFmt")[0].get(qn("w:val")) == fmt
        assert xpath(level, "./w:lvlText")[0].get(qn("w:val")) == label
        assert not xpath(level, "./w:rPr/w:rFonts")


def test_numbered_list_preserves_frozen_marker_punctuation(tmp_path: Path) -> None:
    from docfidelity.platform.hashing import sha256_file

    directory, version = _bundle(tmp_path)
    package = DocxPackage.open(directory / "effective.docx", read_only=False)
    numbering = package.part("word/numbering.xml").tree
    for level in xpath(numbering, "./w:abstractNum/w:lvl"):
        xpath(level, "./w:numFmt")[0].set(qn("w:val"), "lowerRoman")
        xpath(level, "./w:lvlText")[0].set(qn("w:val"), "(%1)")
    package.save(directory / "effective.docx")
    version.file_hashes["effective.docx"] = sha256_file(directory / "effective.docx")
    _save_contract(directory, version)
    content = _ir(ContentUnit(unit_id="n", type="numbered", text="Roman item"))
    generated = DocxPackage.open(render_effective(directory, content, {}, tmp_path / "new.docx"))
    level = _rendered_list_level(generated, "Roman item")
    assert xpath(level, "./w:numFmt")[0].get(qn("w:val")) == "lowerRoman"
    assert xpath(level, "./w:lvlText")[0].get(qn("w:val")) == "(%1)"
    assert xpath(level, "./w:pPr/w:ind")[0].get(qn("w:hanging")) == "360"


def test_frozen_toc_control_is_retained_without_old_cache(tmp_path: Path) -> None:
    source = build_effective_source(tmp_path / "source.docx", cover=False)
    package = DocxPackage.open(source, read_only=False)
    body = xpath(package.part("word/document.xml").tree, "./w:body")[0]
    toc = new_tree("w:p")
    instruction = 'TOC \\o "1-3" \\h'
    field = sub(toc, "w:fldSimple", {"w:instr": instruction})
    sub(sub(field, "w:r"), "w:t").text = "OLD_TOC_CACHE"
    body.insert(1, toc)
    package.save(source)
    selection = default_selection(inspect_template(source, import_id="toc"))
    directory = tmp_path / "bundle"
    version = freeze_template(source, selection, directory, template_id="t", version=1, name="T")
    assert len(version.format_bindings.toc) == 1
    source.unlink()
    tree = _tree(
        render_effective(
            directory,
            _ir(ContentUnit(unit_id="p", type="para", text="Fresh")),
            {},
            tmp_path / "new.docx",
        )
    )
    marker = version.format_bindings.toc[0].marker
    controls = xpath(tree, f'.//w:sdt[w:sdtPr/w:tag[@w:val="{marker}"]]')
    assert len(controls) == 1
    assert xpath(controls[0], ".//w:fldSimple")[0].get(qn("w:instr")) == instruction
    assert not any("OLD_TOC" in (text.text or "") for text in xpath(tree, ".//w:t"))


def test_explicit_neutral_table_supports_new_column_count(tmp_path: Path) -> None:
    directory, _ = _bundle(tmp_path, formats={"tables": False})
    content = _ir(
        ContentUnit(
            unit_id="g",
            type="table",
            fields={
                "columns": ["A", "B", "C"],
                "rows": [["D", "E", "F"]],
            },
        )
    )
    tree = _tree(render_effective(directory, content, {}, tmp_path / "new.docx"))
    table = xpath(tree, ".//w:tbl")[0]
    assert len(xpath(table, "./w:tblGrid/w:gridCol")) == 3
    assert [len(xpath(row, "./w:tc")) for row in xpath(table, "./w:tr")] == [3, 3]
    assert [text.text for text in xpath(table, ".//w:t")] == ["A", "B", "C", "D", "E", "F"]
    assert not xpath(table, ".//w:tblStyle")
    assert all(run.get(qn("w:val")) == "0" for run in xpath(table, ".//w:rPr/w:i"))
    assert (
        sum(int(column.get(qn("w:w"))) for column in xpath(table, "./w:tblGrid/w:gridCol")) <= 8640
    )


def test_flexible_neutral_table_keeps_smaller_baseline_width(tmp_path: Path) -> None:
    from docfidelity.ooxml.doc import parse, serialize
    from docfidelity.platform.hashing import sha256_file

    directory, version = _bundle(tmp_path, formats={"tables": False})
    metadata = next(
        p for p in version.prototypes if p.prototype_id == version.format_bindings.tables[0]
    )
    path = directory / metadata.path
    table = parse(path.read_bytes())
    columns = xpath(table, "./w:tblGrid/w:gridCol")
    assert len(columns) == 1
    columns[0].set(qn("w:w"), "1000")
    path.write_bytes(serialize(table))
    version.file_hashes[metadata.path] = sha256_file(path)
    _save_contract(directory, version)
    content = _ir(
        ContentUnit(unit_id="g", type="table", fields={"columns": ["A", "B", "C"], "rows": []})
    )
    tree = _tree(render_effective(directory, content, {}, tmp_path / "new.docx"))
    widths = [int(c.get(qn("w:w"))) for c in xpath(tree, ".//w:tblGrid/w:gridCol")]
    assert widths == [334, 333, 333]
