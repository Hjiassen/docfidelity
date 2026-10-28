"""内容契约拒绝歧义引用、空材料与无界输入。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from docfidelity.contracts.documents import DocumentDraft, DraftBlock, OutlineSection, SectionDraft
from docfidelity.contracts.generation import GenerationRun, RunEvent, WritingRequest, WritingResult
from docfidelity.contracts.materials import MaterialBlock, SourceMaterial

pytestmark = pytest.mark.unit


def _material() -> SourceMaterial:
    return SourceMaterial(
        material_id="m1",
        name="材料",
        media_type="text",
        digest="a" * 64,
        blocks=[MaterialBlock(block_id="b1", kind="para", text="正文", locator="line:1-1")],
    )


def _document(**values: object) -> DocumentDraft:
    return DocumentDraft.model_validate(
        dict(
            document_id="d1",
            revision=1,
            name="文档",
            template_id="t1",
            template_version=1,
            material_ids=["m1"],
            mode="organize",
        )
        | values
    )


def test_source_contract_rejects_empty_duplicate_ambiguous_and_oversized_data() -> None:
    valid = _material().model_dump()
    for update in [
        dict(blocks=[]),
        dict(material_id="a/b"),
        dict(digest="bad"),
        dict(blocks=valid["blocks"] * 2),
        dict(name="x" * 513),
    ]:
        with pytest.raises(ValidationError):
            SourceMaterial.model_validate(valid | update)
    with pytest.raises(ValidationError):
        MaterialBlock(
            block_id="b1", kind="table", locator="line:1", columns=["a", "b"], rows=[["one"]]
        )
    with pytest.raises(ValidationError):
        MaterialBlock(block_id="b1", kind="para", locator="line:1", text="x" * 100_001)
    with pytest.raises(ValidationError):
        MaterialBlock(block_id="b1", kind="figure", locator="line:1", image_ref="../x.png")


@pytest.mark.parametrize(
    "fields",
    [
        dict(kind="para", text="正文", items=["隐藏"]),
        dict(kind="table", columns=["列"], rows=[["值"]], text="隐藏"),
        dict(kind="bullets", items=["项"], image_ref="images/x.png"),
    ],
)
def test_blocks_reject_irrelevant_fields_instead_of_losing_content(
    fields: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        MaterialBlock.model_validate(dict(block_id="b1", locator="line:1") | fields)
    with pytest.raises(ValidationError):
        DraftBlock.model_validate(fields)


def test_document_rejects_duplicate_unknown_sections_and_invalid_refs() -> None:
    outline = OutlineSection(section_id="s1", title="章节", source_refs=["m1/b1"])
    for values in [
        dict(material_ids=["m1", "m1"]),
        dict(outline=[outline, outline]),
        dict(sections=[SectionDraft(section_id="missing", blocks=[])]),
        dict(outline=[OutlineSection(section_id="s1", title="章节", source_refs=["m2/b1"])]),
    ]:
        with pytest.raises(ValidationError):
            _document(**values)
    with pytest.raises(ValidationError):
        OutlineSection(section_id="s1", title="章节", source_refs=["ambiguous"])


def test_unsourced_manual_content_requires_explicit_edit_marker() -> None:
    block = DraftBlock(kind="para", text="人工改写")
    with pytest.raises(ValidationError):
        SectionDraft(section_id="s1", blocks=[block])
    assert SectionDraft(section_id="s1", blocks=[block], edited=True).edited
    assert SectionDraft(
        section_id="s1", blocks=[block.model_copy(update={"source_refs": ["m1/b1"]})]
    ).blocks


def test_writing_contract_is_bounded_semantic_and_has_independent_defaults() -> None:
    material = _material()
    assert WritingRequest(
        task="outline",
        name="文档",
        instructions="",
        materials=[material],
        capabilities=["para", "table"],
    ).materials
    with pytest.raises(ValidationError):
        WritingRequest(task="outline", name="文档", instructions="", materials=[material, material])
    with pytest.raises(ValidationError):
        WritingRequest(task="section", name="文档", instructions="", materials=[material])
    with pytest.raises(ValidationError):
        WritingRequest(
            task="outline",
            name="文档",
            instructions="",
            materials=[material],
            capabilities=["StyleID"],
        )
    first, second = WritingResult(), WritingResult()
    first.pending.append("待确认")
    assert not second.pending


def test_generation_run_and_replay_event_preserve_frozen_input() -> None:
    run = GenerationRun(
        run_id="r1",
        document_id="d1",
        kind="outline",
        input_digest="b" * 64,
        document_revision=1,
        document_snapshot=_document(),
        materials_snapshot=[_material()],
        template_id="t1",
        template_version=1,
        prompt_version="v1",
    )
    assert run.state == "queued" and run.event_cursor == 0 and not run.attempts
    event = RunEvent(event_id=1, run_id="r1", type="run", payload={"state": "running"})
    assert event.model_validate_json(event.model_dump_json()).event_id == 1
    with pytest.raises(ValidationError):
        RunEvent(event_id=0, run_id="r1", type="run")
