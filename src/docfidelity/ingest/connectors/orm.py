"""SQLAlchemy ORM 与 Alembic 迁移接入（FR-M1-06 / DD-5.2）。

输入：ORM 模型定义源码 + Alembic 迁移脚本。
抽取：表名、中文名、字段清单、类型、约束。

与 ``ast`` 接入器共用同一条铁律：**只 ``ast.parse``，绝不 import**（FR-M1-05）。

DD-5.2 点名的陷阱是"中文名常来自 ``comment``"。本接入器把回退链**显式**产出来：
``comment``（注解）→ ``label``（注解为空时退到类名 / 表名）。只发 ``comment`` 的话，
缺注解的表在后续章节里会变成一行空白标题，而"没有中文名"这件事在产物上看不出来。
"""

from __future__ import annotations

import ast
from typing import Any, ClassVar

from docfidelity.contracts.fact import FactBundle, SourceConfig
from docfidelity.platform.fs import relative_posix

from ..emitter import FactEmitter
from ..protocol import ProbeResult
from ._support import (
    call_name,
    docstring_of,
    keyword_map,
    literal_str,
    option_str_list,
    parse_python,
    root_path,
    short_name,
    source_files,
    unparse,
)

#: 默认认作列构造器的调用名末段。
DEFAULT_COLUMN_CALLS: tuple[str, ...] = ("Column", "mapped_column")

#: Alembic 入口对象名（``op.create_table``）。
DEFAULT_MIGRATION_OP: str = "op"

_TABLE_NAME_ATTR = "__tablename__"

_TABLE_ARGS_ATTR = "__table_args__"


def _string_kwarg(kwargs: dict[str, ast.AST], name: str) -> str:
    return literal_str(kwargs.get(name)) or ""


def _boolean_kwarg(kwargs: dict[str, ast.AST], name: str, default: bool) -> bool:
    node = kwargs.get(name)
    if isinstance(node, ast.Constant) and isinstance(node.value, bool):
        return node.value
    return default


def _column_entry(node: ast.Assign | ast.AnnAssign, calls: set[str]) -> dict[str, Any] | None:
    """把一个 ``x = Column(...)`` 语句读成一列。"""
    if isinstance(node, ast.AnnAssign):
        target: ast.expr = node.target
    else:
        names = [item for item in node.targets if isinstance(item, ast.Name)]
        if len(names) != 1:
            return None  # 链式赋值 / 元组解包：静态上说不清是哪一列
        target = names[0]
    if not isinstance(target, ast.Name):
        return None
    value = node.value
    if not isinstance(value, ast.Call) or short_name(call_name(value.func)) not in calls:
        return None
    kwargs = keyword_map(value)
    type_text = unparse(value.args[0]) if value.args else ""
    return {
        "name": target.id,
        "type": type_text,
        "nullable": _boolean_kwarg(kwargs, "nullable", True),
        "primary_key": _boolean_kwarg(kwargs, "primary_key", False),
        "unique": _boolean_kwarg(kwargs, "unique", False),
        "comment": _string_kwarg(kwargs, "comment"),
    }


def _class_columns(class_node: ast.ClassDef, calls: set[str]) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for statement in class_node.body:
        if isinstance(statement, ast.Assign | ast.AnnAssign):
            entry = _column_entry(statement, calls)
            if entry is not None:
                entries.append(entry)
    return entries


def _dict_comment(node: ast.AST | None) -> str:
    """从 ``{"comment": "..."}`` 里读出注释文本；不是该形状返回空串。"""
    if not isinstance(node, ast.Dict):
        return ""
    for key, value in zip(node.keys, node.values, strict=False):
        if literal_str(key) == "comment":
            return literal_str(value) or ""
    return ""


def _table_label(class_node: ast.ClassDef) -> str:
    """取表的中文名，来源是 ``__table_args__ = {"comment": ...}``。

    DD-5.2 点名的陷阱就在这里：中文名常**只**写在 ``__table_args__`` 的 ``comment`` 里，
    而类 docstring 往往是空的或英文的。只看 docstring 会把"订单记录表"丢掉，
    在产物上表现为一行空白标题，且"没有中文名"这件事在结果里看不出来。
    回退链由调用方接上（本函数为空时才退到 docstring / 类名）。
    """
    for statement in class_node.body:
        if not isinstance(statement, ast.Assign):
            continue
        targets = [item for item in statement.targets if isinstance(item, ast.Name)]
        if not any(item.id == _TABLE_ARGS_ATTR for item in targets):
            continue
        value = statement.value
        if isinstance(value, ast.Dict):
            return _dict_comment(value)
        if isinstance(value, ast.Tuple):  # __table_args__ = ({"comment": ...}, ...)
            for item in value.elts:
                comment = _dict_comment(item)
                if comment:
                    return comment
        return ""
    return ""


