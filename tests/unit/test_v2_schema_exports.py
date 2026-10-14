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
