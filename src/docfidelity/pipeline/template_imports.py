"""V2 有效模板用例门面：服务端只消费契约与这些公开入口。"""

from collections.abc import Mapping
from pathlib import Path

from docfidelity.contracts.imports import ImportDraft, TemplateSelection, TemplateVersion
from docfidelity.contracts.ir import ContentIR
from docfidelity.ooxml.effective_bundle import load_template_version as _load
from docfidelity.registry.candidates import inspect_template as _inspect
from docfidelity.registry.freeze import freeze_template as _freeze
from docfidelity.registry.selection import default_selection as _default
from docfidelity.registry.selection import validate_selection as _validate
from docfidelity.render.effective import render_effective as _render


def inspect_template(source: Path, *, import_id: str) -> ImportDraft:
    return _inspect(source, import_id=import_id)


def default_selection(draft: ImportDraft) -> TemplateSelection:
    return _default(draft)


def validate_selection(draft: ImportDraft, selection: TemplateSelection) -> None:
    _validate(draft, selection)


def freeze_template(
    source: Path,
    selection: TemplateSelection,
    target_dir: Path,
    *,
    template_id: str,
    version: int,
    name: str,
) -> TemplateVersion:
    return _freeze(
        source, selection, target_dir, template_id=template_id, version=version, name=name
    )


def load_template_version(bundle_dir: Path) -> TemplateVersion:
    return _load(bundle_dir)


def render_effective(
    bundle_dir: Path,
    content: ContentIR,
    variables: Mapping[str, str],
    output: Path,
) -> Path:
    return _render(bundle_dir, content, variables, output)