def _table_declaration(class_node: ast.ClassDef, calls: set[str]) -> tuple[str, str] | None:
    """取 (表名, 中文名)，找不到 ``__tablename__`` 返回 ``None``。

    中文名的回退链：``__table_args__["comment"]`` → 类 docstring（类名兜底在调用方）。
    """
    for statement in class_node.body:
        if not isinstance(statement, ast.Assign):
            continue
        targets = [item for item in statement.targets if isinstance(item, ast.Name)]
        if not any(item.id == _TABLE_NAME_ATTR for item in targets):
            continue
        name = literal_str(statement.value)
        if name is None:
            continue
        return name, _table_label(class_node) or docstring_of(class_node)
    return None


def _call_columns(call: ast.Call, calls: set[str]) -> list[dict[str, Any]]:
    """把 ``op.create_table(..., Column(...), ...)`` 的位置参数读成列。"""
    entries: list[dict[str, Any]] = []
    for argument in call.args:
        if not isinstance(argument, ast.Call):
            continue
        if short_name(call_name(argument.func)) not in calls:
            continue
        kwargs = keyword_map(argument)
        name = literal_str(argument.args[0]) if argument.args else None
        type_text = unparse(argument.args[1]) if len(argument.args) > 1 else ""
        entries.append(
            {
                "name": name or "",
                "type": type_text,
                "nullable": _boolean_kwarg(kwargs, "nullable", True),
                "primary_key": _boolean_kwarg(kwargs, "primary_key", False),
                "unique": _boolean_kwarg(kwargs, "unique", False),
                "comment": _string_kwarg(kwargs, "comment"),
            }
        )
    return entries


def _migration_tables(
    tree: ast.Module, op_name: str, calls: set[str]
) -> list[tuple[str, int, list[dict[str, Any]]]]:
    """扫出 ``op.create_table`` 调用。"""
    found: list[tuple[str, int, list[dict[str, Any]]]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute) or func.attr != "create_table":
            continue
        if call_name(func.value) != op_name:
            continue
        name = literal_str(node.args[0]) if node.args else None
        if name is None:
            continue
        found.append((name, node.lineno, _call_columns(node, calls)))
    found.sort(key=lambda item: (item[0], item[1]))
    return found


class OrmConnector:
    """ORM 模型与迁移脚本接入器。"""

    name: ClassVar[str] = "orm"

    def _settings(self, cfg: SourceConfig) -> tuple[set[str], str]:
        calls = {item for item in option_str_list(cfg, "column_calls", (*DEFAULT_COLUMN_CALLS,))}
        return calls, option_str_list(cfg, "migration_op", (DEFAULT_MIGRATION_OP,))[0]

    def _declarations(
        self, cfg: SourceConfig
    ) -> tuple[list[tuple[str, str, int, list[dict[str, Any]], str, str]], list[str]]:
        """产出 (表名, 注解, 行号, 列, 相对路径, 来源形态) 序列。"""
        calls, op_name = self._settings(cfg)
        root = root_path(cfg)
        items: list[tuple[str, str, int, list[dict[str, Any]], str, str]] = []
        warnings: list[str] = []
        for path in source_files(cfg, (".py",)):
            relative = relative_posix(path, root) if root.is_dir() else path.name
            tree = parse_python(path)
            if tree is None:
                warnings.append(f"源码解析失败，已跳过：{relative}")
                continue
            for statement in tree.body:
                if not isinstance(statement, ast.ClassDef):
                    continue
                declaration = _table_declaration(statement, calls)
                if declaration is None:
                    continue
                table_name, comment = declaration
                items.append(
                    (
                        table_name,
                        comment or statement.name,
                        statement.lineno,
                        _class_columns(statement, calls),
                        relative,
                        "model",
                    )
                )
            for table_name, lineno, columns in _migration_tables(tree, op_name, calls):
                items.append((table_name, table_name, lineno, columns, relative, "migration"))
        return items, warnings

    def probe(self, cfg: SourceConfig) -> ProbeResult:
        items, warnings = self._declarations(cfg)
        return ProbeResult(
            ok=True,
            detail=f"识别出 {len(items)} 张表声明",
            item_count=len(items),
            warnings=tuple(warnings),
        )

    def load(self, cfg: SourceConfig) -> FactBundle:
        emitter = FactEmitter(cfg)
        items, warnings = self._declarations(cfg)
        for message in warnings:
            emitter.warn(message)
        for table_name, label, lineno, columns, relative, origin in items:
            emitter.emit(
                "table",
                {
                    "table": table_name,
                    "label": label,
                    "origin": origin,
                    "source_file": relative,
                    "line": lineno,
                    "column_count": len(columns),
                    "columns": columns,
                },
                locator=f"{relative}:{lineno}",
            )
        if not items and not warnings:
            emitter.warn(
                "未识别到任何表声明：检查 options.column_calls / migration_op 是否与项目约定一致"
            )
        return emitter.bundle()


__all__ = ["DEFAULT_COLUMN_CALLS", "DEFAULT_MIGRATION_OP", "OrmConnector"]
