"""接入器注册表（DD-5.1；发现机制不侵入核心，4.5 硬原则 2）。

两个来源，两种纪律
------------------
**内建接入器**走显式闭集表（:data:`_BUILTIN`）："新增一类来源" = 加一行登记，
而不是往引擎里加一个 ``if name == "..."``。表里存的是「模块:属性」字符串，
因此**导入本模块不会导入任何接入器**——否则光是导入内核就会把 xlsx / ast 的解析器
全拉起来。
**第三方接入器**走 ``pyproject.toml`` 的 ``docfidelity.connectors`` entry point，
由 :func:`discover_entry_points` 显式读取。它不在导入期执行：导入内核不该等于
"发现并加载第三方代码"。

与 DD-5.1 的一处**有意偏离**：设计文档把内建接入器也写成 entry point。本实现让内建
走闭集表，理由是内建接入器必须**在未安装（源码树直接跑）时也可用**，而 entry point
只在装好之后才存在；同时避免同一份映射在代码与 ``pyproject.toml`` 里各存一份。
第三方扩展仍完全走 entry point。
"""

from __future__ import annotations

import importlib
from collections.abc import Callable, Iterable
from importlib.metadata import entry_points
from typing import Final, cast

from docfidelity.contracts.errors import ConfigurationError, EngineError, ErrorCode

from .protocol import FactSourceConnector

#: 接入器工厂（内建表与 entry point 都指向"调用即得接入器"的东西）。
Factory = Callable[[], FactSourceConnector]

#: 第三方接入器的 entry point 分组名。
ENTRY_POINT_GROUP: Final = "docfidelity.connectors"

#: 内建接入器：注册名 -> 「模块:属性」。注册名与 ``SourceType`` 取值一致。
_BUILTIN: Final[dict[str, str]] = {
    "openapi": "docfidelity.ingest.connectors.openapi:OpenApiConnector",
    "ast": "docfidelity.ingest.connectors.ast_py:AstConnector",
    "orm": "docfidelity.ingest.connectors.orm:OrmConnector",
    "xlsx": "docfidelity.ingest.connectors.xlsx:XlsxConnector",
    "assets": "docfidelity.ingest.connectors.assets:AssetsConnector",
}


def _load_target(spec: str) -> Factory:
    module_name, _, attribute = spec.partition(":")
    if not module_name or not attribute:
        raise ConfigurationError(
            f"接入器登记格式不合法：{spec!r}，应形如 '模块:属性'",
            where={"spec": spec},
        )
    module = importlib.import_module(module_name)
    try:
        target = getattr(module, attribute)
    except AttributeError as exc:
        raise ConfigurationError(
            f"接入器登记指向的属性不存在：{spec}",
            where={"spec": spec},
        ) from exc
    return cast(Factory, target)


class ConnectorRegistry:
    """注册名 → 接入器实例。注册名即 :data:`SourceType` 取值。"""

    def __init__(self, connectors: Iterable[FactSourceConnector] = ()) -> None:
        self._connectors: dict[str, FactSourceConnector] = {}
        for connector in connectors:
            self.register(connector)

    def register(self, connector: FactSourceConnector) -> None:
        """登记接入器。同名同类型重复登记是幂等的；同名不同类型直接失败。

        静默覆盖的后果是"装了第三方插件之后内建接入器换了个实现"，而配置里看不出来。
        """
        name = connector.name
        existing = self._connectors.get(name)
        if existing is not None and type(existing) is not type(connector):
            raise ConfigurationError(
                f"接入器注册名冲突：{name!r} 已有 {type(existing).__name__}，"
                f"又来了 {type(connector).__name__}",
                where={"name": name},
                remediation=["重命名其中之一，或卸载冲突的插件包"],
            )
        self._connectors[name] = connector

    def get(self, name: str) -> FactSourceConnector:
        """取接入器；未登记即失败（**不返回 ``None``**）。"""
        connector = self._connectors.get(name)
        if connector is None:
            raise EngineError(
                ErrorCode.SOURCE_LOAD_FAILED,
                f"未登记的接入器：{name!r}",
                where={"name": name, "known": self.names()},
                remediation=[f"把来源类型改为已登记之一：{'、'.join(self.names())}"],
            )
        return connector

    def has(self, name: str) -> bool:
        return name in self._connectors

    def names(self) -> list[str]:
        """已登记注册名，升序。返回新列表，调用方改动不影响注册表。"""
        return sorted(self._connectors)

    def __len__(self) -> int:
        return len(self._connectors)


def builtin_connectors() -> list[FactSourceConnector]:
    """实例化全部内建接入器（按注册名升序）。"""
    connectors: list[FactSourceConnector] = []
    for name in sorted(_BUILTIN):
        factory = _load_target(_BUILTIN[name])
        connectors.append(factory())
    return connectors


def default_registry() -> ConnectorRegistry:
    """内建注册表：**不**读 entry point，保证源码树直接可用且无副作用。"""
    return ConnectorRegistry(builtin_connectors())


def discover_entry_points(registry: ConnectorRegistry) -> list[str]:
    """把 entry point 提供的接入器并入注册表，返回新登记的注册名（升序）。

    内建接入器若同时在 entry point 里出现也无妨——同名同类登记是幂等的。
    """
    added: list[str] = []
    for entry in entry_points(group=ENTRY_POINT_GROUP):
        try:
            factory = entry.load()
        except Exception as exc:  # 单个插件坏掉不该拦住其余插件
            raise ConfigurationError(
                f"接入器插件加载失败：{entry.name}",
                where={"entry_point": entry.name, "value": entry.value},
            ) from exc
        connector = factory()
        if not registry.has(connector.name):
            added.append(connector.name)
        registry.register(connector)
    return sorted(added)


__all__ = [
    "ENTRY_POINT_GROUP",
    "ConnectorRegistry",
    "builtin_connectors",
    "default_registry",
    "discover_entry_points",
]
