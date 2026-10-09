"""迁移输出不得暴露连接串中的凭据。"""

from __future__ import annotations

import pytest

from server import migrate

pytestmark = pytest.mark.api


def test_success_output_never_contains_database_credentials(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def run_migrations(url: str, *, revision: str) -> None:
        assert revision == "head"

    monkeypatch.setattr(migrate, "run_migrations", run_migrations)
    url = "postgresql://demo:private-password@localhost/demo?token=private-token"
    assert migrate.main(["--url", url]) == 0
    output = capsys.readouterr().out
    assert "head" in output
    assert "private-password" not in output
    assert "private-token" not in output
    assert url not in output
