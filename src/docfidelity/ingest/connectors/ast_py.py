"""源码 AST 接入（FR-M1-05 / DD-5.2）。

输入：源码目录。
抽取：装饰器声明的操作（路由）、函数 docstring、Pydantic 风格模型的字段清单。

**绝不 import 被接入的模块**（FR-M1-05）。导入会连库、执行模块级代码、拉起后台线程
——"接入一个源码目录"不该产生任何副作用。本模块只用 ``ast.parse``，全程只读文本。

只处理**模块顶层**的类与函数：嵌套定义通常不是声明，误收会把内部实现细节变成事实，
而它们在报告里看起来与真声明一模一样。

哪些装饰器算"操作"是**配置**（``options.http_methods`` / ``options.router_names``），
哪些基类算"模型"同样是配置（``options.model_bases``）——写死等于把某一套框架的约定
焊进引擎。

``probe`` 走同一条遍历但**只计数不产事实**：试接入的目的是告诉用户代价，
而不是先付一遍代价。
"""

from __future__ import annotations

import ast
from pathlib import Path
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

#: 默认认作操作的装饰器属性名。
DEFAULT_HTTP_METHODS: tuple[str, ...] = ("get", "post", "put", "patch", "delete")

#: 默认认作模型的基类名。
DEFAULT_MODEL_BASES: tuple[str, ...] = ("BaseModel",)

#: 具名参数构造器：出现它们时"有没有 default"才决定必填性。
_FIELD_CALLS = frozenset({"Field", "Query", "Body", "Path", "Header", "Cookie", "Form"})


def _relative(path: Path, root: Path) -> str:
    return relative_posix(path, root) if root.is_dir() else path.name


def _module_name(relative: str) -> str:
    """相对路径 → 点分模块名（``pkg/api.py`` → ``pkg.api``）。"""
    without_suffix = relative[:-3] if relative.endswith(".py") else relative
    parts = [part for part in without_suffix.replace("\\", "/").split("/") if part]
    if parts and parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _route_decorators(
    node: ast.FunctionDef | ast.AsyncFunctionDef, methods: set[str], routers: set[str]
) -> list[tuple[str, str]]:
    """从装饰器里取出 (方法, 路由) 对。"""
    found: list[tuple[str, str]] = []
    for decorator in node.decorator_list:
        if not isinstance(decorator, ast.Call):
            continue
        func = decorator.func
        if not isinstance(func, ast.Attribute) or func.attr not in methods:
            continue
        if isinstance(func.value, ast.Call):  # 形如 router().get(...)，静态上判不出宿主
            continue
        if routers and call_name(func.value) not in routers:
            continue
        route = literal_str(decorator.args[0]) if decorator.args else None
        found.append((func.attr, route or ""))
    return found


def _field_entries(class_node: ast.ClassDef) -> list[dict[str, Any]]:
    """从类体里抽字段声明。只认带类型标注的赋值语句。"""
    entries: list[dict[str, Any]] = []
    for statement in class_node.body:
        if not isinstance(statement, ast.AnnAssign) or not isinstance(statement.target, ast.Name):
            continue
        value = statement.value
        required = value is None
        desc = ""
        if value is not None and not isinstance(value, ast.Call):
            required = False  # 直接赋字面量默认值 → 非必填
        elif isinstance(value, ast.Call):
            if short_name(call_name(value.func)) in _FIELD_CALLS:
                kwargs = keyword_map(value)
                required = "default" not in kwargs and "default_factory" not in kwargs
                desc = literal_str(kwargs.get("description")) or ""
            else:
                required = False  # 其它调用（工厂函数等）在静态上算作有默认值
        entries.append(
            {
                "name": statement.target.id,
                "type": unparse(statement.annotation),
                "required": required,
                "desc": desc,
                "line": statement.lineno,
            }
        )
    return entries


def _is_model(node: ast.ClassDef, model_bases: set[str]) -> bool:
    return any(short_name(call_name(base)) in model_bases for base in node.bases)


class AstConnector:
    """源码目录接入器。"""

    name: ClassVar[str] = "ast"

    def _settings(self, cfg: SourceConfig) -> tuple[set[str], set[str], set[str]]:
        methods = {
            item.lower() for item in option_str_list(cfg, "http_methods", DEFAULT_HTTP_METHODS)
        }
        routers = set(option_str_list(cfg, "router_names", ()))
        model_bases = set(option_str_list(cfg, "model_bases", DEFAULT_MODEL_BASES))
        return methods, routers, model_bases

    def _emit_class(
        self, emitter: FactEmitter, node: ast.ClassDef, module: str, relative: str
    ) -> None:
        fields = _field_entries(node)
        emitter.emit(
            "model",
            {
                "model": node.name,
                "module": module,
                "source_file": relative,
                "line": node.lineno,
                "summary": docstring_of(node),
                "field_count": len(fields),
                "fields": fields,
            },
            locator=f"{relative}:{node.lineno}",
        )

    def _emit_function(
        self,
        emitter: FactEmitter,
        node: ast.FunctionDef | ast.AsyncFunctionDef,
        module: str,
        relative: str,
        methods: set[str],
        routers: set[str],
    ) -> None:
        for method, route in _route_decorators(node, methods, routers):
            arguments = [arg.arg for arg in node.args.args]
            emitter.emit(
                "route",
                {
                    "method": method.upper(),
                    "route": route,
                    "handler": node.name,
                    "module": module,
                    "source_file": relative,
                    "line": node.lineno,
                    "summary": docstring_of(node),
                    "param_count": len(arguments),
                    "params": arguments,
                },
                locator=f"{relative}:{node.lineno}",
            )

    def _declarations(
        self, cfg: SourceConfig
    ) -> tuple[
        list[tuple[ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef, str, str]], list[str]
    ]:
        """产出 (顶层节点, 模块名, 相对路径) 序列，以及解析失败的告警。"""
        methods, routers, model_bases = self._settings(cfg)
        root = root_path(cfg)
        items: list[tuple[ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef, str, str]] = []
        warnings: list[str] = []
        for path in source_files(cfg, (".py",)):
            relative = _relative(path, root)
            tree = parse_python(path)
            if tree is None:
                warnings.append(f"源码解析失败，已跳过：{relative}")
                continue
            module = _module_name(relative)
            for statement in tree.body:
                if isinstance(statement, ast.ClassDef):
                    if _is_model(statement, model_bases):
                        items.append((statement, module, relative))
                elif isinstance(statement, ast.FunctionDef | ast.AsyncFunctionDef) and (
                    _route_decorators(statement, methods, routers)
                ):
                    items.append((statement, module, relative))
        return items, warnings

    def probe(self, cfg: SourceConfig) -> ProbeResult:
        items, warnings = self._declarations(cfg)
        return ProbeResult(
            ok=True,
            detail=f"识别出 {len(items)} 项声明",
            item_count=len(items),
            warnings=tuple(warnings),
        )

    def load(self, cfg: SourceConfig) -> FactBundle:
        methods, routers, _ = self._settings(cfg)
        emitter = FactEmitter(cfg)
        items, warnings = self._declarations(cfg)
        for message in warnings:
            emitter.warn(message)
        for node, module, relative in items:
            if isinstance(node, ast.ClassDef):
                self._emit_class(emitter, node, module, relative)
            else:
                self._emit_function(emitter, node, module, relative, methods, routers)
        if not items and not warnings:
            emitter.warn(
                "未识别到任何声明：检查 options.http_methods / model_bases 是否与项目约定一致"
            )
        return emitter.bundle()


__all__ = ["DEFAULT_HTTP_METHODS", "DEFAULT_MODEL_BASES", "AstConnector"]
