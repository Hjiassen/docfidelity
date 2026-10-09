"""文档类型档案（DocTypeProfile，DD-3.2（3））。

档案把「文档类型」从引擎里彻底剥离：引擎不认识任何具体类型，只执行档案声明的
章节骨架（``genre``）与投影规则（``projections``）。接入一类新文档 = 新增一份档案，
**不改一行引擎代码**（决策六）。

投影 DSL 完整字段：``unit`` ``for_each`` ``group_by`` ``layout`` ``fields`` ``columns``
``order_by`` ``filter`` ``required`` ``children``。

.. important::
   ``layout`` 的枚举语义**由渲染层实现**，档案只能取既定值，不能自定义语义。
"""

from __future__ import annotations

from typing import ClassVar, Literal

from pydantic import Field, model_validator

from .base import FormatModel
from .template import SlotSpec

ProjectionLayout = Literal["one_section_per", "one_table_per", "one_row_per", "figure"]
"""投影布局。语义由渲染层实现：
``one_section_per`` 每条一个章节；``one_table_per`` 每条一张表；``one_row_per`` 每条一行；
``figure`` 每条一张图及其图注。"""

ProjectionUnit = Literal["section", "table", "row", "figure", "screenshot", "para", "bullets"]


class AppliesStatement(FormatModel):
    """适用范围三条判据（需求 2.6.1）。

    导出档案包时**必须携带**本结构，用于向使用者说明"什么模板 / 什么内容源
    适用这套档案"。三条任一不成立即视为不适配。
    """

    layout_source: str = Field(description="版式来源：模板编号 @ 版本")
    structure_enumerable: str = Field(
        description="章节骨架为何可枚举（可指向 genre.chapter_skeleton）"
    )
    content_traceable: str = Field(description="内容来源为何可追溯（列出事实源）")


class Convention(FormatModel):
    """体例约定：可被门禁规则引用的具名约束。"""

    id: str = Field(description="约定标识，门禁规则通过它引用本条")
    rule: str = Field(description="人类可读的约定描述")


class ChapterSkeletonItem(FormatModel):
    """章节骨架的一项。"""

    level: int = Field(ge=1)
    title: str
    slots: list[SlotSpec] = Field(default_factory=list)


class GenreSpec(FormatModel):
    """体例规格：章节骨架 + 允许的单元类型 + 约定。"""

    chapter_skeleton: list[ChapterSkeletonItem] = Field(default_factory=list)
    allowed_unit_types: list[str] = Field(default_factory=list)
    conventions: list[Convention] = Field(default_factory=list)


class ProjectionSpec(FormatModel):
    """投影规则：如何把领域对象映射为内容单元。

    ``for_each`` 指向领域集合（``"<kind>"`` 或 ``"<kind>.<字段>"``）；
    引擎按 ``order_by`` 排序、``filter`` 过滤、``required`` 校验必需字段，
    并递归处理 ``children``。``unit_id`` 由 ``for_each`` 键与投影序号确定性派生。

    ``slot`` 决定本投影产出的内容单元**落到骨架的哪个槽位**（DD-8.1 的
    ``units_by_slot``）。为空时落入保留槽位 ``"default"``——而"default" 这类未被任何
    章节声明的槽位不会被装配，故 ``compose`` 会为这些单元记一条 Finding
    （静默丢内容是本项目反复踩的坑，见 1.2 各条）。
    """

    unit: ProjectionUnit
    for_each: str = Field(description="领域集合引用，如 '<kind>' 或 '<kind>.<列表字段>'")
    group_by: str | None = Field(default=None, description="分组字段；分组结果决定章节层次")
    layout: ProjectionLayout
    slot: str | None = Field(
        default=None,
        description="内容单元落位的槽位 id（对应 SlotSpec.slot_id）；为空时落入保留槽位 'default'",
    )
    fields: list[str] = Field(default_factory=list, description="取用的字段，顺序即呈现顺序")
    columns: list[str] = Field(default_factory=list, description="table 布局的列定义")
    order_by: str | None = Field(default=None, description="排序字段")
    filter: dict[str, list[str]] = Field(
        default_factory=dict,
        description="过滤条件：字段名 -> 允许取值集合（白名单语义，非表达式）",
    )
    required: list[str] = Field(default_factory=list, description="缺失即进入裁决队列的字段")
    children: list[ProjectionSpec] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_layout_consistency(self) -> ProjectionSpec:
        if self.unit == "table" and not self.columns:
            raise ValueError("unit=table 的投影必须给出 columns")
        if self.unit != "table" and self.columns:
            raise ValueError(f"unit={self.unit} 的投影不应给出 columns")
        if self.layout == "one_table_per" and self.unit != "table":
            raise ValueError("layout=one_table_per 只对 unit=table 有意义")
        return self


class Bindings(FormatModel):
    """档案与外部资产的绑定。缺失即报 ``E_PROFILE_*_MISSING``。"""

    template_packages: list[str] = Field(
        default_factory=list, description="可用模板包引用列表，形如 template_id@version"
    )
    rule_sets: list[str] = Field(default_factory=list, description="门禁规则集标识")
    terminology_scope: Literal["project", "global", "none"] = "project"


class ProfileFingerprint(FormatModel):
    """档案指纹，用于"改档案后需重跑哪些文档"的漂移判定。"""

    digest: str
    sections: dict[str, str] = Field(default_factory=dict)


class DocTypeProfile(FormatModel):
    """文档类型档案。项目级可覆盖全局同名档案（DD-16.3 配置合并）。"""

    format_family: ClassVar[str] = "profile"

    profile_id: str
    doc_type: str = Field(description="文档类型标识；引擎对该取值不做任何假设")
    title: str = Field(description="中文显示名")
    applies: AppliesStatement
    genre: GenreSpec
    projections: list[ProjectionSpec] = Field(default_factory=list)
    bindings: Bindings = Field(default_factory=Bindings)
    fingerprint: ProfileFingerprint | None = None

    @model_validator(mode="after")
    def _check_projection_uniqueness(self) -> DocTypeProfile:
        seen: set[tuple[str, str]] = set()
        dupes: list[str] = []
        for spec in self.projections:
            marker = (spec.unit, spec.for_each)
            if marker in seen:
                dupes.append(f"{spec.unit}<-{spec.for_each}")
            seen.add(marker)
        if dupes:
            raise ValueError(f"同一档案内存在重复投影（unit + for_each）：{dupes}")
        return self

    def required_template_refs(self) -> list[str]:
        """本档案声明需要的模板包引用。"""
        return list(self.bindings.template_packages)


ProjectionSpec.model_rebuild()
ChapterSkeletonItem.model_rebuild()


__all__ = [
    "AppliesStatement",
    "Bindings",
    "ChapterSkeletonItem",
    "Convention",
    "DocTypeProfile",
    "GenreSpec",
    "ProfileFingerprint",
    "ProjectionLayout",
    "ProjectionSpec",
    "ProjectionUnit",
]
