"""Windows 并发解析可能返回等价的扩展路径，包含判定必须一致。"""

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from docfidelity.contracts.errors import EngineError
from docfidelity.platform.fs import ensure_within

pytestmark = [pytest.mark.unit, pytest.mark.skipif(os.name != "nt", reason="Windows 路径解析")]


@pytest.mark.parametrize(
    ("root", "candidate"),
    [
        ("C:/workspace", "//?/C:/workspace/jobs/new"),
        ("//?/C:/workspace", "C:/workspace/jobs/new"),
        ("//server/share/workspace", "//?/UNC/server/share/workspace/jobs/new"),
        ("//?/UNC/server/share/workspace", "//server/share/workspace/jobs/new"),
    ],
)
def test_extended_path_is_equivalent_for_containment(root: str, candidate: str) -> None:
    resolved = Path(candidate)
    with patch.object(Path, "resolve", side_effect=[Path(root), resolved]):
        assert ensure_within(root, candidate) == resolved


def test_extended_prefix_does_not_allow_a_sibling_escape() -> None:
    root, candidate = "C:/workspace", "//?/C:/workspace-other/secret"
    with (
        patch.object(Path, "resolve", side_effect=[Path(root), Path(candidate)]),
        pytest.raises(EngineError) as caught,
    ):
        ensure_within(root, candidate)
    assert caught.value.code == "E_PATH_ESCAPE"
