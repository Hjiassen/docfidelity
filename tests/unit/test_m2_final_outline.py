"""大纲跨契约上限必须由公共边界给出内核错误。"""

import pytest

from docfidelity.compose.outline import generate_outline, suggest_outline
from docfidelity.contracts.documents import DocumentDraft
from docfidelity.contracts.errors import EngineError, ErrorCode
from docfidelity.ingest.materials import ingest_text
from docfidelity.pipeline.writing import validate_document

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("counts", [(501,), (300, 201)])
@pytest.mark.parametrize("entry", ["suggest", "format_only", "organize", "validate"])
def test_heading_overflow_is_schema_error_without_truncating_material(counts, entry):
    materials = [
        ingest_text(
            "\n".join(f"# 章节{n}" for n in range(count)),
            name=f"m{i}.md",
            material_id=f"m{i}",
            markdown=True,
        )
        for i, count in enumerate(counts)
    ]
    before = [m.model_dump_json() for m in materials]
    document = DocumentDraft(
        document_id="d",
        revision=1,
        name="项目",
        template_id="t",
        template_version=1,
        material_ids=[m.material_id for m in materials],
        mode=entry if entry in {"format_only", "organize"} else "format_only",
    )
    with pytest.raises(EngineError) as caught:
        if entry == "suggest":
            suggest_outline(materials)
        elif entry == "validate":
            validate_document(document, materials)
        else:
            generate_outline(document, materials, None)
    assert caught.value.code == ErrorCode.SCHEMA_INVALID
    assert [m.model_dump_json() for m in materials] == before
    assert sum(len(m.blocks) for m in materials) == 501


def test_exact_outline_limit_retains_every_source_ref():
    material = ingest_text(
        "\n".join(f"# 章节{n}" for n in range(500)), name="m.md", material_id="m", markdown=True
    )
    outline = suggest_outline([material])
    assert len(outline) == 500
    assert outline[-1].source_refs == ["m/b0500"]
