"""接入器的公共装配件（配置取值、根路径解析、AST 文本提取）。

为什么要单独一层：五个接入器都要做同样四件事——解析 ``root``、按后缀遍历、读
``options``、把 AST 节点还原成源码文本。各写一份的后果是同一份配置在五个地方被
解释，其中一处忘了校验类型，错误的配置就会静默变成空事实包。

这里的所有取值函数都在**类型不符时直接抛错**：``options`` 是外部数据，
"给了个字符串却期望列表"必须当场发现，不能靠后面某处 ``for`` 循环悄悄退化成不循环。
"""

from __future__ import annotations

import ast
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any, TypeVar, cast

from docfidelity.contracts.errors import ConfigurationError
from docfidelity.contracts.fact import SourceConfig
from docfidelity.platform.fs import iter_files

T = TypeVar("T")

#: 可以阅读 docstring 的 AST 节点类型。
DocstringNode = ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef


def _bad_option(cfg: SourceConfig, name: str, expected: str, actual: Any) -> ConfigurationError:
    return ConfigurationError(
        f"事实源 {cfg.source_id} 的 options.{name} 类型不符：期望 {expected}，"
        f"实际 {type(actual).__name__}",
        where={"source": cfg.source_id, "option": name, "actual": type(actual).__name__},
        remediation=["修正 fact-sources.yaml 中该来源的 options 取值"],
    )


def option(cfg: SourceConfig, name: str, default: T) -> T:
    """取 ``options`` 里的标量值；未给出时返回默认值，类型不符即抛错。

    只支持 ``bool`` / ``str`` / ``int`` 三种默认值类型——列表型走
    :func:`option_str_list`。默认值类型不在支持范围属调用方编程错误。
    """
    if name not in cfg.options:
        return default
    value: Any = cfg.options[name]
    if isinstance(default, bool):
        if isinstance(value, bool):
            return cast(T, value)
        raise _bad_option(cfg, name, "bool", value)
    if isinstance(default, str):
        if isinstance(value, str):
            return cast(T, value)
        raise _bad_option(cfg, name, "str", value)
    if isinstance(default, int):
        if isinstance(value, int) and not isinstance(value, bool):
            return cast(T, value)
        raise _bad_option(cfg, name, "int", value)
    raise ConfigurationError(
        f"事实源 {cfg.source_id} 的 options.{name} 默认值类型不受支持",
        where={"source": cfg.source_id, "option": name},
        remediation=["改用 option_str_list，或把默认值改为 bool / str / int"],
    )


def option_str_list(cfg: SourceConfig, name: str, default: Sequence[str]) -> list[str]:
    """取字符串列表型选项。"""
    if name not in cfg.options:
        return list(default)
    value: Any = cfg.options[name]
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise _bad_option(cfg, name, "list[str]", value)
    return list(value)


def root_path(cfg: SourceConfig) -> Path:
    """解析来源根路径；未配置或不存在即抛错。

    **不返回 ``None``**：把"没配路径"与"路径下没有文件"混为一谈，会让一个漏配的
    来源产出空事实包并顺利通过所有下游检查。
    """
    if cfg.root is None:
        raise ConfigurationError(
            f"事实源 {cfg.source_id} 未配置 root",
            where={"source": cfg.source_id},
            remediation=["在 fact-sources.yaml 中为该来源补上 root（相对工作区根）"],
        )
    path = Path(cfg.root)
    if not path.exists():
        raise ConfigurationError(
            f"事实源 {cfg.source_id} 的 root 不存在：{cfg.root}",
            where={"source": cfg.source_id, "root": cfg.root},
            remediation=["检查路径拼写，或先上传该来源"],
        )
    return path


def source_files(cfg: SourceConfig, suffixes: Sequence[str]) -> list[Path]:
    """按后缀列出来源文件（相对路径升序，顺序由 :func:`platform.fs.iter_files` 保证）。"""
    root = root_path(cfg)
    wanted = {suffix.lower() for suffix in suffixes}
    if root.is_file():
        return [root] if root.suffix.lower() in wanted else []
    return list(iter_files(root, suffixes=suffixes))


def read_json(path: Path) -> Any:
    """读 JSON；格式或编码错误即抛错（带定位）。"""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigurationError(
            f"JSON 解析失败：{path.name} 第 {exc.lineno} 行第 {exc.colno} 列",
            where={"file": path.name, "line": exc.lineno, "column": exc.colno},
            remediation=["修正该 JSON 文件后重试"],
        ) from exc
    except UnicodeDecodeError as exc:
        raise ConfigurationError(
            f"文件编码不是 UTF-8：{path.name}",
            where={"file": path.name},
            remediation=["把该文件转成 UTF-8 编码后重试"],
        ) from exc


def parse_python(path: Path) -> ast.Module | None:
    """``ast.parse`` 一个源文件；语法错误或编码错误返回 ``None``（调用方记告警）。

    **绝不 import 被接入的模块**（FR-M1-05）：导入会连库、执行模块级代码、
    触发副作用。解析源码只需要 ``ast``。
    """
    try:
        return ast.parse(path.read_text(encoding="utf-8"), filename=path.name)
    except (SyntaxError, UnicodeDecodeError):
        return None


def unparse(node: ast.AST | None) -> str:
    """把 AST 节点还原成源码文本（用于把类型标注读成文本）。"""
    if node is None:
        return ""
    try:
        return ast.unparse(node)
    except (AttributeError, ValueError):  # pragma: no cover - 只在畸形节点上触发
        return ""


def call_name(node: ast.AST) -> str:
    """取调用 / 属性表达式的点分名（``sa.Column`` → ``sa.Column``，``Column`` → ``Column``）。"""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = call_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    return ""


def short_name(dotted: str) -> str:
    """取点分名的末段（``sa.Column`` → ``Column``）。"""
    return dotted.rsplit(".", 1)[-1]


def literal_str(node: ast.AST | None) -> str | None:
    """节点若是字符串常量则返回其值。"""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def keyword_map(node: ast.Call) -> dict[str, ast.AST]:
    """调用的关键字参数表（``**kwargs`` 展开会被忽略，其键在静态上不可知）。"""
    return {item.arg: item.value for item in node.keywords if item.arg is not None}


def docstring_of(node: DocstringNode) -> str:
    """取类 / 函数的 docstring 首段（去空白）。"""
    raw = ast.get_docstring(node)
    if not raw:
        return ""
    for line in raw.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return ""


__all__ = [
    "DocstringNode",
    "call_name",
    "docstring_of",
    "keyword_map",
    "literal_str",
    "option",
    "option_str_list",
    "parse_python",
    "read_json",
    "root_path",
    "short_name",
    "source_files",
    "unparse",
]
