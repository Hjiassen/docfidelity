"""新版对外格式必须随发行包导出完整 schema。"""

import json

import pytest
from scripts.export_schemas import generate

pytestmark = pytest.mark.unit


def test_effective_template_schema_is_exported() -> None:
    output = generate()
    name = "docs/spec/effective-template/schema.json"
    assert name in output
    document = json.loads(output[name])
    definitions = document["$defs"]
    assert "TemplateVersion" in definitions
    assert "TemplateSelection" in definitions
    assert "FormatBindings" in definitions
    assert "source_sha256" in definitions["ImportDraft"]["properties"]


@pytest.mark.parametrize(
    ("family", "models"),
    [
        ("materials", ["SourceMaterial", "MaterialBlock"]),
        ("documents", ["DocumentDraft", "OutlineSection", "SectionDraft"]),
        ("generation", ["GenerationRun", "WritingRequest", "WritingResult", "RunEvent"]),
    ],
)
def test_writing_contract_schemas_are_exported(family: str, models: list[str]) -> None:
    output = generate()
    name = f"docs/spec/{family}/schema.json"
    assert name in output
    definitions = json.loads(output[name])["$defs"]
    for model in models:
        assert model in definitions
