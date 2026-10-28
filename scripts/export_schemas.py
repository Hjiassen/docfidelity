#!/usr/bin/env python3
"""导出契约层 JSON Schema（DD-3.1）。

契约层是对外承诺的唯一载体，因此其 schema 必须可机器校验、可交付给前端
（前端用同一份 schema 做表单校验，禁止手写重复类型）。

用法::

    python scripts/export_schemas.py            # 生成并写盘
    python scripts/export_schemas.py --check    # 只校验，不写盘；不一致则退出 1

输出：``docs/spec/<family>/schema.json``
"""

from __future__ import annotations

import argparse
import importlib
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = REPO_ROOT / "src"
OUTPUT_ROOT = REPO_ROOT / "docs" / "spec"

if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from pydantic import BaseModel  # noqa: E402
from pydantic.json_schema import models_json_schema  # noqa: E402

from docfidelity.contracts.base import FormatModel  # noqa: E402

SCHEMA_DIALECT = "https://json-schema.org/draft/2020-12/schema"

#: 输出目录名 -> 契约模块。顺序即输出顺序。
FAMILY_MODULES: dict[str, str] = {
    "template": "docfidelity.contracts.template",
    "content-ir": "docfidelity.contracts.ir",
    "domain": "docfidelity.contracts.domain",
    "profile": "docfidelity.contracts.profile",
    "fact": "docfidelity.contracts.fact",
    "job": "docfidelity.contracts.job",
    "qa": "docfidelity.contracts.qa",
    "config": "docfidelity.contracts.config",
    "effective-template": "docfidelity.contracts.imports",
    "materials": "docfidelity.contracts.materials",
    "documents": "docfidelity.contracts.documents",
    "generation": "docfidelity.contracts.generation",
}


def _models_of(module: ModuleType) -> list[type[BaseModel]]:
    """收集模块内**定义于本模块**的契约模型，按名称排序以保证输出稳定。"""
    collected: list[type[BaseModel]] = []
    for obj in vars(module).values():
        if (
            isinstance(obj, type)
            and issubclass(obj, FormatModel)
            and obj is not FormatModel
            and obj.__module__ == module.__name__
        ):
            collected.append(obj)
    return sorted(collected, key=lambda model: model.__name__)


def _build_document(family: str, models: list[type[BaseModel]]) -> dict[str, Any]:
    if not models:
        raise RuntimeError(f"契约家族 {family} 未收集到任何模型，schema 导出中止")

    _, core = models_json_schema(
        [(model, "validation") for model in models],
        title=f"docfidelity contracts: {family}",
    )

    # 固定 $schema 方言，避免 pydantic 版本升级导致输出漂移
    core["$schema"] = SCHEMA_DIALECT
    core["x-contract-family"] = family
    core["x-contract-models"] = [model.__name__ for model in models]
    return core


def _serialize(document: dict[str, Any]) -> str:
    return json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _target_path(family: str) -> Path:
    return OUTPUT_ROOT / family / "schema.json"


def generate() -> dict[str, str]:
    """生成全部家族的 schema 文本，返回 {相对路径: 内容}。"""
    outputs: dict[str, str] = {}
    for family, module_path in FAMILY_MODULES.items():
        module = importlib.import_module(module_path)
        models = _models_of(module)
        document = _build_document(family, models)
        relative = _target_path(family).relative_to(REPO_ROOT).as_posix()
        outputs[relative] = _serialize(document)
    return outputs


def _run_write(outputs: dict[str, str]) -> int:
    for relative, content in outputs.items():
        path = REPO_ROOT / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        print(f"written  {relative}")
    return 0


def _run_check(outputs: dict[str, str]) -> int:
    stale: list[str] = []
    missing: list[str] = []
    for relative, content in outputs.items():
        path = REPO_ROOT / relative
        if not path.exists():
            missing.append(relative)
            continue
        if path.read_text(encoding="utf-8") != content:
            stale.append(relative)

    if not stale and not missing:
        print(f"OK 契约 Schema 与模型一致（{len(outputs)} 个家族）")
        return 0

    for relative in missing:
        print(f"MISSING  {relative}")
    for relative in stale:
        print(f"STALE    {relative}")
    print("处置：运行 `python scripts/export_schemas.py` 重新生成并提交。")
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="导出契约层 JSON Schema")
    parser.add_argument("--check", action="store_true", help="只校验一致性，不写盘")
    args = parser.parse_args(argv)

    outputs = generate()
    return _run_check(outputs) if args.check else _run_write(outputs)


if __name__ == "__main__":
    sys.exit(main())
