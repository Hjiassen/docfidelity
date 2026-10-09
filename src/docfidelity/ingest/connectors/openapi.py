"""OpenAPI 接入（FR-M1-04 / DD-5.2）。

输入：``openapi.json``（含 FastAPI ``app.openapi()`` 的导出）。
抽取：操作清单、参数、请求体的出入参形状、响应码、鉴权方案、作用域分类。

两条纪律
--------
1. **``$ref`` 必须限深且防环**（DD-5.2 点名的陷阱）。递归展开一个自引用的 schema
   会直接把进程打死，而"展开失败"与"这个 schema 是空的"在产物上长得一样——
   因此超限时保留 ``{"$ref": ...}`` 原样可见，不抹成空。
2. **只发中性字段名**。本接入器产出的 ``scope`` / ``security`` / ``parameters`` 是
   **形态字段**，不是领域字段；把它们变成领域模型里的 ``module`` / ``auth`` 由
   ``KindBinding.field_map`` 完成（FR-M1-13 / FR-M1-14）。

作用域分类（DD-5.2 的"按 ``tags`` 归 ``scope``"）取操作的**首个 tag**；无 tag 时用
``options.scope_default``。缺省值放在配置里而不是写死——它是数据，不是引擎的判断。
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any, ClassVar

from docfidelity.contracts.errors import ConfigurationError
from docfidelity.contracts.fact import FactBundle, SourceConfig

from ..emitter import FactEmitter
from ..protocol import ProbeResult
from ._support import option, read_json, source_files

#: 会被当作操作处理的键；``parameters`` 等路径级字段与 ``$ref`` 不算操作。
_NON_OPERATION_KEYS = frozenset({"parameters", "summary", "description", "servers", "$ref"})

_SAFE_KEY = re.compile(r"[^A-Za-z0-9]+")


def _safe_identifier(method: str, path: str) -> str:
    """由方法 + 路径派生稳定的操作标识。

    派生结果可能被绑定成标识键，因此必须**不含 ``/`` 与 ``{}``**——键会被拼进
    ``"<kind>:<key 各段以 / 连接>"``，路径里的斜杠会让键的段数看起来忽多忽少。
    """
    return _SAFE_KEY.sub("_", f"{method}_{path}").strip("_").lower()


class _Resolver:
    """本地 ``$ref`` 展开器：限深 + 防环，超出即原样保留引用。"""

    def __init__(self, document: Mapping[str, Any], max_depth: int) -> None:
        self._document = document
        self._max_depth = max_depth

    def _lookup(self, ref: str) -> Any:
        if not ref.startswith("#/"):
            return None  # 外部引用不解析：网络访问不在接入层的职责内
        node: Any = self._document
        for part in ref[2:].split("/"):
            if not isinstance(node, Mapping) or part not in node:
                return None
            node = node[part]
        return node

    def expand(self, node: Any, *, chain: tuple[str, ...] = ()) -> Any:
        if isinstance(node, Mapping):
            ref = node.get("$ref")
            if isinstance(ref, str):
                if ref in chain or len(chain) >= self._max_depth:
                    return {"$ref": ref}
                target = self._lookup(ref)
                if target is None:
                    return {"$ref": ref}
                merged = self.expand(target, chain=(*chain, ref))
                extra = {key: value for key, value in node.items() if key != "$ref"}
                if extra and isinstance(merged, Mapping):
                    combined = dict(merged)
                    combined.update(self.expand(extra, chain=chain))
                    return combined
                return merged
            return {key: self.expand(value, chain=chain) for key, value in node.items()}
        if isinstance(node, list):
            return [self.expand(item, chain=chain) for item in node]
        return node


def _text_of(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _parameter_entry(raw: Any, source: str, resolver: _Resolver) -> dict[str, Any]:
    resolved = resolver.expand(raw)
    if not isinstance(resolved, Mapping):
        return {"name": "", "type": "", "required": False, "desc": "", "source": source}
    schema = resolved.get("schema")
    type_text = ""
    if isinstance(schema, Mapping):
        type_text = _text_of(schema.get("type")) or _text_of(schema.get("format"))
    return {
        "name": _text_of(resolved.get("name")),
        "type": type_text,
        "required": bool(resolved.get("required")),
        "desc": _text_of(resolved.get("description")),
        "source": source,
    }


def _body_entries(
    path_item: Mapping[str, Any], operation: Mapping[str, Any], resolver: _Resolver
) -> list[dict[str, Any]]:
    raw = operation.get("requestBody", path_item.get("requestBody"))
    if raw is None:
        return []
    resolved = resolver.expand(raw)
    if not isinstance(resolved, Mapping):
        return []
    return [
        {
            "name": "body",
            "type": "object",
            "required": bool(resolved.get("required")),
            "desc": _text_of(resolved.get("description")),
            "source": "body",
        }
    ]


def _response_entries(operation: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw = operation.get("responses")
    if not isinstance(raw, Mapping):
        return []
    entries: list[dict[str, Any]] = []
    for code in sorted(raw, key=str):
        item = raw[code]
        desc = _text_of(item.get("description")) if isinstance(item, Mapping) else ""
        entries.append({"code": str(code), "desc": desc})
    return entries


def _security_names(document: Mapping[str, Any], operation: Mapping[str, Any]) -> list[str]:
    declared = operation.get("security", document.get("security"))
    if not isinstance(declared, list):
        return []
    names: set[str] = set()
    for requirement in declared:
        if isinstance(requirement, Mapping):
            names.update(str(name) for name in requirement)
    return sorted(names)


def _operations(
    document: Mapping[str, Any],
) -> list[tuple[str, str, Mapping[str, Any], Mapping[str, Any]]]:
    """按 (路径, 方法) 升序列出全部操作。顺序由内容决定（NFR-02）。"""
    paths = document.get("paths")
    if not isinstance(paths, Mapping):
        return []
    found: list[tuple[str, str, Mapping[str, Any], Mapping[str, Any]]] = []
    for path in sorted(paths, key=str):
        item = paths[path]
        if not isinstance(item, Mapping):
            continue
        for method in sorted(item, key=str):
            if method in _NON_OPERATION_KEYS or method.startswith("x-"):
                continue
            operation = item[method]
            if isinstance(operation, Mapping):
                found.append((str(path), method, item, operation))
    return found


def _schemas(document: Mapping[str, Any]) -> list[tuple[str, Mapping[str, Any]]]:
    components = document.get("components")
    if not isinstance(components, Mapping):
        return []
    schemas = components.get("schemas")
    if not isinstance(schemas, Mapping):
        return []
    return [
        (str(name), schemas[name])
        for name in sorted(schemas, key=str)
        if isinstance(schemas[name], Mapping)
    ]


class OpenApiConnector:
    """OpenAPI 文档接入器。"""

    name: ClassVar[str] = "openapi"

    def _first_document(self, cfg: SourceConfig) -> tuple[str, Mapping[str, Any]]:
        files = source_files(cfg, (".json",))
        if not files:
            raise ConfigurationError(
                f"事实源 {cfg.source_id} 下没有 .json 文件",
                where={"source": cfg.source_id, "root": cfg.root},
                remediation=["指向 OpenAPI 导出的 JSON 文件或其所在目录"],
            )
        for path in files:
            document = read_json(path)
            if isinstance(document, Mapping) and "paths" in document:
                return path.name, document
        raise ConfigurationError(
            f"事实源 {cfg.source_id} 下没有可识别的 OpenAPI 文档（缺 paths）",
            where={"source": cfg.source_id, "files": [path.name for path in files]},
            remediation=["确认导出的是 OpenAPI 文档本体，而不是包装层"],
        )

    def probe(self, cfg: SourceConfig) -> ProbeResult:
        try:
            origin, document = self._first_document(cfg)
        except ConfigurationError as exc:
            return ProbeResult(ok=False, detail=exc.message)
        count = len(_operations(document))
        schemas = len(_schemas(document))
        return ProbeResult(
            ok=True,
            detail=f"{origin}：{count} 个操作、{schemas} 个 schema 定义",
            item_count=count,
        )

    def load(self, cfg: SourceConfig) -> FactBundle:
        origin, document = self._first_document(cfg)
        resolver = _Resolver(document, option(cfg, "max_ref_depth", 8))
        scope_default = option(cfg, "scope_default", "default")
        emitter = FactEmitter(cfg)
        self._emit_operations(emitter, document, resolver, scope_default, origin)
        self._emit_schemas(emitter, document, origin)
        return emitter.bundle()

    def _emit_operations(
        self,
        emitter: FactEmitter,
        document: Mapping[str, Any],
        resolver: _Resolver,
        scope_default: str,
        origin: str,
    ) -> None:
        for path, method, item, operation in _operations(document):
            tags = operation.get("tags")
            scope = (
                str(tags[0])
                if isinstance(tags, Sequence) and tags and isinstance(tags[0], str)
                else scope_default
            )
            parameters = [
                _parameter_entry(raw, "path", resolver) for raw in _as_list(item.get("parameters"))
            ] + [
                _parameter_entry(raw, "operation", resolver)
                for raw in _as_list(operation.get("parameters"))
            ]
            parameters.extend(_body_entries(item, operation, resolver))
            security = _security_names(document, operation)
            emitter.emit(
                "operation",
                {
                    "method": method.upper(),
                    "path": path,
                    "scope": scope,
                    "operation_ref": _safe_identifier(method, path),
                    "operation_id": _text_of(operation.get("operationId"))
                    or _safe_identifier(method, path),
                    "summary": _text_of(operation.get("summary")),
                    "description": _text_of(operation.get("description")),
                    "deprecated": bool(operation.get("deprecated")),
                    "security": security,
                    "auth_required": bool(security),
                    "parameter_count": len(parameters),
                    "parameters": parameters,
                    "responses": _response_entries(operation),
                },
                locator=f"{origin}#/paths/{path}/{method}",
            )

    def _emit_schemas(self, emitter: FactEmitter, document: Mapping[str, Any], origin: str) -> None:
        for name, schema in _schemas(document):
            properties = schema.get("properties")
            emitter.emit(
                "schema",
                {
                    "name": name,
                    "type": _text_of(schema.get("type")) or "object",
                    "property_count": len(properties) if isinstance(properties, Mapping) else 0,
                    "description": _text_of(schema.get("description")),
                },
                locator=f"{origin}#/components/schemas/{name}",
            )


def _as_list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list) else []


__all__ = ["OpenApiConnector"]
