"""``compose`` 轮的可复用夹具（测试专用）。

与 ``domain_fixture`` 一样，这里出现的 ``endpoint`` / ``dish`` / ``diagram``
只允许出现在 ``tests/fixtures/`` 下：它们正是"引擎不认识任何领域概念"的反证。

三种夹具刻意分开
----------------
* :func:`api_profile` —— 软件域，**声明了 slot**（``ProjectionSpec.slot`` 是 P0-08
  才加的可选字段），覆盖"逐条段落 + 分组章节 + 子表 + 总表"四种形态。
* :func:`kitchen_profile` —— 非软件域，字段名与引擎内部用词毫无重合。只用一套
  软件域测"类型无关"，很容易被"字段名恰好与引擎用词相同"掩盖硬编码。
* :func:`diagram_profile` —— 带图注的档案，专供图注编号与 ``FigureData`` 走通
  （``caption_slot`` 只在领域字段里声明，引擎猜不出来）。
"""

from __future__ import annotations

from datetime import datetime

from docfidelity.contracts.config import Terminology, TerminologyEntry
from docfidelity.contracts.domain import DomainModel, DomainObject, FieldSchema
from docfidelity.contracts.fact import Fact, FactSnapshot
from docfidelity.contracts.profile import (
    AppliesStatement,
    Bindings,
    ChapterSkeletonItem,
    Convention,
    DocTypeProfile,
    GenreSpec,
    ProjectionSpec,
)
from docfidelity.contracts.template import (
    BlockIndex,
    ChapterNode,
    Margins,
    NumberingContract,
    OutlineSkeleton,
    PageSetupContract,
    PageSize,
    PlaceholderList,
    SlotSpec,
    TemplateManifest,
    TemplatePackage,
    UsedStylesContract,
)

from .domain_fixture import api_models, api_objects, kitchen_models, kitchen_objects

__all__ = [
    "api_models",
    "api_objects",
    "api_profile",
    "api_snapshot",
    "diagram_models",
    "diagram_objects",
    "diagram_profile",
    "diagram_snapshot",
    "kitchen_models",
    "kitchen_objects",
    "kitchen_profile",
    "kitchen_snapshot",
    "snapshot_of",
    "template_package",
    "terminology",
]

_FIXED_TIME = datetime(2026, 9, 21, 0, 0, 0)
"""固定时间戳。内核时间只进元数据，不参与逻辑（NFR-02），故测试里写死。"""


# -- 术语表 ---------------------------------------------------------------


def terminology() -> Terminology:
    """两条术语：一条供变量替换，一条供旧称归一。

    ``forbidden`` 与 ``canonical`` **刻意不互为子串**：若 ``forbidden="XX分析仪"`` 而
    ``canonical="XXX分析仪"``，替换会把自己越替越长（"XXX分析仪" 里含 "XX分析仪"）。
    """
    return Terminology(
        scope="project",
        entries=[
            TerminologyEntry(
                term="软件名",
                canonical="XXX分析仪",
                forbidden=["YYY分析仪"],
                note="软件全称",
            ),
            TerminologyEntry(term="模块名", canonical="订单模块", forbidden=["订单模组"]),
        ],
    )


# -- 领域一：接口清单（软件域，带槽位） ------------------------------------


