"""``pipeline.sources`` 接缝单测（DD-5.1 / DD-13.2 的 ``sources/*``）。

三条引脚：

① **探针永不抛**——它服务界面上的一个按钮，"连不通"要显示成结果而不是 500；
② **配置期与接入期分工**——``check_config`` 不读文件内容，``load_items`` 才读，
   于是"来源写错了"能在写库之前就答出来，不会因为"今天那个文件被移走"而失败；
③ **截断要如实说**——条目浏览器有上限，少给了必须能被看出来（``truncated``），
   否则"这份来源只有 200 条"会被读成事实。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from docfidelity.contracts.errors import EngineError, ErrorCode
from docfidelity.contracts.fact import SourceConfig
from docfidelity.pipeline import (
    check_config,
    connector_names,
    load_items,
    probe_source,
)
from tests.fixtures.ingest_fixture import build_workspace, openapi_config

pytestmark = pytest.mark.unit


def _source(source_type: str, *, root: str | None = None) -> SourceConfig:
    return SourceConfig(source_id="s1", source_type=source_type, root=root)  # type: ignore[arg-type]


class TestConnectorNames:
    def test_lists_exactly_the_builtin_connectors(self) -> None:
        assert connector_names() == ["assets", "ast", "openapi", "orm", "xlsx"]


class TestCheckConfig:
    def test_accepts_a_type_that_has_a_connector(self) -> None:
        assert check_config(_source("openapi")) == []

    def test_reports_a_type_without_a_connector(self) -> None:
        """``SourceType`` 那个 Literal 是闭集（含 markdown / proto / manual），
        而注册表只装了内建的五个。两侧的不一致必须在这里被说出来——否则它会一路
        走到构建期才现形，表现为"来源读不出东西"。"""
        problems = check_config(_source("markdown"))
        assert len(problems) == 1
        assert "markdown" in problems[0]

    def test_reports_a_root_that_escapes_the_workspace(self, tmp_path: Path) -> None:
        problems = check_config(_source("openapi", root="../../etc"), workspace_root=tmp_path)
        assert len(problems) == 1
        assert "工作区" in problems[0]

    def test_skips_the_path_check_when_no_root_is_given(self, tmp_path: Path) -> None:
        """没给工作区根 = 这一条做不了。做不了就不答，绝不默认为"没问题"。"""
        assert check_config(_source("openapi", root="../../etc")) == []

    def test_a_source_without_a_file_is_fine(self, tmp_path: Path) -> None:
        assert check_config(_source("openapi"), workspace_root=tmp_path) == []


class TestProbeSource:
    def test_unknown_type_returns_a_result_instead_of_raising(self, tmp_path: Path) -> None:
        probe = probe_source(_source("markdown"), workspace_root=tmp_path)
        assert probe.ok is False
        assert "markdown" in probe.detail

    def test_missing_target_is_reported_as_not_ok(self, tmp_path: Path) -> None:
        probe = probe_source(
            openapi_config(root="specs"), workspace_root=tmp_path / "empty-workspace"
        )
        assert probe.ok is False
        assert probe.detail

    def test_probes_a_real_workspace(self, tmp_path: Path) -> None:
        workspace = build_workspace(tmp_path)
        probe = probe_source(openapi_config(root="specs"), workspace_root=workspace)
        assert probe.ok is True
        assert probe.item_count is not None and probe.item_count > 0


class TestLoadItems:
    def test_reads_facts_and_their_kinds(self, tmp_path: Path) -> None:
        workspace = build_workspace(tmp_path)
        items = load_items(openapi_config(root="specs"), workspace_root=workspace)
        assert items.truncated is False
        assert items.total == len(items.facts) == 3
        assert items.kinds() == {"endpoint": 3}
        assert items.digest

    def test_truncates_and_says_so(self, tmp_path: Path) -> None:
        workspace = build_workspace(tmp_path)
        items = load_items(openapi_config(root="specs"), workspace_root=workspace, limit=1)
        assert len(items.facts) == 1
        assert items.total == 3
        assert items.truncated is True

    def test_carries_the_guard_warnings_of_a_directory_source(self, tmp_path: Path) -> None:
        """目录里的密钥类文件必须留下告警——丢了它等于把一次安全跳过说成正常。"""
        workspace = build_workspace(tmp_path)
        items = load_items(openapi_config(root="."), workspace_root=workspace)
        assert any(".env" in warning.message for warning in items.warnings)

    def test_raises_when_the_root_escapes_the_workspace(self, tmp_path: Path) -> None:
        with pytest.raises(EngineError) as caught:
            load_items(openapi_config(root="../../etc"), workspace_root=tmp_path)
        assert caught.value.code == ErrorCode.PATH_ESCAPE
