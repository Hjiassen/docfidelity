"""接入框架与安全校验的单测（DD-5.1 / DD-5.4）。

这些测试盯的是**失败姿态**而不是happy path：未登记的接入器、回填错来源标识的接入器、
抛裸异常的接入器、越界的压缩包条目、密钥文件。每一条都对应一种"产物看着正常、
问题没人知道"的失败形态。
"""

from __future__ import annotations

import os
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from docfidelity.contracts.errors import ConfigurationError, EngineError, ErrorCode
from docfidelity.contracts.fact import Fact, FactBundle, SourceConfig
from docfidelity.ingest import (
    ENTRY_POINT_GROUP,
    ConnectorRegistry,
    build_bundle,
    check_file,
    check_tree,
    default_registry,
    discover_entry_points,
    fact_sort_key,
    inspect_archive,
    is_sensitive_name,
    probe_connector,
    run_connector,
)
from docfidelity.ingest.protocol import FactSourceConnector, ProbeResult
from docfidelity.ingest.safety import SourceGuard, UploadPolicy

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
BUILTIN_NAMES = ["assets", "ast", "openapi", "orm", "xlsx"]


def _fact(key: str, payload: dict[str, object], *, source: str = "s1", locator: str = "L1") -> Fact:
    return Fact(source=source, kind="k", key=[key], payload=payload, locator=locator)


class _StubConnector:
    """最小接入器：可被指定返回值或抛出指定异常。"""

    name = "stub"

    def __init__(
        self,
        *,
        bundle: FactBundle | None = None,
        error: BaseException | None = None,
    ) -> None:
        self._bundle = bundle
        self._error = error

    def probe(self, cfg: SourceConfig) -> ProbeResult:
        if self._error is not None:
            raise self._error
        return ProbeResult(ok=True, detail="stub")

    def load(self, cfg: SourceConfig) -> FactBundle:
        if self._error is not None:
            raise self._error
        if self._bundle is None:
            return build_bundle(source_id=cfg.source_id, source_type=cfg.source_type, facts=[])
        return self._bundle


def _cfg(**overrides: object) -> SourceConfig:
    base: dict[str, object] = {"source_id": "s1", "source_type": "openapi"}
    base.update(overrides)
    return SourceConfig(**base)  # type: ignore[arg-type]


# -- 注册表 ----------------------------------------------------------------


def test_builtin_registry_covers_the_documented_connectors() -> None:
    registry = default_registry()
    assert registry.names() == BUILTIN_NAMES
    assert len(registry) == len(BUILTIN_NAMES)


def test_every_connector_satisfies_the_protocol() -> None:
    for connector in default_registry()._connectors.values():
        assert isinstance(connector, FactSourceConnector), connector


def test_unknown_connector_name_raises_with_known_names() -> None:
    with pytest.raises(EngineError) as info:
        default_registry().get("nope")
    assert info.value.code == ErrorCode.SOURCE_LOAD_FAILED
    assert info.value.where["known"] == BUILTIN_NAMES


def test_registering_a_different_class_under_the_same_name_fails() -> None:
    registry = ConnectorRegistry()
    registry.register(_StubConnector())

    class Other:
        name = "stub"

        def probe(self, cfg: SourceConfig) -> ProbeResult:  # pragma: no cover - 不会被调用
            return ProbeResult(ok=True, detail="")

        def load(self, cfg: SourceConfig) -> FactBundle:  # pragma: no cover - 不会被调用
            raise NotImplementedError

    with pytest.raises(ConfigurationError):
        registry.register(Other())  # type: ignore[arg-type]


def test_registering_the_same_class_twice_is_idempotent() -> None:
    registry = ConnectorRegistry()
    registry.register(_StubConnector())
    registry.register(_StubConnector())
    assert registry.names() == ["stub"]


def test_names_returns_a_fresh_list() -> None:
    registry = default_registry()
    registry.names().append("junk")
    assert "junk" not in registry.names()


def test_discover_entry_points_merges_plugin_connectors(monkeypatch: pytest.MonkeyPatch) -> None:
    class Entry:
        name = "stub"
        value = "tests:_StubConnector"

        def load(self) -> type[_StubConnector]:
            return _StubConnector

    monkeypatch.setattr(
        "docfidelity.ingest.registry.entry_points",
        lambda *, group: [Entry()] if group == ENTRY_POINT_GROUP else [],
    )
    registry = ConnectorRegistry()
    added = discover_entry_points(registry)
    assert added == ["stub"]
    assert registry.has("stub")
    assert discover_entry_points(registry) == []  # 第二次不再算"新增"