def api_profile() -> DocTypeProfile:
    """一模块一节 + 每接口一张参数表 + 一张总表，且每个投影都声明了 ``slot``。"""
    return DocTypeProfile(
        profile_id="api-compact",
        doc_type="api-compact",
        title="接口参考（槽位版）",
        applies=AppliesStatement(
            layout_source="TPL-API@1.0",
            structure_enumerable="章节骨架见 genre.chapter_skeleton",
            content_traceable="事实源为 OpenAPI 导出",
        ),
        genre=GenreSpec(
            chapter_skeleton=[
                ChapterSkeletonItem(
                    level=1,
                    title="总览",
                    slots=[
                        SlotSpec(
                            slot_id="overview",
                            required=True,
                            unit_types=["para"],
                            locator="chapter[1]",
                        )
                    ],
                ),
                ChapterSkeletonItem(
                    level=1,
                    title="接口清单",
                    slots=[
                        SlotSpec(
                            slot_id="apis",
                            required=True,
                            unit_types=["heading", "table"],
                            locator="chapter[2]",
                        )
                    ],
                ),
            ],
            allowed_unit_types=[
                "heading",
                "para",
                "bullets",
                "table",
                "figure",
                "screenshot",
            ],
            conventions=[Convention(id="no_period", rule="表格内描述不带句末句号")],
        ),
        projections=[
            ProjectionSpec(
                unit="para",
                for_each="endpoint",
                layout="one_row_per",
                slot="overview",
                fields=["summary"],
                order_by="path",
            ),
            ProjectionSpec(
                unit="section",
                for_each="endpoint",
                group_by="module",
                layout="one_section_per",
                slot="apis",
                # 只取聚合项：分组章节的标题是分组键（模块名），逐条字段属于 children。
                # 这里若声明 path/method，会命中 FR-M3-14（"分组后仍声明逐条字段"）——
                # 那是 model 层的正确告警，不该由"干净路径"的夹具去背。
                fields=["count"],
                order_by="path",
                children=[
                    ProjectionSpec(
                        unit="table",
                        for_each="endpoint.parameters",
                        layout="one_table_per",
                        columns=["name", "type", "required", "desc"],
                    )
                ],
            ),
            ProjectionSpec(
                unit="table",
                for_each="endpoint",
                layout="one_table_per",
                slot="apis",
                columns=["module", "path", "method"],
                order_by="path",
            ),
        ],
        bindings=Bindings(template_packages=["TPL-API@1.0"], rule_sets=["base_style"]),
    )


def api_snapshot() -> FactSnapshot:
    return snapshot_of(api_objects(), source="openapi")


# -- 领域二：菜谱（非软件域） ---------------------------------------------


def kitchen_profile() -> DocTypeProfile:
    """一菜系一节 + 每道菜的配料要点；字段名与"接口 / 参数"毫无关系。"""
    return DocTypeProfile(
        profile_id="cookbook",
        doc_type="cookbook",
        title="菜谱",
        applies=AppliesStatement(
            layout_source="TPL-COOK@2.0",
            structure_enumerable="章节骨架见 genre.chapter_skeleton",
            content_traceable="事实源为手工录入清单",
        ),
        genre=GenreSpec(
            chapter_skeleton=[
                ChapterSkeletonItem(
                    level=1,
                    title="菜系",
                    slots=[
                        SlotSpec(
                            slot_id="cuisines",
                            required=True,
                            unit_types=["heading", "bullets"],
                            locator="chapter[1]",
                        )
                    ],
                )
            ],
            allowed_unit_types=["heading", "para", "bullets", "table"],
        ),
        projections=[
            ProjectionSpec(
                unit="section",
                for_each="dish",
                group_by="cuisine",
                layout="one_section_per",
                slot="cuisines",
                fields=["count", "sum(minutes)"],
                order_by="minutes",
                children=[
                    ProjectionSpec(
                        unit="bullets",
                        for_each="dish.ingredients",
                        layout="one_row_per",
                        fields=["name"],
                    )
                ],
            )
        ],
        bindings=Bindings(template_packages=["TPL-COOK@2.0"], rule_sets=["base_style"]),
    )


def kitchen_snapshot() -> FactSnapshot:
    return snapshot_of(kitchen_objects(), source="manual")


# -- 领域三：图与表（供图注编号走通） ------------------------------------


def diagram_models() -> list[DomainModel]:
    diagram = DomainModel(
        kind="diagram",
        title="图",
        key=["name"],
        fields={
            "name": FieldSchema(type="str", required=True),
            "caption": FieldSchema(type="str"),
            "caption_slot": FieldSchema(type="int"),
            "image_ref": FieldSchema(type="asset"),
            "width_px": FieldSchema(type="int"),
            "height_px": FieldSchema(type="int"),
            "dpi": FieldSchema(type="int"),
        },
        comparable=["caption"],
        traceable=["name"],
    )
    return [diagram]


