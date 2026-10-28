"""新版撰写必须只采用当前材料，并在不可信输出时保守回退。"""

from __future__ import annotations

import pytest

from docfidelity.compose.assembly import assemble_content
from docfidelity.compose.draft import generate_section
from docfidelity.compose.outline import generate_outline, suggest_outline
from docfidelity.contracts.documents import DocumentDraft, DraftBlock, OutlineSection, SectionDraft
from docfidelity.contracts.errors import EngineError
from docfidelity.contracts.generation import WritingRequest, WritingResult
from docfidelity.ingest.materials import ingest_text

pytestmark = pytest.mark.unit


def material():
    return ingest_text(
        "# 新计划\n甲队预算100万元。乙队预算200万元。\n# 新进度\n六月交付。",
        name="新材料.md",
        material_id="m1",
        markdown=True,
    )


def document(**updates):
    values = dict(
        document_id="d1",
        revision=1,
        name="新项目",
        template_id="t1",
        template_version=1,
        material_ids=["m1"],
        mode="organize",
    )
    return DocumentDraft.model_validate(values | updates)


class Generator:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def generate(self, request: WritingRequest) -> WritingResult:
        self.calls.append(request)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def confirmed(**updates):
    return document(outline=suggest_outline([material()]), outline_confirmed=True, **updates)


def test_rule_outline_uses_only_new_material_and_format_only_never_calls_model():
    source = material()
    outline = suggest_outline([source])
    assert [item.title for item in outline] == ["新计划", "新进度"]
    assert outline[0].source_refs == ["m1/b0001", "m1/b0002"]
    generator = Generator(RuntimeError("must not call"))
    doc = document(mode="format_only", outline=outline, outline_confirmed=True)
    assert generate_outline(doc, [source], generator).outline == outline
    section = generate_section(doc, [source], outline[0].section_id, generator).section
    assert section.blocks[0].text == "甲队预算100万元。乙队预算200万元。"
    assert not generator.calls


def test_organize_adopts_semantic_outline_and_actual_section_output():
    source = material()
    outline = [
        OutlineSection(
            section_id="new",
            title="预算安排",
            source_refs=[f"m1/{b.block_id}" for b in source.blocks],
        )
    ]
    generator = Generator(WritingResult(outline=outline, model="local", tokens_in=12, tokens_out=6))
    result = generate_outline(document(), [source], generator)
    assert result.outline == outline and result.model == "local" and not result.error
    doc = document(outline=outline, outline_confirmed=True)
    section = SectionDraft(
        section_id="new",
        blocks=[
            DraftBlock(
                kind="para",
                text="预算明确。甲队预算100万元。乙队预算200万元。",
                source_refs=["m1/b0002"],
            )
        ],
    )
    generator.result = WritingResult(section=section, model="local", tokens_out=9)
    generated = generate_section(doc, [source], "new", generator)
    assert generated.section == section and generated.tokens_out == 9 and not generated.error
    payload = generator.calls[-1].model_dump_json()
    assert "template" not in payload and "t1" not in payload and "style" not in payload
    assert generated.pending


@pytest.mark.parametrize(
    "text,refs",
    [
        ("预算999万元。", ["m1/b0002"]),
        ("甲队预算200万元。乙队预算100万元。", ["m1/b0002"]),
        ("无中生有", ["m1/missing"]),
        ("无中生有", ["m2/b0001"]),
        ("六月交付。", ["m1/b0004"]),
        ("七月交付。", ["m1/b0002"]),
    ],
)
def test_invalid_facts_refs_and_section_ownership_fall_back(text, refs):
    doc = confirmed()
    section_id = doc.outline[0].section_id
    candidate = SectionDraft(
        section_id=section_id, blocks=[DraftBlock(kind="para", text=text, source_refs=refs)]
    )
    result = generate_section(
        doc, [material()], section_id, Generator(WritingResult(section=candidate))
    )
    assert result.error and result.pending
    assert result.section.blocks[0].text == "甲队预算100万元。乙队预算200万元。"


