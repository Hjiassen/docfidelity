"""事实源用例（DD-5.1 / DD-13.2 的 ``sources/*``）——``server`` 与 ``ingest`` 的接缝。

与另外两条接缝的关系
--------------------
``decisions``（消费模型协议）、``templates``（认识 ``registry``）、本模块（认识
``ingest``）三者形状相同：都是"服务端要用一步内核能力，但不能 import 它的实现"。
本模块多担了一件事——**配置期**的判定。档案与模板都是"先有资产再登记"，而事实源是
"先写一行配置再接入"；那一行配置本身对不对（接入器认不认识、路径在不在工作区里）
必须在写进库之前就能回答，否则界面只能等到"发起构建"才发现来源是坏的。

两个"不"
--------
* **不决定路径**：工作区根由调用方给（``workspace_root``）。本模块不知道工作区在哪，
  也不知道有几个——无项目上下文的知识一旦进来，"这个来源属于哪个项目"就会在这里
  被猜一次。
* **不落盘**：探针与条目浏览都不写任何东西（``probe`` 契约本身就要求"不得写盘、
  不得产生副作用"）。要留痕的是调用方（记 ``last_probed_at`` 与 ``item_count``）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from docfidelity.contracts.errors import EngineError, EngineErrorInfo
from docfidelity.contracts.fact import AssetRef, Fact, SourceConfig
from docfidelity.ingest import (
    ConnectorRegistry,
    SourceGuard,
    default_registry,
    probe_connector,
    run_connector,
)
from docfidelity.platform.fs import safe_join

__all__ = [
    "DEFAULT_ITEM_LIMIT",
    "SourceItems",
    "SourceProbe",
    "check_config",
    "connector_names",
    "load_items",
    "probe_source",
]

#: 条目浏览器的默认上限。**有上限**而不是"全给"：一次接入可能产出上万条事实，
#: 而界面一屏只能看几十条——全量传输会让"打开来源详情"变成一个几秒的白屏。
#: 截断了要如实说（:attr:`SourceItems.truncated`），不能悄悄少给。
DEFAULT_ITEM_LIMIT: Final = 200


@dataclass(frozen=True)
class SourceProbe:
    """「试接入」的结果（DD-13.2 的 ``POST …/sources/{sid}/probe``）。

    ``ok=False`` 也**是**一次正常回答：探针服务于界面上的一个按钮，失败要显示成
    "这条来源现在连不通 + 为什么"，而不是一个 500。
    """

    ok: bool
    detail: str
    item_count: int | None = None
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class SourceItems:
    """「条目浏览器」的结果（DD-13.2 的 ``GET …/sources/{sid}/items``）。"""

    source_id: str
    source_type: str
    facts: list[Fact] = field(default_factory=list)
    assets: list[AssetRef] = field(default_factory=list)
    digest: str = ""
    #: 该来源这次一共产出多少条事实（未截断时等于 ``len(facts)``）。
    total: int = 0
    truncated: bool = False
    warnings: list[EngineErrorInfo] = field(default_factory=list)

    def kinds(self) -> dict[str, int]:
        """按领域类型计数，供界面显示"这份来源供给哪几类对象"。"""
        counts: dict[str, int] = {}
        for fact in self.facts:
            counts[fact.kind] = counts.get(fact.kind, 0) + 1
        return counts


def connector_names(registry: ConnectorRegistry | None = None) -> list[str]:
    """已登记接入器的注册名（升序），供界面下拉。"""
    return (registry or default_registry()).names()


def check_config(
    config: SourceConfig,
    *,
    registry: ConnectorRegistry | None = None,
    workspace_root: str | Path | None = None,
) -> list[str]:
    """**配置期**校验：这条来源声明能不能用。返回问题清单（空 = 没问题，**不抛**）。

    与 :func:`probe_source` 的分工是"配置期 vs 接入期"：本函数不读文件内容，
    只回答"接入器认不认识、路径解不解得开"。于是它可以在写库之前跑，
    且不会因为"今天那个文件被移走了"而失败。

    只做两项，因为只有这两项是**声明本身**的问题：

    1. ``source_type`` 有没有对应的接入器。注意它与 ``SourceType`` 那个 Literal
       并不等价——注册表可由 entry point 扩充（``discover_entry_points``），
       而 Literal 是闭集，两侧长期必然出现"注册表里有、Literal 里没有"。
    2. ``root`` 能不能在工作区内解开（越界即问题）。空 ``root`` 合法：
       手工输入型来源本来就没有文件。

    ``workspace_root`` 为 ``None`` 时跳过第 2 项——**跳过不等于通过**，
    调用方要自己决定怎么记（``server`` 侧一律给根，故这条只服务测试与离线调用）。
    """
    problems: list[str] = []
    names = connector_names(registry)
    if config.source_type not in names:
        problems.append(
            f"没有名为 {config.source_type!r} 的接入器；已登记：{'、'.join(names) or '（无）'}"
        )
    if config.root is not None and workspace_root is not None:
        try:
            safe_join(Path(workspace_root), config.root)
        except EngineError as exc:  # 越界即配置问题，不必区分具体异常类型
            problems.append(f"root 不在工作区内：{exc}")
    return problems


def probe_source(
    config: SourceConfig,
    *,
    workspace_root: str | Path,
    registry: ConnectorRegistry | None = None,
) -> SourceProbe:
    """试接入：**永不抛**（见 :func:`docfidelity.ingest.probe_connector`）。

    接入器取不到（``source_type`` 不认识）时也返回 ``ok=False`` 而不是抛：
    探针的调用场景是"人在界面上点了一下按钮"，而这个按钮的输入可能正是不认识的名字。

    与 :func:`load_items` 共用同一个 ``SourceGuard``，故两条通路对 ``root`` 的解释
    必然一致——**这正是它俩最容易被做成分叉的地方**（见 ``ingest.loader`` 的说明）。
    """
    table = registry or default_registry()
    if not table.has(config.source_type):
        return SourceProbe(
            ok=False,
            detail=f"没有名为 {config.source_type!r} 的接入器；已登记：{'、'.join(table.names())}",
        )
    guard = SourceGuard(workspace_root=Path(workspace_root))
    result = probe_connector(table.get(config.source_type), config, guard=guard)
    return SourceProbe(
        ok=result.ok,
        detail=result.detail,
        item_count=result.item_count,
        warnings=list(result.warnings),
    )


def load_items(
    config: SourceConfig,
    *,
    workspace_root: str | Path,
    registry: ConnectorRegistry | None = None,
    limit: int = DEFAULT_ITEM_LIMIT,
) -> SourceItems:
    """真接入一次，把条目取回来给界面看（**不落盘**）。

    这里与 :func:`probe_source` 的关键差别是**它会抛**：探针是"能不能通"，
    条目浏览器是"通的话里面是什么"，读不出来就是一次失败请求（``E_SOURCE_LOAD_FAILED``
    / ``E_PATH_ESCAPE``），要被界面显示成错误而不是变成一份空清单——空清单会被
    读成"这份来源本来就没有对象"。

    Raises
    ------
    EngineError
        ``E_SOURCE_LOAD_FAILED``：接入器执行失败（带文件行定位）。
        ``E_PATH_ESCAPE``：``root`` 越出工作区。
    """
    table = registry or default_registry()
    guard = SourceGuard(workspace_root=Path(workspace_root))
    outcome = run_connector(table.get(config.source_type), config, guard=guard)
    facts = list(outcome.facts)
    truncated = len(facts) > limit
    shown = facts[:limit] if truncated else facts
    return SourceItems(
        source_id=outcome.source_id,
        source_type=outcome.source_type,
        facts=shown,
        assets=list(outcome.assets),
        digest=outcome.digest,
        total=len(facts),
        truncated=truncated,
        warnings=list(outcome.warnings),
    )
