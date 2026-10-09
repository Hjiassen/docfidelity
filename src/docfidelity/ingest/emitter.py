"""接入器侧的产事实助手：形态 → 绑定 → 事实（DD-5.2 / FR-M1-13 / FR-M1-14）。

接入器天然认识"操作 / 表 / 列 / 图"这类**形态**，但真正的领域类型与标识键来自外部
配置。本模块把「查绑定 → 改写字段名 → 组键 → 造事实」收成一处，使每个接入器只剩
"怎么读"这一件事。

两条纪律
--------
1. **未绑定的形态必须留告警**。静默丢弃的表现是"这份来源本来就只产出了这些"：
   少掉的一整类事实在报告里不留任何痕迹，而报告全绿。
2. **键的文本化只有一处实现**——直接复用 ``model.values.scalar_text``。接入期与
   投影期对同一个值给出两种文本（``3`` 与 ``"3"``、``True`` 与 ``"true"``），
   去重与追溯就会在两边各算一套。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from docfidelity.contracts.errors import ConfigurationError, EngineErrorInfo
from docfidelity.contracts.fact import AssetRef, Fact, FactBundle, SourceConfig
from docfidelity.model import scalar_text

from .loader import build_bundle, skip_warning


@dataclass
class FactEmitter:
    """收集接入器产出，统一处理形态绑定、未绑定告警与事实包构造。"""

    cfg: SourceConfig
    _facts: list[Fact] = field(default_factory=list, init=False, repr=False)
    _assets: list[AssetRef] = field(default_factory=list, init=False, repr=False)
    _warnings: list[EngineErrorInfo] = field(default_factory=list, init=False, repr=False)
    _unbound: dict[str, int] = field(default_factory=dict, init=False, repr=False)

    def warn(self, message: str, *, locator: str | None = None) -> None:
        """记一条非致命告警（会进事实包，不阻断）。"""
        where = {"source": self.cfg.source_id}
        if locator is not None:
            where["locator"] = locator
        self._warnings.append(skip_warning(message, where=where))

    def add_asset(self, asset: AssetRef) -> None:
        """登记一个二进制资产。"""
        self._assets.append(asset)

    def emit(
        self,
        shape: str,
        payload: dict[str, Any],
        *,
        locator: str,
        asset_names: Sequence[str] = (),
    ) -> Fact | None:
        """按形态找绑定并产出一条事实；未绑定返回 ``None`` 并累计告警。"""
        binding = self.cfg.binding_for(shape)
        if binding is None:
            self._unbound[shape] = self._unbound.get(shape, 0) + 1
            return None
        fields = binding.renamed(payload)
        missing = [name for name in binding.key if name not in fields]
        if missing:
            raise ConfigurationError(
                f"形态 {shape!r} 的绑定键引用了不存在的字段：{missing}",
                where={
                    "source": self.cfg.source_id,
                    "shape": shape,
                    "key": list(binding.key),
                    "field_map": dict(binding.field_map),
                    "available": sorted(fields),
                },
                remediation=[
                    "KindBinding.key 写的是 field_map 之后的名字；确认 key 与 field_map 不互相覆盖",
                    "该形态实际能产出的字段见 available",
                ],
            )
        fact = Fact(
            source=self.cfg.source_id,
            kind=binding.kind,
            key=[scalar_text(fields.get(name)) for name in binding.key],
            payload=fields,
            locator=locator,
            priority=self.cfg.priority + binding.priority,
            asset_names=list(asset_names),
        )
        self._facts.append(fact)
        return fact

    @property
    def facts(self) -> list[Fact]:
        return list(self._facts)

    def bundle(self, source_id: str | None = None) -> FactBundle:
        """产出事实包。未绑定形态在此汇总成告警——每种形态只报一条，带条数。"""
        warnings: list[EngineErrorInfo] = list(self._warnings)
        for shape in sorted(self._unbound):
            count = self._unbound[shape]
            warnings.append(
                skip_warning(
                    f"未绑定的形态已跳过：{shape}（{count} 条）",
                    where={"source": self.cfg.source_id, "shape": shape, "count": count},
                )
            )
        return build_bundle(
            source_id=source_id or self.cfg.source_id,
            source_type=self.cfg.source_type,
            facts=self._facts,
            assets=self._assets,
            warnings=warnings,
        )


def text_of(payload: dict[str, Any], name: str) -> str:
    """按点分路径取值的文本形式（键的文本化与投影期同源）。"""
    current: Any = payload
    for part in name.split("."):
        if not isinstance(current, dict):
            return ""
        current = current.get(part)
    return scalar_text(current)


def sorted_items(mapping: Iterable[tuple[str, Any]]) -> list[tuple[str, Any]]:
    """按键排序的条目——遍历顺序必须由内容决定（NFR-02）。"""
    return sorted(mapping, key=lambda item: item[0])


__all__ = ["FactEmitter", "sorted_items", "text_of"]