def test_failure_preserves_prior_success_and_manual_edits_skip_model():
    doc = confirmed()
    section_id = doc.outline[0].section_id
    prior = SectionDraft(
        section_id=section_id,
        blocks=[DraftBlock(kind="para", text="已有成功", source_refs=["m1/b0002"])],
    )
    doc.sections = [prior]
    generator = Generator(TimeoutError("secret-key response body"))
    result = generate_section(doc, [material()], section_id, generator)
    assert result.section == prior and result.error and "secret" not in result.model_dump_json()
    prior.edited = True
    generator.calls.clear()
    assert generate_section(doc, [material()], section_id, generator).section == prior
    assert not generator.calls


def test_current_materials_and_confirmed_outline_are_required():
    with pytest.raises(EngineError):
        generate_section(document(outline=suggest_outline([material()])), [material()], "s1", None)
    with pytest.raises(EngineError):
        generate_outline(document(), [material().model_copy(update={"material_id": "other"})], None)
    doc = confirmed()
    doc.outline[0].source_refs = ["m1/missing"]
    with pytest.raises(EngineError):
        assemble_content(doc, [material()])


def test_outline_rejects_unknown_refs_duplicate_ids_and_silent_material_loss():
    source = material()
    for outline in [
        [OutlineSection(section_id="x", title="坏", source_refs=["m1/missing"])],
        [OutlineSection(section_id="x", title="坏", source_refs=["m1/b0001"])],
        [OutlineSection(section_id="x", title="坏", source_refs=["m1/b0001"])] * 2,
    ]:
        result = generate_outline(document(), [source], Generator(WritingResult(outline=outline)))
        assert result.error and result.outline == suggest_outline([source])


def test_assembly_preserves_reorder_rename_add_delete_and_marks_manual_unverified():
    source = material()
    first, second = suggest_outline([source])
    first.title = "用户改名"
    manual = OutlineSection(section_id="manual", title="人工新增")
    doc = document(
        outline=[second, manual, first],
        outline_confirmed=True,
        sections=[
            SectionDraft(
                section_id="manual",
                edited=True,
                blocks=[DraftBlock(kind="para", text="人工正文888")],
            )
        ],
    )
    ir = assemble_content(doc, [source])
    assert [u.text for u in ir.units] == ["新进度", "人工新增", "用户改名"]
    assert ir.units[1].children[0].origin.verified is False
    assert ir.units[2].children[0].trace == ["m1/b0002"]
    assert ir.units[2].children[0].origin.locator == "line:2-2"
    assert ir.source_digests == {"m1": source.digest} and not ir.duplicate_unit_ids()
    assert assemble_content(doc, [source]).model_dump_json() == ir.model_dump_json()
    doc.outline = [first]
    doc.sections = []
    reduced = assemble_content(doc, [source])
    assert len(reduced.units) == 1 and reduced.units[0].unit_id == ir.units[2].unit_id


def test_only_project_name_and_missing_model_are_pending_without_invented_facts():
    source = ingest_text("新项目", name="项目名.txt", material_id="m1")
    result = generate_outline(document(), [source], None)
    assert any("材料" in item for item in result.pending)
    doc = document(outline=result.outline, outline_confirmed=True)
    section = generate_section(doc, [source], result.outline[0].section_id, None)
    assert section.section.blocks[0].text == "新项目" and section.pending


def test_table_image_ir_is_kept_and_missing_render_capability_is_explicit():
    from docfidelity.contracts.imports import FormatBindings, TemplateVersion
    from docfidelity.contracts.materials import MaterialBlock
    from docfidelity.pipeline.writing import semantic_capabilities, validate_render_capabilities

    source = material().model_copy(
        update={
            "blocks": [
                MaterialBlock(
                    block_id="grid", kind="table", columns=["列"], rows=[["值"]], locator="line:1"
                ),
                MaterialBlock(
                    block_id="pic", kind="figure", image_ref="images/new.png", locator="line:2"
                ),
            ]
        }
    )
    doc = document(outline=suggest_outline([source]), outline_confirmed=True)
    ir = assemble_content(doc, [source])
    assert [unit.type for unit in ir.units[0].children] == ["table", "figure"]
    version = TemplateVersion.model_construct(format_bindings=FormatBindings(), prototypes=[])
    assert semantic_capabilities(version) == []
    with pytest.raises(EngineError):
        validate_render_capabilities(ir, version)


