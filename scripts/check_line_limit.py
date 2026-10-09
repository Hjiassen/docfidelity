#!/usr/bin/env python3
"""单文件行数上限检查。

规则来源：NFR-09 —— 单文件不得超过 800 行，超出必须按职责拆分。
用法：
    python scripts/check_line_limit.py [路径 ...]
不传路径时扫描仓库默认源码目录。
"""

from __future__ import annotations

import sys
from collections.abc import Iterable, Iterator
from pathlib import Path

MAX_LINES = 800

EXCLUDE_PARTS = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "__pycache__",
        "build",
        "dist",
        ".mypy_cache",
        ".ruff_cache",
        ".pytest_cache",
        "node_modules",
        ".idea",
        ".workbuddy",
    }
)

DEFAULT_ROOTS = ("src", "server", "scripts", "tests")


def _iter_py_files(roots: Iterable[str]) -> Iterator[Path]:
    for root in roots:
        path = Path(root)
        if not path.exists():
            continue
        candidates: Iterator[Path]
        if path.is_file():
            candidates = iter([path]) if path.suffix == ".py" else iter(())
        else:
            candidates = (p for p in path.rglob("*.py") if p.is_file())
        for file in candidates:
            if EXCLUDE_PARTS.isdisjoint(file.parts):
                yield file


def _count_lines(file: Path) -> int:
    # 以二进制读取，避免不同换行风格导致的计数偏差；末行无换行符也要计入。
    data = file.read_bytes()
    if not data:
        return 0
    newlines = data.count(b"\n")
    return newlines if data.endswith(b"\n") else newlines + 1


def main(argv: list[str]) -> int:
    roots = argv[1:] or list(DEFAULT_ROOTS)
    offenders: list[tuple[Path, int]] = []

    for file in _iter_py_files(roots):
        lines = _count_lines(file)
        if lines > MAX_LINES:
            offenders.append((file.as_posix(), lines))

    if offenders:
        print(f"FAIL 单文件行数上限 {MAX_LINES}（NFR-09）：")
        for path, lines in sorted(offenders, key=lambda item: -item[1]):
            print(f"  {path}: {lines} 行（超出 {lines - MAX_LINES} 行）")
        print("处置：按职责拆分为多个模块，保持单文件 <= 800 行。")
        return 1

    print(f"OK 单文件行数上限检查通过（<= {MAX_LINES} 行）")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
