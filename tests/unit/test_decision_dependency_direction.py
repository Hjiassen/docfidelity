"""决策协议下沉后，规则会话无需加载模型适配层。"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]


def test_content_ir_does_not_require_legacy_profile() -> None:
    from docfidelity.contracts.ir import ContentIR

    content = ContentIR(project_id="new", doc_key="document", template_ref="format@1")
    assert content.profile_id is None


def test_pipeline_does_not_import_agents() -> None:
    violations: list[str] = []
    for path in (ROOT / "src/docfidelity/pipeline").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            elif isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            else:
                continue
            if any(name.startswith("docfidelity.agents") for name in names):
                violations.append(f"{path.name}:{node.lineno}")
    assert violations == []


def test_rule_session_does_not_load_agents() -> None:
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; from docfidelity.pipeline import rule_only_session; "
                "s = rule_only_session('offline'); "
                "assert s.adapter.describe()['kind'] == 'null'; "
                "assert not any(n.startswith('docfidelity.agents') for n in sys.modules)"
            ),
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