def test_model_cannot_silently_omit_table_or_image_or_forge_manual_sources():
    from docfidelity.contracts.materials import MaterialBlock

    source = material().model_copy(
        update={
            "blocks": [
                MaterialBlock(
                    block_id="grid", kind="table", columns=["列"], rows=[["值"]], locator="line:1"
                ),
                MaterialBlock(
                    block_id="pic", kind="figure", image_ref="images/new.png", locator="line:2"
                ),
            ]
        }
    )
    outline = suggest_outline([source])
    doc = document(outline=outline, outline_confirmed=True)
    for candidate in [
        SectionDraft(
            section_id=outline[0].section_id,
            blocks=[DraftBlock(kind="para", text="表图说明", source_refs=["m1/grid", "m1/pic"])],
        ),
        SectionDraft(
            section_id=outline[0].section_id,
            edited=True,
            blocks=[DraftBlock(kind="para", text="编造")],
        ),
    ]:
        result = generate_section(
            doc, [source], outline[0].section_id, Generator(WritingResult(section=candidate))
        )
        assert result.error and [b.kind for b in result.section.blocks] == ["table", "figure"]


def test_assemble_requires_confirmed_outline_and_marks_model_words_unverified():
    source = material()
    doc = document(outline=suggest_outline([source]))
    with pytest.raises(EngineError):
        assemble_content(doc, [source])
    doc.outline_confirmed = True
    section_id = doc.outline[0].section_id
    doc.sections = [
        SectionDraft(
            section_id=section_id,
            blocks=[DraftBlock(kind="para", text="整理后的描述", source_refs=["m1/b0002"])],
        )
    ]
    ir = assemble_content(doc, [source])
    assert ir.units[0].children[0].origin.verified is False
    assert any("审核" in item for item in ir.unresolved)


def test_numeric_table_cells_cannot_swap_between_rows():
    source = ingest_text(
        "| 团队 | 预算 |\n|---|---|\n| 甲 | 100 |\n| 乙 | 200 |",
        name="预算.md",
        material_id="m1",
        markdown=True,
    )
    doc = document(outline=suggest_outline([source]), outline_confirmed=True)
    section_id = doc.outline[0].section_id
    candidate = SectionDraft(
        section_id=section_id,
        blocks=[
            DraftBlock(
                kind="table",
                columns=["团队", "预算"],
                rows=[["甲", "200"], ["乙", "100"]],
                source_refs=["m1/b0001"],
            )
        ],
    )
    result = generate_section(
        doc, [source], section_id, Generator(WritingResult(section=candidate))
    )
    assert result.error and result.section.blocks[0].rows == [["甲", "100"], ["乙", "200"]]


def test_request_limit_is_explicit_and_does_not_send_truncated_materials():
    from docfidelity.contracts.materials import MaterialBlock, SourceMaterial

    sources = [
        SourceMaterial(
            material_id=f"m{n}",
            name="材料",
            media_type="text",
            digest="a" * 64,
            blocks=[
                MaterialBlock(block_id=f"b{i}", kind="para", text="原" * 100_000, locator="line:1")
                for i in range(15)
            ],
        )
        for n in range(3)
    ]
    doc = document(material_ids=[m.material_id for m in sources])
    generator = Generator(RuntimeError("must not send"))
    with pytest.raises(EngineError, match="不得截断"):
        generate_outline(doc, sources, generator)
    assert not generator.calls


def test_numeric_substring_cannot_claim_smaller_value_from_larger_number():
    source = ingest_text("1000", name="数字.txt", material_id="m1")
    doc = document(outline=suggest_outline([source]), outline_confirmed=True)
    section_id = doc.outline[0].section_id
    candidate = SectionDraft(
        section_id=section_id,
        blocks=[DraftBlock(kind="para", text="100", source_refs=["m1/b0001"])],
    )
    result = generate_section(
        doc, [source], section_id, Generator(WritingResult(section=candidate))
    )
    assert result.error and result.section.blocks[0].text == "1000"
