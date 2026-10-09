"""领域模型与领域对象（DD-3.2（4））。

领域模型是**外部数据**，不是引擎的一部分：``kind`` 完全开放（FR-M1-13），
引擎只按 ``key`` / ``fields`` / ``comparable`` / ``traceable`` 这四组声明行事，
**不得对任何具体 kind 分支**。

本模块只提供示例声明所需的类型定义；实际领域模型以 YAML 档案形式随项目提供，
例如::

    kind: <自定义 kind>
    title: <中文显示名>
    key: [<标识字段>, ...]
    fields:
      <字段名>: {type: str, required: true}
"""

from __future__ import annotations

from typing import Any, ClassVar, Literal

from pydantic import Field, model_validator

from .base import FormatModel

FieldType = Literal[
    "str", "int", "float", "bool", "enum", "list", "object", "datetime", "path", "asset"
]
"""字段类型。``enum`` 必须同时给 ``values``；``list`` 可用 ``of`` 声明成员 kind；

``asset`` 表示工作区内的二进制资产（图片等），值形态为逻辑名而非路径。"""


class FieldSchema(FormatModel):
    """单字段的声明。"""

    type: FieldType
    required: bool = False
    values: list[str] | None = Field(default=None, description="enum 的取值域，必须非空")
    of: str | None = Field(default=None, description="list 的成员 kind（引用另一个领域模型）")
    fields: dict[str, FieldSchema] | None = Field(default=None, description="object 的嵌套字段声明")
    description: str | None = None
    default: Any = None

    @model_validator(mode="after")
    def _check_type_specifics(self) -> FieldSchema:
        if self.type == "enum" and not self.values:
            raise ValueError("type=enum 的字段必须给出非空 values")
        if self.type != "enum" and self.values:
            raise ValueError(f"type={self.type} 的字段不应给出 values（仅 enum 使用）")
        if self.type == "object" and not self.fields:
            raise ValueError("type=object 的字段必须给出 fields")
        if self.type == "list" and not self.of:
            raise ValueError("type=list 的字段必须给出 of（成员 kind）")
        return self


class DomainObjectRef(FormatModel):
    """领域对象的轻量引用，用于 ``ContentUnit.trace`` 与追溯矩阵。

    字符串形式为 ``"<kind>:<key 各段以 / 连接>"``。
    """

    kind: str
    key: list[str]

    @property
    def ref(self) -> str:
        return f"{self.kind}:{'/'.join(self.key)}"

    @classmethod
    def parse(cls, raw: str) -> DomainObjectRef:
        """从 ``"<kind>:<k1>/<k2>"`` 解析。用于测试与调试，不用于主链路。"""
        kind, _, rest = raw.partition(":")
        return cls(kind=kind, key=[part for part in rest.split("/") if part])


class DomainModel(FormatModel):
    """领域模型的**声明**（schema）。``kind`` 开放。"""

    format_family: ClassVar[str] = "domain"

    kind: str = Field(description="领域类型标识；引擎对其取值不做任何假设")
    title: str = Field(description="中文显示名，用于界面与报告")
    key: list[str] = Field(min_length=1, description="标识键字段名，顺序即键序；用于去重与追溯")
    fields: dict[str, FieldSchema] = Field(default_factory=dict)
    comparable: list[str] = Field(
        default_factory=list, description="参与稳定化比对的字段；未列出者不参与 diff"
    )
    traceable: list[str] = Field(
        default_factory=list, description="构成追溯边标识的字段；通常等于 key 的子集"
    )
    description: str | None = None

    @model_validator(mode="after")
    def _check_references(self) -> DomainModel:
        missing = [name for name in self.key if name not in self.fields]
        if missing:
            raise ValueError(f"key 引用了未声明的字段：{missing}")
        for group, names in (("comparable", self.comparable), ("traceable", self.traceable)):
            unknown = [name for name in names if name not in self.fields]
            if unknown:
                raise ValueError(f"{group} 引用了未声明的字段：{unknown}")
        return self

    def key_of(self, fields: dict[str, Any]) -> list[str]:
        """按本模型声明的键序，从字段值中提取标识键。"""
        return [str(fields.get(name, "")) for name in self.key]


class DomainObject(FormatModel):
    """领域模型的**实例**。

    ``fields`` 为字段值（对应 :class:`DomainModel.fields` 的 schema）。
    引擎只按 ``kind`` + ``key`` 索引它，不对具体字段语义做判断。
    """

    format_family: ClassVar[str] = "domain"

    kind: str
    key: list[str] = Field(min_length=1, description="标识键取值，顺序与 DomainModel.key 一致")
    fields: dict[str, Any] = Field(default_factory=dict)
    origin: DomainObjectOrigin | None = None

    @property
    def ref(self) -> str:
        return f"{self.kind}:{'/'.join(self.key)}"

    def get(self, name: str, default: Any = None) -> Any:
        """读取字段值；键不存在时返回 ``default``。"""
        return self.fields.get(name, default)


class DomainObjectOrigin(FormatModel):
    """领域对象在事实源中的落点。"""

    source: str = Field(description="事实源标识")
    locator: str = Field(description="源内定位（文件:行 / JSON 指针 / 单元格）")
    verified: bool = True
    priority: int = Field(default=0, description="冲突裁决优先级，越大越优先")


FieldSchema.model_rebuild()
DomainObject.model_rebuild()


__all__ = [
    "DomainModel",
    "DomainObject",
    "DomainObjectOrigin",
    "DomainObjectRef",
    "FieldSchema",
    "FieldType",
]
