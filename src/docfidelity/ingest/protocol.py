"""接入器协议与「试接入」（DD-5.1）。

协议只有两个动作：

``probe``
    **不落盘、不产事实**，只回答"这份来源能不能读、大概有多少东西"。它服务于
    界面上的"试接入"：用户先看到代价，再决定要不要真接。
``load``
    产出 :class:`~docfidelity.contracts.fact.FactBundle`（事实集合 + 资产登记 + 摘要）。

与 DD-5.1 的一处**有意偏离**
----------------------------
设计文档把 ``load`` 的返回写成 ``tuple[list[DomainObject], list[Finding]]``；本实现返回
``FactBundle``。理由是契约层已经把这件事定死了——``contracts/fact.py`` 写明
「FactBundle = 单个事实源的产出」，并且写明分层动机：「接入器只负责把外部东西读成事实，
归一化与冲突裁决集中在一处」。若让每个接入器各自把裸字段拼成 ``DomainObject``，
``normalize``（FR-M1-14）就会被复制 N 份，各来源的同名字段立刻开始漂移。

契约层是唯一对外承诺，接入器协议属于实现细节，因此这里跟契约层对齐。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import ClassVar, Protocol, runtime_checkable

from docfidelity.contracts.fact import FactBundle, SourceConfig


@dataclass(frozen=True)
class ProbeResult:
    """「试接入」结果。

    ``item_count`` 允许为 ``None``——"读得通但数不清"与"读得通且知道有多少"必须
    可区分，前者不该被伪造成 ``0``。
    """

    ok: bool
    detail: str
    item_count: int | None = None
    warnings: tuple[str, ...] = field(default_factory=tuple)


@runtime_checkable
class FactSourceConnector(Protocol):
    """事实源接入器。

    ``name`` 同时是注册名与 :data:`~docfidelity.contracts.fact.SourceType` 的取值；
    它是**数据**（选择哪个接入器由配置决定），不是引擎里的分支条件。
    """

    name: ClassVar[str]

    def probe(self, cfg: SourceConfig) -> ProbeResult:
        """探测是否可接入；不得写盘、不得产生副作用。"""
        ...

    def load(self, cfg: SourceConfig) -> FactBundle:
        """读取来源并产出事实包。只读、不执行被接入的代码（FR-M1-05）。"""
        ...


__all__ = ["FactSourceConnector", "ProbeResult"]