def test_importing_ingest_does_not_pull_connector_implementations() -> None:
    """导入接入层不得连带拉起各接入器实现（惰性导入的可执行证明）。"""
    program = (
        "import sys, docfidelity.ingest;"
        "leaked=[m for m in sys.modules if m.startswith('docfidelity.ingest.connectors.')];"
        "print(sorted(leaked));"
        "assert not leaked"
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(REPO_ROOT / "src"), str(REPO_ROOT)])
    result = subprocess.run(
        [sys.executable, "-c", program], capture_output=True, text=True, env=env, check=False
    )
    assert result.returncode == 0, result.stdout + result.stderr


# -- 事实包规范化 ----------------------------------------------------------


def test_bundle_digest_ignores_input_order() -> None:
    facts = [_fact("b", {"v": 2}), _fact("a", {"v": 1})]
    forward = build_bundle(source_id="s", source_type="openapi", facts=facts)
    backward = build_bundle(source_id="s", source_type="openapi", facts=list(reversed(facts)))
    assert forward.digest == backward.digest
    assert [fact.key for fact in forward.facts] == [["a"], ["b"]]


def test_bundle_digest_changes_when_content_changes() -> None:
    base = build_bundle(source_id="s", source_type="openapi", facts=[_fact("a", {"v": 1})])
    other = build_bundle(source_id="s", source_type="openapi", facts=[_fact("a", {"v": 2})])
    assert base.digest != other.digest


def test_sort_key_is_total_for_identical_location() -> None:
    left = _fact("a", {"v": 1}, locator="same")
    right = _fact("a", {"v": 2}, locator="same")
    assert fact_sort_key(left) != fact_sort_key(right)


# -- 加载器 ----------------------------------------------------------------


def test_run_connector_passes_engine_error_through() -> None:
    error = EngineError(ErrorCode.SOURCE_LOAD_FAILED, "boom")
    with pytest.raises(EngineError) as info:
        run_connector(_StubConnector(error=error), _cfg())
    assert info.value is error


def test_run_connector_wraps_unexpected_exception() -> None:
    with pytest.raises(EngineError) as info:
        run_connector(_StubConnector(error=ValueError("bad")), _cfg())
    assert info.value.code == ErrorCode.SOURCE_LOAD_FAILED
    assert isinstance(info.value.__cause__, ValueError)


def test_run_connector_rejects_mismatched_source_identity() -> None:
    bundle = build_bundle(source_id="other", source_type="openapi", facts=[])
    with pytest.raises(EngineError) as info:
        run_connector(_StubConnector(bundle=bundle), _cfg())
    assert info.value.code == ErrorCode.INTERNAL


def test_run_connector_rejects_mismatched_source_type() -> None:
    bundle = build_bundle(source_id="s1", source_type="xlsx", facts=[])
    with pytest.raises(EngineError) as info:
        run_connector(_StubConnector(bundle=bundle), _cfg())
    assert info.value.code == ErrorCode.INTERNAL


def test_probe_never_raises() -> None:
    result = probe_connector(_StubConnector(error=RuntimeError("nope")), _cfg())
    assert result.ok is False
    assert "nope" in result.detail


