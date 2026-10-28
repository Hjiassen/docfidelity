"""V2 本地不可变记录与原子提交指针。"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from docfidelity.contracts.errors import EngineError, ErrorCode
from docfidelity.platform.fs import atomic_write_text, safe_join

from .template_import_io import import_lock
from .template_imports import _segment

_TRANSACTIONS = threading.local()


@contextmanager
def transaction_lock(path: Path) -> Iterator[None]:
    """同线程嵌套复用系统锁，跨线程和进程仍通过文件锁串行。"""
    active: set[str] = getattr(_TRANSACTIONS, "active", set())
    key = str(path.resolve())
    if key in active:
        yield
        return
    with import_lock(path):
        _TRANSACTIONS.active = active | {key}
        try:
            yield
        finally:
            _TRANSACTIONS.active = active


class Records:
    """只有 current 历史中存在的修订才对读者可见。"""

    def __init__(self, root: Path, family: str) -> None:
        self.root = root / family
        self.root.mkdir(parents=True, exist_ok=True)

    def directory(self, key: str) -> Path:
        return safe_join(self.root, _segment(key))

    def history(self, key: str) -> list[int]:
        try:
            result = json.loads((self.directory(key) / "current.json").read_text(encoding="utf-8"))
            if (
                not isinstance(result, list)
                or not result
                or any(type(v) is not int or v < 1 for v in result)
            ):
                raise ValueError("非法提交指针")
            return result
        except FileNotFoundError as exc:
            raise EngineError(ErrorCode.NOT_FOUND, "记录不存在或尚未提交") from exc
        except (OSError, ValueError, TypeError) as exc:
            raise EngineError(ErrorCode.PACKAGE_INCOMPLETE, "提交指针损坏") from exc

    def read(self, key: str, revision: int | None = None) -> dict[str, Any]:
        history = self.history(key)
        number = history[-1] if revision is None else revision
        if number not in history:
            raise EngineError(ErrorCode.NOT_FOUND, "修订不存在或尚未提交")
        try:
            value = json.loads(
                (self.directory(key) / "revisions" / f"{number}.json").read_text(encoding="utf-8")
            )
            if not isinstance(value, dict):
                raise ValueError("记录不是对象")
            return value
        except (OSError, ValueError) as exc:
            raise EngineError(ErrorCode.PACKAGE_INCOMPLETE, "提交记录损坏") from exc

    def next_revision(self, key: str) -> int:
        occupied = [
            int(p.stem)
            for p in (self.directory(key) / "revisions").glob("*.json")
            if p.stem.isdigit()
        ]
        return max(occupied, default=0) + 1

    def commit(self, key: str, value: dict[str, Any], *, revision: int | None = None) -> int:
        try:
            history = self.history(key)
        except EngineError as exc:
            if exc.code != ErrorCode.NOT_FOUND:
                raise
            history = []
        number = self.next_revision(key) if revision is None else revision
        target = self.directory(key) / "revisions" / f"{number}.json"
        if target.exists():
            raise EngineError(ErrorCode.VERSION_CONFLICT, "不可变修订已存在")
        try:
            atomic_write_text(target, json.dumps(value, ensure_ascii=False))
            atomic_write_text(self.directory(key) / "current.json", json.dumps([*history, number]))
        except OSError as exc:
            raise EngineError(ErrorCode.INTERNAL, "记录提交失败") from exc
        return number

    def identifiers(self) -> list[str]:
        return sorted(p.parent.name for p in self.root.glob("*/current.json"))
