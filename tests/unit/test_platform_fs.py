"""``platform.fs`` 单测（DD-2.1；NFR-08 / 10.11）。

两条线各有一组"防假通过"断言：

1. 越界拦截必须真的拦住——包括**字符串前缀检查会放过**的两种情况
   （``out-evil`` 兄弟目录、``root/../secret``）。
2. 原子落盘必须真的不残留临时文件——只断言"目标文件内容对"是拦不住
   "写了一半被杀"的。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from docfidelity.contracts.errors import EngineError, ErrorCode
from docfidelity.platform.fs import (
    atomic_write_bytes,
    atomic_write_text,
    ensure_dir,
    ensure_within,
    iter_files,
    read_bytes,
    read_text,
    relative_posix,
    safe_join,
)

pytestmark = pytest.mark.unit


class TestSafeJoin:
    def test_nested_relative_path_is_allowed(self, tmp_path: Path) -> None:
        target = safe_join(tmp_path, "contract", "styles.json")
        assert target == (tmp_path / "contract" / "styles.json").resolve()

    def test_inner_dotdot_that_resolves_inside_is_allowed(self, tmp_path: Path) -> None:
        """``a/../b`` 解析后仍在根内，拦截它是误伤。"""
        assert safe_join(tmp_path, "a/../b") == (tmp_path / "b").resolve()

    def test_root_itself_is_allowed(self, tmp_path: Path) -> None:
        assert safe_join(tmp_path) == tmp_path.resolve()

    def test_escaping_dotdot_is_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(EngineError) as info:
            safe_join(tmp_path, "../secret.txt")
        assert info.value.code == ErrorCode.PATH_ESCAPE

    def test_deep_escape_is_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(EngineError) as info:
            safe_join(tmp_path, "a/b/../../..")
        assert info.value.code == ErrorCode.PATH_ESCAPE

    def test_sibling_with_shared_prefix_is_rejected(self, tmp_path: Path) -> None:
        """字符串前缀检查会放过 ``out-evil``——它 ``startswith('out')`` 为真。"""
        root = tmp_path / "out"
        ensure_dir(root)
        sibling = tmp_path / "out-evil"
        ensure_dir(sibling)
        with pytest.raises(EngineError) as info:
            ensure_within(root, sibling / "x.txt")
        assert info.value.code == ErrorCode.PATH_ESCAPE

    def test_error_carries_actionable_remediation(self, tmp_path: Path) -> None:
        with pytest.raises(EngineError) as info:
            safe_join(tmp_path, "../x")
        assert info.value.remediation, "错误必须带可执行动作，界面据此渲染"
        assert info.value.fatal is True


class TestEnsureWithin:
    def test_inside_path_returned_resolved(self, tmp_path: Path) -> None:
        nested = tmp_path / "a" / "b"
        assert ensure_within(tmp_path, nested) == nested.resolve()

    def test_nonexistent_inside_path_is_allowed(self, tmp_path: Path) -> None:
        """尚未创建的文件也要能通过校验——写入前它就是不存在。"""
        assert ensure_within(tmp_path, tmp_path / "new" / "f.json").name == "f.json"


class TestAtomicWrite:
    def test_bytes_round_trip(self, tmp_path: Path) -> None:
        target = tmp_path / "deep" / "payload.bin"
        atomic_write_bytes(target, b"\x00\x01\x02")
        assert read_bytes(target) == b"\x00\x01\x02"

    def test_text_round_trip_preserves_newlines(self, tmp_path: Path) -> None:
        """不做平台换行转换——产物必须逐字节可复现。"""
        target = tmp_path / "note.txt"
        atomic_write_text(target, "a\nb\n")
        assert target.read_bytes() == b"a\nb\n"

    def test_no_temp_file_is_left_behind(self, tmp_path: Path) -> None:
        atomic_write_bytes(tmp_path / "f.bin", b"x")
        leftovers = [p.name for p in tmp_path.iterdir() if p.name.endswith(".dftmp")]
        assert leftovers == []

    def test_overwrite_replaces_content_atomically(self, tmp_path: Path) -> None:
        target = tmp_path / "f.bin"
        atomic_write_bytes(target, b"first-longer-content")
        atomic_write_bytes(target, b"second")
        assert read_bytes(target) == b"second"
        assert [p.name for p in tmp_path.iterdir() if p.name.endswith(".dftmp")] == []

    def test_temp_file_failure_cleans_up(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """写入过程失败时不得留下临时文件，否则目录会被垃圾塞满。"""
        import docfidelity.platform.fs as module

        def explode(*_args: object, **_kwargs: object) -> None:
            raise OSError("disk full")

        monkeypatch.setattr(module.os, "replace", explode)
        with pytest.raises(OSError):
            atomic_write_bytes(tmp_path / "f.bin", b"x")
        assert [p.name for p in tmp_path.iterdir() if p.name.endswith(".dftmp")] == []

    def test_parent_directories_are_created(self, tmp_path: Path) -> None:
        target = tmp_path / "a" / "b" / "c" / "f.bin"
        atomic_write_bytes(target, b"x")
        assert target.is_file()


class TestIterFiles:
    def test_sorted_by_relative_path(self, tmp_path: Path) -> None:
        for name in ("b.txt", "a.txt", "c.txt"):
            (tmp_path / name).write_text("x")
        assert [p.name for p in iter_files(tmp_path)] == ["a.txt", "b.txt", "c.txt"]

    def test_suffix_filter_is_case_insensitive(self, tmp_path: Path) -> None:
        (tmp_path / "a.JSON").write_text("{}")
        (tmp_path / "b.txt").write_text("x")
        assert [p.name for p in iter_files(tmp_path, suffixes=(".json",))] == ["a.JSON"]

    def test_missing_directory_yields_nothing(self, tmp_path: Path) -> None:
        assert list(iter_files(tmp_path / "absent")) == []

    def test_directories_are_not_yielded(self, tmp_path: Path) -> None:
        ensure_dir(tmp_path / "sub")
        (tmp_path / "sub" / "f.txt").write_text("x")
        assert [p.name for p in iter_files(tmp_path)] == ["f.txt"]


class TestRelativePosix:
    def test_uses_forward_slashes(self, tmp_path: Path) -> None:
        """包内条目名必须跨平台一致，否则摘要会随操作系统变化。"""
        nested = tmp_path / "contract" / "styles.json"
        assert relative_posix(nested, tmp_path) == "contract/styles.json"


class TestEnsureDir:
    def test_idempotent(self, tmp_path: Path) -> None:
        first = ensure_dir(tmp_path / "x")
        second = ensure_dir(tmp_path / "x")
        assert first == second == tmp_path / "x"


class TestReadHelpers:
    def test_read_text_default_encoding(self, tmp_path: Path) -> None:
        target = tmp_path / "cn.txt"
        target.write_text("模板保真", encoding="utf-8")
        assert read_text(target) == "模板保真"
