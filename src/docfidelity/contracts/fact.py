"""事实包与事实快照（DD-5）。

一条事实 = 一个领域对象的**原始形态**（尚未归一化）。事实包是单个事实源的产出；
事实快照是多源合并、去重、冲突裁决之后的稳定输入。

分层动机：接入器只负责"把外部东西读成事实"，归一化与冲突裁决集中在
``ingest.normalize`` / ``ingest.conflict``，避免每个接入器各写一套合并逻辑。
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any, ClassVar, Literal

from pydantic import Field

from .base import FormatModel
from .errors import EngineErrorInfo

SourceType = Literal["openapi", "ast", "orm", "proto", "xlsx", "markdown", "assets", "manual"]
"""内置接入器类型。新增类型只需实现 Connector 协议并登记，不改引擎分支。"""


class AssetRef(FormatModel):
    """工作区内一个二进制资产的登记条目。"""

    logical_name: str = Field(description="逻辑名，IR 与追溯边引用此名而非路径")
    relative_path: str = Field(description="相对工作区根的路径")
    media_type: str = Field(description="MIME 类型")
    sha256: str
    size_bytes: int = Field(ge=0)
    width_px: int | None = None
    height_px: int | None = None
    dpi: int | None = Field(default=None, gt=0)


class KindBinding(FormatModel):
    """把接入器产出的**形态**映射到外部声明的领域类型（FR-M1-13 / FR-M1-14）。

    为什么要这一层：接入器天然认识"操作 / 表 / 列 / 图"这类**形态**，而领域类型
    （``kind``）与标识键是**外部数据**。二者之间的映射一旦写进引擎代码，"引擎与文档
    类型无关"就破了。因此映射以配置形式给出，引擎只查表、不作判断。

    ``shape`` 是接入器内部形态标识；``kind`` / ``key`` 必须与领域模型声明一致
    （``key`` 是**改名后**的字段名，顺序即键序）。``field_map`` 把来源侧裸字段名
    改写成领域声明的字段名——这是"不同来源的同名对象可合并、可去重"的前提。

    注意 ``key`` 与 ``field_map`` 的先后：``field_map`` 先执行，``key`` 里写的必须是
    **改名之后**的名字。写错了不会报错，只会得到空键——两条不同的记录会被折叠成一条。
    接入器对此有显式校验（``ingest.emitter``），键名取不到时当场失败。
    """

    shape: str = Field(description="接入器内部形态标识")
    kind: str = Field(description="映射到的领域类型，取值由外部档案声明")
    key: list[str] = Field(
        min_length=1, description="构成标识键的字段名（field_map 之后的名字），顺序即键序"
    )
    field_map: dict[str, str] = Field(
        default_factory=dict, description="来源字段名 -> 领域字段名；未列出者原样保留"
    )
    priority: int = Field(default=0, description="该形态产出的冲突裁决优先级")

    def renamed(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """按 ``field_map`` 改写字段名；未列出的原样保留。"""
        return {self.field_map.get(name, name): value for name, value in payload.items()}


class SourceConfig(FormatModel):
    """一个事实源实例的配置（``fact-sources.yaml`` 的一行，DD-5.1 / DD-16）。

    ``source_type`` 同时充当接入器注册名——选择哪个接入器是**数据**，不是分支。
    每个形态通过 :class:`KindBinding` 声明目标领域类型；未绑定的形态**不得静默丢弃**，
    接入器必须给出 Finding（否则"少了一整章"会伪装成"这份来源本来就没有"）。
    """

    format_family: ClassVar[str] = "fact"

    source_id: str = Field(description="事实源标识，事实与冲突记录都引用它")
    source_type: SourceType = Field(description="接入器注册名")
    title: str | None = Field(default=None, description="中文显示名，仅用于界面与报告")
    root: str | None = Field(default=None, description="文件或目录，相对工作区根")
    bindings: list[KindBinding] = Field(default_factory=list, description="形态 -> 领域类型映射")
    options: dict[str, Any] = Field(default_factory=dict, description="接入器私有参数")
    priority: int = Field(default=0, description="本源默认优先级，越大越优先")
    enabled: bool = True

    def binding_for(self, shape: str) -> KindBinding | None:
        """取某形态的绑定；未绑定返回 ``None``（调用方必须记 Finding）。"""
        for binding in self.bindings:
            if binding.shape == shape:
                return binding
        return None

    def bound_shapes(self) -> list[str]:
        """全部已绑定形态，按声明顺序去重。"""
        seen: dict[str, None] = {}
        for binding in self.bindings:
            seen.setdefault(binding.shape, None)
        return list(seen)


class Fact(FormatModel):
    """一条原始事实。

    ``key`` 的顺序必须与对应 :class:`~docfidelity.contracts.domain.DomainModel` 的
    ``key`` 一致——归一化与去重都依赖这一点。
    """

    format_family: ClassVar[str] = "fact"

    source: str = Field(description="事实源标识")
    kind: str = Field(description="领域类型")
    key: list[str] = Field(min_length=1, description="标识键取值，顺序与领域模型的 key 一致")
    payload: dict[str, Any] = Field(default_factory=dict, description="原始字段值")
    locator: str = Field(description="源内定位，用于报错定位与追溯")
    verified: bool = Field(default=True, description="是否通过该源的接入校验")
    priority: int = Field(default=0, description="冲突裁决优先级，越大越优先")
    asset_names: list[str] = Field(default_factory=list, description="本事实关联的资产逻辑名")

    @property
    def ref(self) -> str:
        return f"{self.kind}:{'/'.join(self.key)}"


class FactBundle(FormatModel):
    """单个事实源的产出。"""

    format_family: ClassVar[str] = "fact"

    source_id: str
    source_type: SourceType
    facts: list[Fact] = Field(default_factory=list)
    assets: list[AssetRef] = Field(default_factory=list)
    digest: str = Field(description="内容摘要，用于判定输入是否变化")
    loaded_at: datetime | None = Field(default=None, description="元数据，不参与逻辑判定")
    warnings: list[EngineErrorInfo] = Field(
        default_factory=list, description="非致命问题：记下来但不阻断（fatal=False）"
    )

    def by_kind(self, kind: str) -> list[Fact]:
        return [fact for fact in self.facts if fact.kind == kind]


class ConflictCandidate(FormatModel):
    """冲突的一个候选值。"""

    value: Any
    source: str
    locator: str
    priority: int = 0
    verified: bool = True


class ConflictRecord(FormatModel):
    """同一对象同一字段出现多个不同取值时的记录。

    ``resolution`` 为 ``unresolved`` 的冲突必须进入裁决队列，
    **不得静默采用**（验收 39）。
    """

    kind: str
    key: list[str]
    field: str
    candidates: list[ConflictCandidate] = Field(default_factory=list)
    resolution: Literal["priority", "manual", "unresolved"] = "unresolved"
    chosen: Any = None
    reason: str | None = None

    @property
    def ref(self) -> str:
        return f"{self.kind}:{'/'.join(self.key)}#{self.field}"


class FactSnapshot(FormatModel):
    """多源合并后的稳定输入。``compose`` 只读本模型，不直接接触事实包。"""

    format_family: ClassVar[str] = "fact"

    project_id: str
    merged: list[Fact] = Field(default_factory=list, description="去重与裁决后的对象集合")
    conflicts: list[ConflictRecord] = Field(default_factory=list)
    source_digests: dict[str, str] = Field(default_factory=dict, description="事实源标识 -> 摘要")
    digest: str = Field(description="合并结果的整体摘要")

    def by_kind(self, kind: str) -> list[Fact]:
        return [fact for fact in self.merged if fact.kind == kind]

    def unresolved_conflicts(self) -> list[ConflictRecord]:
        """返回尚未裁决的冲突——它们必须出现在裁决队列里。"""
        return [record for record in self.conflicts if record.resolution == "unresolved"]


__all__ = [
    "AssetRef",
    "ConflictCandidate",
    "ConflictRecord",
    "Fact",
    "FactBundle",
    "FactSnapshot",
    "KindBinding",
    "SourceConfig",
    "SourceType",
]
