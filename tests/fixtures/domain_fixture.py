"""领域模型与档案的可复用夹具（测试专用）。

这些名字（``endpoint`` / ``parameter`` / ``dish`` / ``ingredient``）只允许出现在
``tests/fixtures/`` 下——它们本身就是「引擎不得认识任何领域概念」的反证：
引擎跑通下面这两套毫不相干的领域，靠的是它们**声明的** ``kind`` / ``key`` / ``fields``，
而不是它们**叫什么**。

第二套刻意选了非软件域（菜谱）。只测一套软件领域，"引擎与文档类型无关"这句话
就没有被检验过——软件领域的字段名恰好与引擎内部用词重合，很容易掩盖硬编码。
"""

from __future__ import annotations

from docfidelity.contracts.domain import DomainModel, DomainObject, FieldSchema
from docfidelity.contracts.profile import (
    AppliesStatement,
    Bindings,
    ChapterSkeletonItem,
    Convention,
    DocTypeProfile,
    GenreSpec,
    ProjectionSpec,
)
from docfidelity.contracts.template import SlotSpec

__all__ = [
    "api_models",
    "api_objects",
    "api_profile",
    "kitchen_models",
    "kitchen_objects",
    "kitchen_profile",
]


# -- 领域一：接口清单 ------------------------------------------------------


def api_models() -> list[DomainModel]:
    parameter = DomainModel(
        kind="parameter",
        title="参数",
        key=["name"],
        fields={
            "name": FieldSchema(type="str", required=True),
            "type": FieldSchema(type="str", required=True),
            "required": FieldSchema(type="bool"),
            "desc": FieldSchema(type="str"),
        },
        comparable=["type", "required"],
        traceable=["name"],
    )
    endpoint = DomainModel(
        kind="endpoint",
        title="接口",
        key=["module", "path"],
        fields={
            "module": FieldSchema(type="str", required=True),
            "path": FieldSchema(type="str", required=True),
            "method": FieldSchema(
                type="enum", values=["GET", "POST", "PUT", "DELETE"], required=True
            ),
            "summary": FieldSchema(type="str"),
            "auth": FieldSchema(type="enum", values=["none", "token", "sign"]),
            "score": FieldSchema(type="int"),
            "parameters": FieldSchema(type="list", of="parameter"),
        },
        comparable=["method", "summary", "auth"],
        traceable=["module", "path"],
    )
    return [parameter, endpoint]


def api_objects() -> list[DomainObject]:
    """四条接口、两个模块。刻意让 ``auth`` 有差异，好让 filter 真正筛掉东西。"""
    return [
        DomainObject(
            kind="endpoint",
            key=["order", "/orders"],
            fields={
                "module": "order",
                "path": "/orders",
                "method": "POST",
                "summary": "创建订单",
                "auth": "token",
                "score": 10,
                "parameters": [
                    {"name": "body", "type": "object", "required": True, "desc": "订单体"},
                    {
                        "name": "idempotent",
                        "type": "bool",
                        "required": False,
                        "desc": "幂等开关",
                    },
                ],
            },
        ),
        DomainObject(
            kind="endpoint",
            key=["order", "/orders/{id}"],
            fields={
                "module": "order",
                "path": "/orders/{id}",
                "method": "GET",
                "summary": "查询订单",
                "auth": "none",
                "score": 3,
                "parameters": [{"name": "id", "type": "str", "required": True, "desc": "订单号"}],
            },
        ),
        DomainObject(
            kind="endpoint",
            key=["order", "/orders/{id}/cancel"],
            fields={
                "module": "order",
                "path": "/orders/{id}/cancel",
                "method": "PUT",
                "summary": "取消订单",
                "auth": "sign",
                "score": 8,
                "parameters": [],
            },
        ),
        DomainObject(
            kind="endpoint",
            key=["user", "/users"],
            fields={
                "module": "user",
                "path": "/users",
                "method": "GET",
                "summary": "用户列表",
                "auth": "none",
                "score": 1,
                "parameters": [],
            },
        ),
    ]


def api_profile() -> DocTypeProfile:
    """一份"一模块一节 + 每接口一张参数表 + 一张总表"的档案。"""
    return DocTypeProfile(
        profile_id="api-reference",
        doc_type="api-reference",
        title="接口参考",
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
            allowed_unit_types=["heading", "para", "bullets", "table", "figure"],
            conventions=[Convention(id="no_period", rule="表格内描述不带句末句号")],
        ),
        projections=[
            ProjectionSpec(
                unit="section",
                for_each="endpoint",
                group_by="module",
                layout="one_section_per",
                fields=["path", "method", "summary", "count"],
                order_by="path",
                required=["path", "method"],
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
                columns=["module", "path", "method"],
                order_by="path",
            ),
        ],
        bindings=Bindings(template_packages=["TPL-API@1.0"], rule_sets=["base_style"]),
    )


# -- 领域二：菜谱（非软件域，用于证明类型无关） ----------------------------


def kitchen_models() -> list[DomainModel]:
    ingredient = DomainModel(
        kind="ingredient",
        title="配料",
        key=["name"],
        fields={
            "name": FieldSchema(type="str", required=True),
            "amount": FieldSchema(type="float"),
        },
        comparable=["amount"],
        traceable=["name"],
    )
    dish = DomainModel(
        kind="dish",
        title="菜",
        key=["name"],
        fields={
            "name": FieldSchema(type="str", required=True),
            "cuisine": FieldSchema(type="str", required=True),
            "minutes": FieldSchema(type="int", required=True),
            "ingredients": FieldSchema(type="list", of="ingredient"),
        },
        comparable=["cuisine", "minutes"],
        traceable=["name"],
    )
    return [ingredient, dish]


def kitchen_objects() -> list[DomainObject]:
    return [
        DomainObject(
            kind="dish",
            key=["番茄炒蛋"],
            fields={
                "name": "番茄炒蛋",
                "cuisine": "家常",
                "minutes": 15,
                "ingredients": [
                    {"name": "番茄", "amount": 2.0},
                    {"name": "鸡蛋", "amount": 3.0},
                ],
            },
        ),
        DomainObject(
            kind="dish",
            key=["可乐鸡翅"],
            fields={
                "name": "可乐鸡翅",
                "cuisine": "家常",
                "minutes": 40,
                "ingredients": [{"name": "鸡翅", "amount": 8.0}],
            },
        ),
        DomainObject(
            kind="dish",
            key=["味噌汤"],
            fields={
                "name": "味噌汤",
                "cuisine": "日式",
                "minutes": 10,
                "ingredients": [
                    {"name": "味噌", "amount": 1.0},
                    {"name": "豆腐", "amount": 1.0},
                ],
            },
        ),
    ]


def kitchen_profile() -> DocTypeProfile:
    """一菜系一节 + 每道菜一份配料要点。字段名与"接口/参数"毫无关系。"""
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
                            slot_id="dishes",
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
