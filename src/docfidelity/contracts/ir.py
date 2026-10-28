"""内容中间表示（Content IR）与内容单元（DD-3.2（2））。

IR 是 ``compose`` 与 ``render`` 之间**唯一**的接口（决策一：编排与渲染解耦）。
``compose`` 只产出 IR，``render`` 只消费 IR。

三条 IR 硬约束（由 ``qa.type_agnostic`` 与 CI 自动检索）
-------------------------------------------------------
1. **无样式痕迹**：不得出现 ``styleId`` / ``w:`` 前缀 / twips / 绝对列宽。
   样式一律由渲染层按模板契约决定；IR 只表达"这是什么内容"。
2. **无领域概念标签**：不得出现领域模型的具体名字（如 endpoint）。
3. **无文档类型名**：不得出现"详细设计""接口测试方案"这类具体类型名。

``unit_id`` 必须由投影路径**确定性派生**（``f"{for_each_key}#{projection_index}"``），
以保证同一输入连续构建产物一致（NFR-02 幂等性）。
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any, ClassVar, Literal

from pydantic import Field, field_validator, model_validator
from typing_extensions import Self

from .base import FormatModel
from .materials import ImageExtent

UnitType = Literal[
    "heading",
    "para",
    "bullets",
    "numbered",
    "table",
    "figure",
    "screenshot",
    "trace",
    "pagebreak",
    "block_ref",
]
"""内容单元类型。``layout`` 枚举由渲染层实现，档案只能取值，不能自定义语义。"""

UNIT_TYPES: tuple[str, ...] = (
    "heading",
    "para",
    "bullets",
    "numbered",
    "table",
    "figure",
    "screenshot",
    "trace",
    "pagebreak",
    "block_ref",
)

#: IR 中禁止出现的样式痕迹（DD-2.3（2）的 IR 侧检索目标）
FORBIDDEN_IR_TOKENS: tuple[str, ...] = ("w:", "twips", "styleId", "pStyle", "tblStyle")


class OriginRef(FormatModel):
    """内容单元的来源事实引用。

    追溯链的最后一跳：``ContentUnit.trace`` 给出领域对象 key，
    ``OriginRef`` 给出该对象在事实源中的落点与可信度。
    """

    source: str = Field(description="事实源标识，如 'openapi' / 'xlsx:用例表'")
    locator: str = Field(description="源内定位，如 'paths./api/x.get' 或 'Sheet1!A12'")
    verified: bool = Field(default=True, description="是否已通过该源的接入校验")
    priority: int = Field(default=0, description="冲突裁决优先级，越大越优先")


class TableData(FormatModel):
    """``type == "table"`` 时 ``fields`` 的约定结构。

    注意：**不含任何列宽信息**——列宽由渲染层按模板表格原型与版心推导，
    IR 给出绝对列宽会让样式保真失效。
    """

    columns: list[str] = Field(description="列标题，顺序即列序")
    rows: list[list[str]] = Field(description="行数据；每行长度应等于 columns 长度")
    caption: str | None = Field(default=None, description="表注文本，编号由渲染层填充")
    caption_slot: int | None = Field(default=None, description="表注编号槽位；None 表示不编号")


class FigureData(FormatModel):
    """``type == "figure"`` / ``"screenshot"`` 时 ``fields`` 的约定结构。

    普通事实使用像素 + DPI；原件图片可携带可信显示上限，等比缩放属渲染层职责。
    """

    image_ref: str = Field(description="工作区内图片逻辑名，由 ingest 落盘并登记")
    caption: str | None = None
    caption_slot: int | None = None
    width_px: int | None = None
    height_px: int | None = None
    dpi: int = Field(default=96, gt=0)
    image_extent: ImageExtent | None = None

    @model_validator(mode="after")
    def validate_extent(self) -> Self:
        """可信原件尺寸和像素声明不能同时决定显示大小。"""
        if self.image_extent is not None and (
            self.width_px is not None or self.height_px is not None
        ):
            raise ValueError("图片不得混用原件显示尺寸与像素尺寸")
        return self


class ContentUnit(FormatModel):
    """IR 的最小单元。可嵌套（``children``），形成章节树。"""

    format_family: ClassVar[str] = "ir"

    unit_id: str = Field(description="稳定 ID，由投影路径派生；必须确定性可复现")
    type: UnitType
    level: int | None = Field(default=None, ge=1, description="heading 专用层级")
    text: str | None = Field(default=None, description="正文文本；表格 / 图片单元为 None")
    fields: dict[str, Any] = Field(
        default_factory=dict,
        description="体例声明的字段。table 见 TableData，figure/screenshot 见 FigureData",
    )
    children: list[ContentUnit] = Field(default_factory=list)
    trace: list[str] = Field(default_factory=list, description="领域对象 key 列表，构成追溯边")
    origin: OriginRef | None = None

    @field_validator("unit_id")
    @classmethod
    def _unit_id_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("unit_id 不得为空——它是对外引用内容单元的唯一句柄")
        return value

    @field_validator("level")
    @classmethod
    def _level_only_for_heading(cls, value: int | None) -> int | None:
        # 语义校验放在 model_validator 之外，避免与字段顺序耦合；此处只做取值域检查
        return value

    def walk(self) -> Iterator[ContentUnit]:
        """深度优先遍历自身与后代。"""
        yield self
        for child in self.children:
            yield from child.walk()

    def unit_ids(self) -> Iterator[str]:
        for unit in self.walk():
            yield unit.unit_id


class ContentIR(FormatModel):
    """一次构建产出的完整内容 IR。

    它同时是**可导出的对外格式**：``docs/spec/content-ir/`` 下有对应 JSON Schema。
    """

    format_family: ClassVar[str] = "ir"

    project_id: str
    doc_key: str = Field(description="项目内的文档条目键，如 'detail' / 'testplan'")
    profile_id: str | None = Field(default=None, description="V1 档案引用；V2 无需提供")
    template_ref: str = Field(description="生成所用的模板包引用 template_id@version")
    units: list[ContentUnit] = Field(default_factory=list)
    source_digests: dict[str, str] = Field(
        default_factory=dict, description="事实源标识 -> 内容摘要，用于判定输入是否变化"
    )
    unresolved: list[str] = Field(
        default_factory=list, description="未能解析的引用（slot_id / trace key），进入裁决队列"
    )

    def walk(self) -> Iterator[ContentUnit]:
        """遍历全部单元（含嵌套）。"""
        for unit in self.units:
            yield from unit.walk()

    def find(self, unit_id: str) -> ContentUnit | None:
        for unit in self.walk():
            if unit.unit_id == unit_id:
                return unit
        return None

    def duplicate_unit_ids(self) -> list[str]:
        """返回重复出现的 ``unit_id``（应为空；非空表示投影路径派生有缺陷）。"""
        seen: set[str] = set()
        dupes: list[str] = []
        for unit in self.walk():
            if unit.unit_id in seen:
                dupes.append(unit.unit_id)
            seen.add(unit.unit_id)
        return dupes


ContentUnit.model_rebuild()


__all__ = [
    "FORBIDDEN_IR_TOKENS",
    "UNIT_TYPES",
    "ContentIR",
    "ContentUnit",
    "FigureData",
    "OriginRef",
    "TableData",
    "UnitType",
]