def test_guard_warnings_land_in_the_bundle(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("SECRET=1\n", encoding="utf-8")
    (tmp_path / "keep.json").write_text("{}", encoding="utf-8")
    cfg = _cfg(root=".")
    guard = SourceGuard(workspace_root=tmp_path)
    bundle = run_connector(_StubConnector(), cfg, guard=guard)
    messages = [warning.message for warning in bundle.warnings]
    assert any(".env" in message for message in messages)
    assert all(warning.fatal is False for warning in bundle.warnings)


# -- 安全校验 --------------------------------------------------------------


@pytest.mark.parametrize("name", [".env", "id_rsa", "cert.pem", "server.key", ".netrc"])
def test_sensitive_names_are_detected(name: str) -> None:
    assert is_sensitive_name(name)


@pytest.mark.parametrize("name", ["data.json", "notes.md", "logo.png"])
def test_ordinary_names_are_not_sensitive(name: str) -> None:
    assert not is_sensitive_name(name)


def test_sensitive_file_is_refused(tmp_path: Path) -> None:
    target = tmp_path / ".env"
    target.write_text("SECRET=1\n", encoding="utf-8")
    with pytest.raises(EngineError) as info:
        check_file(target)
    assert info.value.code == ErrorCode.UPLOAD_TYPE


def test_executable_content_is_refused_regardless_of_whitelist(tmp_path: Path) -> None:
    target = tmp_path / "run.exe"
    target.write_bytes(b"MZ")
    with pytest.raises(EngineError) as info:
        check_file(target, UploadPolicy(allowed_extensions=frozenset({".exe"})))
    assert info.value.code == ErrorCode.UPLOAD_TYPE


def test_macro_document_is_refused(tmp_path: Path) -> None:
    target = tmp_path / "report.docm"
    target.write_bytes(b"PK")
    with pytest.raises(EngineError) as info:
        check_file(target)
    assert info.value.code == ErrorCode.UPLOAD_TYPE


def test_extension_outside_whitelist_is_refused(tmp_path: Path) -> None:
    target = tmp_path / "data.parquet"
    target.write_bytes(b"x")
    with pytest.raises(EngineError) as info:
        check_file(target)
    assert info.value.code == ErrorCode.UPLOAD_TYPE


def test_oversized_file_is_refused(tmp_path: Path) -> None:
    target = tmp_path / "big.json"
    target.write_bytes(b"{}")
    with pytest.raises(EngineError) as info:
        check_file(target, UploadPolicy(max_file_bytes=1))
    assert info.value.code == ErrorCode.UPLOAD_SIZE


def _zip(path: Path, entries: dict[str, bytes]) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        for name, payload in entries.items():
            archive.writestr(name, payload)
    return path


def test_archive_entry_escaping_is_refused(tmp_path: Path) -> None:
    target = _zip(tmp_path / "a.zip", {"../evil.json": b"{}"})
    with pytest.raises(EngineError) as info:
        inspect_archive(target)
    assert info.value.code == ErrorCode.PATH_ESCAPE


def test_archive_absolute_entry_is_refused(tmp_path: Path) -> None:
    target = _zip(tmp_path / "b.zip", {"/abs.json": b"{}"})
    with pytest.raises(EngineError) as info:
        inspect_archive(target)
    assert info.value.code == ErrorCode.PATH_ESCAPE


def test_archive_executable_member_is_refused(tmp_path: Path) -> None:
    target = _zip(tmp_path / "c.zip", {"payload/run.sh": b"echo"})
    with pytest.raises(EngineError) as info:
        inspect_archive(target)
    assert info.value.code == ErrorCode.UPLOAD_TYPE


def test_archive_macro_payload_is_refused(tmp_path: Path) -> None:
    target = _zip(tmp_path / "d.docx", {"word/document.xml": b"<x/>", "word/vbaProject.bin": b"m"})
    with pytest.raises(EngineError) as info:
        check_file(target)
    assert info.value.code == ErrorCode.UPLOAD_TYPE


def test_archive_entry_count_limit(tmp_path: Path) -> None:
    target = _zip(tmp_path / "e.zip", {f"{index}.json": b"{}" for index in range(5)})
    with pytest.raises(EngineError) as info:
        inspect_archive(target, UploadPolicy(max_archive_entries=2))
    assert info.value.code == ErrorCode.UPLOAD_SIZE


def test_archive_uncompressed_size_limit(tmp_path: Path) -> None:
    target = _zip(tmp_path / "f.zip", {"a.json": b"1234567890"})
    with pytest.raises(EngineError) as info:
        inspect_archive(target, UploadPolicy(max_uncompressed_bytes=4))
    assert info.value.code == ErrorCode.UPLOAD_SIZE


def test_non_archive_is_refused_as_archive(tmp_path: Path) -> None:
    target = tmp_path / "g.zip"
    target.write_bytes(b"not a zip")
    with pytest.raises(EngineError) as info:
        inspect_archive(target)
    assert info.value.code == ErrorCode.UPLOAD_TYPE


def test_check_tree_skips_secrets_and_reports(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("SECRET=1\n", encoding="utf-8")
    (tmp_path / "a.json").write_text("{}", encoding="utf-8")
    (tmp_path / "nested").mkdir()
    (tmp_path / "nested" / "b.py").write_text("x = 1\n", encoding="utf-8")
    kept, warnings = check_tree(tmp_path)
    assert [path.name for path in kept] == ["a.json", "b.py"]
    assert len(warnings) == 1 and ".env" in warnings[0]


def test_check_tree_refuses_executables(tmp_path: Path) -> None:
    (tmp_path / "tool.exe").write_bytes(b"MZ")
    with pytest.raises(EngineError) as info:
        check_tree(tmp_path)
    assert info.value.code == ErrorCode.UPLOAD_TYPE


def test_source_guard_refuses_path_escape(tmp_path: Path) -> None:
    guard = SourceGuard(workspace_root=tmp_path)
    with pytest.raises(EngineError) as info:
        guard.check(_cfg(root="../outside"))
    assert info.value.code == ErrorCode.PATH_ESCAPE


def test_source_guard_ignores_config_without_root(tmp_path: Path) -> None:
    guard = SourceGuard(workspace_root=tmp_path)
    target, warnings = guard.check(_cfg())
    assert target is None
    assert warnings == []