def diagram_objects() -> list[DomainObject]:
    """两幅带编号的图 + 一幅**不参与编号**的图（``caption_slot`` 缺省）。"""
    return [
        DomainObject(
            kind="diagram",
            key=["登录流程图"],
            fields={
                "name": "登录流程图",
                "caption": "登录流程",
                "caption_slot": 0,
                "image_ref": "shot-login",
                "width_px": 640,
                "height_px": 360,
                "dpi": 96,
            },
        ),
        DomainObject(
            kind="diagram",
            key=["订单时序图"],
            fields={
                "name": "订单时序图",
                "caption": "订单时序",
                "caption_slot": 0,
                "image_ref": "shot-order",
                "width_px": 800,
                "height_px": 400,
            },
        ),
        DomainObject(
            kind="diagram",
            key=["未编号草图"],
            fields={
                "name": "未编号草图",
                "caption": "草图说明",
                "image_ref": "shot-sketch",
                "width_px": 320,
                "height_px": 200,
            },
        ),
    ]


def diagram_profile() -> DocTypeProfile:
    """一章图文；`caption_slot` 缺省的图原样保留标题、不占编号。"""
    return DocTypeProfile(
        profile_id="illustrated",
        doc_type="illustrated",
        title="图文说明",
        applies=AppliesStatement(
            layout_source="TPL-IMG@1.0",
            structure_enumerable="章节骨架见 genre.chapter_skeleton",
            content_traceable="事实源为资产登记 + 手工清单",
        ),
        genre=GenreSpec(
            chapter_skeleton=[
                ChapterSkeletonItem(
                    level=1,
                    title="图文说明",
                    slots=[
                        SlotSpec(
                            slot_id="figs",
                            required=True,
                            unit_types=["figure", "screenshot"],
                            locator="chapter[1]",
                        )
                    ],
                )
            ],
            allowed_unit_types=["heading", "para", "bullets", "table", "figure", "screenshot"],
        ),
        projections=[
            ProjectionSpec(
                unit="figure",
                for_each="diagram",
                layout="figure",
                slot="figs",
                fields=["image_ref", "caption", "caption_slot", "width_px", "height_px", "dpi"],
                order_by="name",
            )
        ],
        bindings=Bindings(template_packages=["TPL-IMG@1.0"], rule_sets=["base_style"]),
    )


def diagram_snapshot() -> FactSnapshot:
    return snapshot_of(diagram_objects(), source="assets")


# -- 快照与模板包 ---------------------------------------------------------


def snapshot_of(
    objects: list[DomainObject],
    *,
    source: str = "fixture",
    project_id: str = "demo",
    source_digests: dict[str, str] | None = None,
) -> FactSnapshot:
    """把领域对象摊成 ``Fact`` 再包成快照。

    刻意不直接塞 ``DomainObject``：``compose`` 吃的是 ``FactSnapshot``，经
    ``ingest.normalize`` 摊成领域对象。跳过这一步的测试测不到接入期的硬校验。
    """
    facts = [
        Fact(
            source=source,
            kind=obj.kind,
            key=list(obj.key),
            payload=dict(obj.fields),
            locator=f"{obj.ref}@fixture",
        )
        for obj in objects
    ]
    digests = dict(source_digests) if source_digests is not None else {source: "d0"}
    return FactSnapshot(
        project_id=project_id, merged=facts, source_digests=digests, digest="merge0"
    )


def template_package(
    *,
    slots: list[SlotSpec] | None = None,
    chapter_tree: list[ChapterNode] | None = None,
    template_id: str = "TPL-API",
    version: str = "1.0",
    allowed_block_types: tuple[str, ...] = ("cover", "revision", "toc", "header", "table"),
) -> TemplatePackage:
    """构造一个最小可用的模板包（只为预检与契约查询）。"""
    return TemplatePackage(
        manifest=TemplateManifest(
            template_id=template_id,
            version=version,
            source_hash="src0",
            package_hash="pkg0",
            created_at=_FIXED_TIME,
        ),
        page_setup=PageSetupContract(
            page_size=PageSize(width_twips=11906, height_twips=16838),
            margins=Margins(top_twips=1440, bottom_twips=1440, left_twips=1800, right_twips=1800),
        ),
        outline=OutlineSkeleton(
            chapter_tree=list(chapter_tree or []),
            allowed_block_types=list(allowed_block_types),  # type: ignore[arg-type]
            slots=list(slots or []),
        ),
        numbering=NumberingContract(),
        used_styles=UsedStylesContract(),
        blocks=BlockIndex(),
        placeholders=PlaceholderList(),
        fingerprint=None,
    )
