#!/usr/bin/env python3
"""内核零 Web / 零 DB 依赖检查（决策四 / 验收 32）。

规则：`src/docfidelity/**` 不得引入任何 Web 框架、ASGI 服务器、DB 驱动或 server 包。
另附一条：内核代码禁止 `import random`（DD-2.4 —— 随机性不得参与逻辑，时间戳只进元数据；
需要唯一标识时用 uuid / 稳定哈希）。

用法：
    python scripts/check_kernel_no_web.py
退出码：0 通过；1 违规。
"""

from __future__ import annotations

import ast
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
KERNEL_ROOT = REPO_ROOT / "src" / "docfidelity"

FORBIDDEN_MODULES = frozenset(
    {
        "fastapi",
        "starlette",
        "flask",
        "uvicorn",
        "sqlalchemy",
        "alembic",
        "sse_starlette",
        "pydantic_settings",
        "server",
        "random",
    }
)

EXCLUDE_PARTS = frozenset({".git", "__pycache__", ".venv", ".mypy_cache", ".ruff_cache"})


@dataclass(frozen=True)
class Violation:
    path: str
    line: int
    module: str


def _top_level(module_name: str) -> str:
    return module_name.split(".", 1)[0]


def _iter_kernel_sources() -> list[Path]:
    if not KERNEL_ROOT.exists():
        return []
    return [p for p in KERNEL_ROOT.rglob("*.py") if EXCLUDE_PARTS.isdisjoint(p.parts)]


def _violations_in(tree: ast.AST) -> list[tuple[int, str]]:
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = _top_level(alias.name)
                if top in FORBIDDEN_MODULES:
                    found.append((node.lineno, top))
        elif isinstance(node, ast.ImportFrom):
            if node.level and node.level > 0:
                continue  # 相对导入不出内核
            if node.module:
                top = _top_level(node.module)
                if top in FORBIDDEN_MODULES:
                    found.append((node.lineno, top))
    return found


def main() -> int:
    violations: list[Violation] = []
    for file in _iter_kernel_sources():
        rel = file.relative_to(REPO_ROOT).as_posix()
        try:
            tree = ast.parse(file.read_text(encoding="utf-8"), filename=rel)
        except SyntaxError as exc:
            violations.append(Violation(rel, exc.lineno or 0, f"syntax_error: {exc}"))
            continue
        for lineno, module in _violations_in(tree):
            violations.append(Violation(rel, lineno, module))

    if violations:
        print(f"FAIL 内核发现 {len(violations)} 处越界依赖（决策四）：")
        for item in sorted(violations, key=lambda v: (v.path, v.line)):
            print(f"  {item.path}:{item.line}  import {item.module}")
        print("处置：内核只依赖 contracts / platform / 标准库；Web 与 DB 归 server 包。")
        return 1

    print("OK 内核零 Web / 零 DB 依赖检查通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
