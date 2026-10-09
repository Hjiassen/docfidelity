"""投影求值器（DD-6.2 / FR-M3-14 / FR-M3-15）。"""

from __future__ import annotations

from collections.abc import Sequence
from typing import get_args

import pytest

from docfidelity.contracts.domain import DomainObject
from docfidelity.contracts.errors import EngineError, ErrorCode
from docfidelity.contracts.ir import ContentUnit, TableData, UnitType
from docfidelity.contracts.profile import (
    GenreSpec,
    ProjectionSpec,
    ProjectionUnit,
)
from docfidelity.model import (
    DomainModelRegistry,
    EvalContext,
    apply_filter,
    compose_sort,
    content_type_of,
    evaluate,
    resolve_mapping,
)
from tests.fixtures.domain_fixture import (
    api_models,
    api_objects,
    api_profile,
    kitchen_models,
    kitchen_objects,
    kitchen_profile,
)

pytestmark = pytest.mark.unit


@pytest.fixture
def registry() -> DomainModelRegistry:
    return DomainModelRegistry([*api_models(), *kitchen_models()])


def _ctx(
    registry: DomainModelRegistry,
    profile: object | None = None,
    *,
    index: int = 0,
    level: int = 1,
) -> EvalContext:
    return EvalContext(domain=registry, profile=profile, index=index, level=level)  # type: ignore[arg-type]


def _ids(units: Sequence[ContentUnit]) -> list[str]:
    return [unit_id for unit in units for unit_id in unit.unit_ids()]


def _shaped(units: Sequence[ContentUnit]) -> list[tuple[str, str, object, object]]:
    """可用于整体比对的形状指纹：id + 类型 + 文本 + 字段。"""
    return [(unit.unit_id, unit.type, unit.text, unit.fields) for unit in units]


# -- 单元类型映射 ---------------------------------------------------------


class TestContentTypeMapping:
    def test_the_mapping_covers_every_declared_unit(self) -> None:
        """``ProjectionUnit`` 新增取值而映射表没跟上时必须红。

        这不是防御性断言：漏一个取值的表现是 ``content_type_of`` 在运行时抛错，
        而"档案里恰好没用这个取值"的一切测试都会是绿的。
        """
        declared = set(get_args(ProjectionUnit))
        mapped = {content_type_of(unit) for unit in declared}
        assert mapped <= set(get_args(UnitType))
        assert mapped == {
            "heading",
            "table",
            "bullets",
            "para",
            "figure",
            "screenshot",
        }

    def test_an_unknown_unit_raises_instead_of_defaulting(self) -> None:
        with pytest.raises(EngineError) as caught:
            content_type_of("no-such-unit")
        assert caught.value.code == ErrorCode.PROFILE_INVALID

    @pytest.mark.parametrize("unit", get_args(ProjectionUnit))
    def test_every_declared_unit_type_can_actually_be_projected(
        self, registry: DomainModelRegistry, unit: str
    ) -> None:
        """逐个取值真跑一遍。

        静态的映射覆盖检查拦不住"映射表里有、但分组粒度没分类"这类漏项——那种情况
        要到 ``evaluate`` 才会暴露，而档案里恰好没用这个取值时一切测试都是绿的。
        """
        speculative: dict[str, object] = {"unit": unit, "for_each": "endpoint"}
        if unit == "table":
            speculative["layout"] = "one_table_per"
            speculative["columns"] = ["path"]
        else:
            speculative["layout"] = "one_row_per"
            speculative["fields"] = ["path"]
        units = evaluate(api_objects(), ProjectionSpec(**speculative), _ctx(registry))  # type: ignore[arg-type]
        assert units
        assert {item.type for item in units} <= set(get_args(UnitType))


# -- 分组粒度 -------------------------------------------------------------


class TestGranularity:
    def test_group_by_yields_one_unit_per_group_in_first_appearance_order(
        self, registry: DomainModelRegistry
    ) -> None:
        profile = api_profile()
        ctx = _ctx(registry, profile)
        units = evaluate(api_objects(), profile.projections[0], ctx)
        assert [unit.text for unit in units] == ["order", "user"]

    def test_a_section_without_group_by_is_one_unit_per_item(
        self, registry: DomainModelRegistry
    ) -> None:
        """需求 12 章的"一接口一节型"就是这个组合。

        DD-6.2 的字面伪代码（``else {None: items}``）在这里会产出**一节**，
        于是"一接口一节"这条被明确要求的能力无法表达——这是本实现对伪代码的有意偏离。
        """
        proj = ProjectionSpec(
            unit="section", for_each="endpoint", layout="one_section_per", fields=["path"]
        )
        units = evaluate(api_objects(), proj, _ctx(registry))
        assert len(units) == 4
        assert [unit.text for unit in units] == [
            "/orders",
            "/orders/{id}",
            "/orders/{id}/cancel",
            "/users",
        ]

    def test_a_table_without_group_by_holds_every_item(self, registry: DomainModelRegistry) -> None:
        """表格是**聚合**单元：不带 group_by 时是"所有条目一张表"，不是"每条一张表"。"""
        proj = ProjectionSpec(
            unit="table",
            for_each="endpoint",
            layout="one_table_per",
            columns=["module", "path"],
        )
        units = evaluate(api_objects(), proj, _ctx(registry))
        assert len(units) == 1
        assert len(units[0].fields["rows"]) == 4

    def test_trace_names_every_item_of_the_group(self, registry: DomainModelRegistry) -> None:
        profile = api_profile()
        units = evaluate(api_objects(), profile.projections[0], _ctx(registry, profile))
        assert units[0].trace == [
            "endpoint:order//orders",
            "endpoint:order//orders/{id}",
            "endpoint:order//orders/{id}/cancel",
        ]

    def test_level_comes_from_the_context(self, registry: DomainModelRegistry) -> None:
        proj = ProjectionSpec(unit="section", for_each="endpoint", layout="one_section_per")
        ctx = _ctx(registry, index=3, level=2)
        units = evaluate(api_objects(), proj, ctx)
        assert {unit.level for unit in units} == {2}
        assert all(unit.unit_id.endswith("#3") for unit in units)


# -- 排序 -----------------------------------------------------------------


class TestOrdering:
    def test_order_by_sorts(self, registry: DomainModelRegistry) -> None:
        proj = ProjectionSpec(
            unit="table",
            for_each="endpoint",
            layout="one_table_per",
            columns=["score"],
            order_by="score",
        )
        rows = evaluate(api_objects(), proj, _ctx(registry))[0].fields["rows"]
        assert [row[0] for row in rows] == ["1", "3", "8", "10"]

    def test_without_order_by_items_sort_by_key(self, registry: DomainModelRegistry) -> None:
        """不保留入参顺序。

        入参顺序来自事实源合并结果；把它当排序依据的话，"同一份事实、不同合并轮次"
        会产出不同的单元次序，构建随之失去幂等性（NFR-02 / FR-M4-12）。
        """
        proj = ProjectionSpec(
            unit="table", for_each="endpoint", layout="one_table_per", columns=["path"]
        )
        rows = evaluate(api_objects(), proj, _ctx(registry))[0].fields["rows"]
        assert [row[0] for row in rows] == [
            "/orders",
            "/orders/{id}",
            "/orders/{id}/cancel",
            "/users",
        ]

    def test_mixed_types_do_not_raise(self, registry: DomainModelRegistry) -> None:
        """同一个字段混类型是常态（``"3"`` 与 ``3``）；``sorted`` 不能因此抛 ``TypeError``。"""
        objects = [
            DomainObject(kind="endpoint", key=["m", "/a"], fields={"path": "/a", "score": 3}),
            DomainObject(kind="endpoint", key=["m", "/b"], fields={"path": "/b", "score": "x"}),
            DomainObject(kind="endpoint", key=["m", "/c"], fields={"path": "/c"}),
        ]
        ordered = sorted(objects, key=compose_sort("score"))
        assert [item.get("path") for item in ordered] == ["/c", "/a", "/b"]


# -- 过滤 -----------------------------------------------------------------


class TestFilter:
    def test_whitelist_semantics(self, registry: DomainModelRegistry) -> None:
        kept, absent = apply_filter(api_objects(), {"auth": ["none"]})
        assert [item.get("path") for item in kept] == ["/orders/{id}", "/users"]
        assert absent == []

    def test_an_empty_filter_keeps_everything(self, registry: DomainModelRegistry) -> None:
        kept, absent = apply_filter(api_objects(), {})
        assert len(kept) == 4
        assert absent == []

    def test_an_absent_field_does_not_silently_drop_content(
        self, registry: DomainModelRegistry
    ) -> None:
        """过滤字段整体缺失时**不排除**对象，并把这件事回报出来。

        若按"白名单没命中就排除"，``filter: {scope: [...]}`` 遇上一份没有 ``scope``
        字段的事实源会让整批内容凭空消失，而报告毫无提示。取舍是：宁可不筛，
        也要让内容出现在产物里（人看得见），并且记一条待确认。
        """
        objects = [
            DomainObject(kind="endpoint", key=["m", "/a"], fields={"path": "/a"}),
            DomainObject(kind="endpoint", key=["m", "/b"], fields={"path": "/b", "auth": "token"}),
        ]
        kept, absent = apply_filter(objects, {"auth": ["none"]})
        assert [item.get("path") for item in kept] == ["/a"]
        assert absent == ["auth"]

    def test_the_absent_field_is_reported_as_a_finding(self, registry: DomainModelRegistry) -> None:
        proj = ProjectionSpec(
            unit="table",
            for_each="endpoint",
            layout="one_table_per",
            columns=["path"],
            filter={"no_such_field": ["x"]},
        )
        ctx = _ctx(registry)
        units = evaluate(api_objects(), proj, ctx)
        assert len(units[0].fields["rows"]) == 4  # 一个都没被排除
        assert [finding.severity for finding in ctx.findings] == ["minor"]
        assert ctx.findings[0].rule_id == "FR-M3-14"


# -- 字段映射与聚合 -------------------------------------------------------


class TestResolveMapping:
    def test_keys_are_the_declaration_text(self, registry: DomainModelRegistry) -> None:
        """键保持原文，``ProjectionSpec.required`` 才查得到聚合项（DD-6.2 的 ``mapping.get(f)``）。"""
        mapping = resolve_mapping(["count", "sum(score)", "path"], api_objects())
        assert list(mapping) == ["count", "sum(score)", "path"]

    def test_aggregates(self, registry: DomainModelRegistry) -> None:
        mapping = resolve_mapping(
            ["count", "count(auth)", "sum(score)", "min(score)", "max(score)", "join(module)"],
            api_objects(),
        )
        assert mapping["count"] == 4
        assert mapping["count(auth)"] == 4
        assert mapping["sum(score)"] == 22.0
        assert mapping["min(score)"] == 1
        assert mapping["max(score)"] == 10
        assert mapping["join(module)"] == "order、user"

    def test_join_deduplicates_but_keeps_order(self, registry: DomainModelRegistry) -> None:
        mapping = resolve_mapping(["join(auth)"], api_objects())
        assert mapping["join(auth)"] == "token、none、sign"

    def test_a_plain_field_comes_from_the_first_item_after_sorting(
        self, registry: DomainModelRegistry
    ) -> None:
        ordered = sorted(api_objects(), key=compose_sort("score"))
        assert resolve_mapping(["path"], ordered)["path"] == "/users"

    def test_an_empty_collection_yields_no_representative(
        self, registry: DomainModelRegistry
    ) -> None:
        assert resolve_mapping(["path", "count"], []) == {"path": None, "count": 0}

    def test_nested_mapping_paths_are_readable(self, registry: DomainModelRegistry) -> None:
        objects = [
            DomainObject(
                kind="endpoint",
                key=["m", "/a"],
                fields={"path": "/a", "detail": {"note": "内层"}},
            )
        ]
        assert resolve_mapping(["detail.note"], objects)["detail.note"] == "内层"

    def test_a_path_that_walks_into_a_collection_yields_none(
        self, registry: DomainModelRegistry
    ) -> None:
        """点路径只在对象上下钻；要遍历集合用 ``for_each``，不要在字段路径里做索引。"""
        assert resolve_mapping(["parameters.0.name"], api_objects())["parameters.0.name"] is None


# -- 子投影 ---------------------------------------------------------------


class TestChildren:
    def test_a_dotted_child_runs_once_per_group_item(self, registry: DomainModelRegistry) -> None:
        profile = api_profile()
        units = evaluate(api_objects(), profile.projections[0], _ctx(registry, profile))
        order_children = units[0].children
        assert len(order_children) == 3
        assert order_children[0].fields["rows"] == [
            ["body", "object", "true", "订单体"],
            ["idempotent", "bool", "false", "幂等开关"],
        ]

    def test_child_unit_ids_are_unique(self, registry: DomainModelRegistry) -> None:
        """子投影的段常常只由 ``for_each`` 派生，一个组里 N 个条目各求值一次就会撞车。

        撞车的后果不是"少一个单元"，而是**两个不同的内容单元共用一个 id**：
        对外引用取到错的那个、追溯链指向错的来源，而产物看上去完全正常。
        这里逐个分组分别求值，正是为了遮住这个 bug。
        """
        profile = api_profile()
        ids: list[str] = []
        for index, proj in enumerate(profile.projections):
            ids.extend(_ids(evaluate(api_objects(), proj, _ctx(registry, profile, index=index))))
        assert len(ids) == len(set(ids))

    def test_a_global_child_runs_once_per_group_not_once_per_item(
        self, registry: DomainModelRegistry
    ) -> None:
        """子投影引用全局集合时不该被重复 N 遍。

        DD-6.2 的 ``for c in children for gi in gitems`` 逐条求值，遇到全局引用会把
        同一张表插进每一节；本实现只在子投影**依赖所在条目**时逐条求值。
        """
        proj = ProjectionSpec(
            unit="section",
            for_each="endpoint",
            group_by="module",
            layout="one_section_per",
            fields=["path"],
            children=[
                ProjectionSpec(
                    unit="table", for_each="parameter", layout="one_table_per", columns=["name"]
                )
            ],
        )
        units = evaluate(api_objects(), proj, _ctx(registry))
        assert len(units) == 2
        assert all(len(unit.children) == 1 for unit in units)

    def test_children_are_one_level_deeper(self, registry: DomainModelRegistry) -> None:
        profile = api_profile()
        ctx = _ctx(registry, profile, level=1)
        units = evaluate(api_objects(), profile.projections[0], ctx)
        child = units[0].children[0]
        grand = ContentUnit(unit_id="x", type="heading", children=units[0].children)
        assert child.level is None  # 表单元不带层级
        assert units[0].level == 1
        assert next(item.unit_id for item in grand.walk()) == "x"


# -- 各单元的载荷 ---------------------------------------------------------


class TestPayloads:
    def test_a_table_unit_parses_back_as_table_data(self, registry: DomainModelRegistry) -> None:
        profile = api_profile()
        unit = evaluate(api_objects(), profile.projections[1], _ctx(registry, profile))[0]
        data = TableData.model_validate(unit.fields)
        assert data.columns == ["module", "path", "method"]
        assert data.rows[0] == ["order", "/orders", "POST"]

    def test_a_row_unit_takes_its_columns_from_fields(self, registry: DomainModelRegistry) -> None:
        """``columns`` 只允许出现在 ``unit=table`` 上，行单元的列只能来自 ``fields``。"""
        proj = ProjectionSpec(
            unit="row", for_each="endpoint", layout="one_row_per", fields=["path", "method"]
        )
        unit = evaluate(api_objects(), proj, _ctx(registry))[0]
        assert unit.type == "table"
        assert unit.fields["columns"] == ["path", "method"]
        assert unit.fields["rows"][0] == ["/orders", "POST"]

    def test_a_bullets_unit_lists_the_group(self, registry: DomainModelRegistry) -> None:
        proj = ProjectionSpec(
            unit="bullets", for_each="endpoint", layout="one_row_per", fields=["path"]
        )
        unit = evaluate(api_objects(), proj, _ctx(registry))[0]
        assert unit.type == "bullets"
        assert unit.fields["items"] == [
            "/orders",
            "/orders/{id}",
            "/orders/{id}/cancel",
            "/users",
        ]

    def test_a_bullets_unit_skips_aggregates(self, registry: DomainModelRegistry) -> None:
        """聚合在组内是常量，展开成 N 条一模一样的要点只是噪音。"""
        objects = [
            DomainObject(kind="endpoint", key=["m", "/a"], fields={"path": "/a", "score": 2}),
            DomainObject(kind="endpoint", key=["m", "/b"], fields={"path": "/b", "score": 4}),
        ]
        proj = ProjectionSpec(
            unit="bullets",
            for_each="endpoint",
            layout="one_row_per",
            fields=["count", "path"],
        )
        unit = evaluate(objects, proj, _ctx(registry))[0]
        assert unit.fields["items"] == ["/a", "/b"]

    def test_a_para_unit_uses_the_first_non_blank_value(
        self, registry: DomainModelRegistry
    ) -> None:
        proj = ProjectionSpec(
            unit="para", for_each="endpoint", layout="one_section_per", fields=["summary"]
        )
        units = evaluate(api_objects(), proj, _ctx(registry))
        assert {unit.type for unit in units} == {"para"}
        assert [unit.text for unit in units] == ["创建订单", "查询订单", "取消订单", "用户列表"]

    def test_a_figure_declares_the_image_without_any_layout_numbers(
        self, registry: DomainModelRegistry
    ) -> None:
        """图单元只给像素与 DPI，不给 twips——换算与等比缩放是渲染层的职责（DD-4.4）。

        尺寸与 DPI 走 ``fields`` 声明（和 ``caption`` 一样）：它们在 ``mapping`` 里
        才有键，档案不声明就取不到，不会凭空冒出来。
        """
        objects = [
            DomainObject(
                kind="endpoint",
                key=["m", "/a"],
                fields={"path": "/a", "image_ref": "封面.png", "width_px": 1200, "dpi": 144},
            )
        ]
        proj = ProjectionSpec(
            unit="figure",
            for_each="endpoint",
            layout="figure",
            fields=["image_ref", "width_px", "dpi"],
        )
        unit = evaluate(objects, proj, _ctx(registry))[0]
        assert unit.type == "figure"
        assert unit.fields["image_ref"] == "封面.png"
        assert unit.fields["width_px"] == 1200
        assert unit.fields["dpi"] == 144
        assert "twips" not in unit.model_dump_json()

    def test_a_figure_without_an_image_is_a_major_finding(
        self, registry: DomainModelRegistry
    ) -> None:
        proj = ProjectionSpec(
            unit="figure", for_each="endpoint", layout="figure", fields=["no_such_image"]
        )
        ctx = _ctx(registry)
        units = evaluate(api_objects(), proj, ctx)
        assert len(units) == 4  # 逐条型：一条目一张图
        assert units[0].fields["image_ref"] == ""
        assert {finding.severity for finding in ctx.findings} == {"major"}
        assert len(ctx.findings) == 4


# -- 体例校验 -------------------------------------------------------------


class TestConventionFindings:
    def test_a_missing_required_field_is_major(self, registry: DomainModelRegistry) -> None:
        proj = ProjectionSpec(
            unit="section",
            for_each="endpoint",
            layout="one_section_per",
            fields=["path"],
            required=["path", "no_such_field"],
        )
        ctx = _ctx(registry)
        units = evaluate(api_objects(), proj, ctx)
        assert len(units) == 4  # 逐条型：每条都要单独过一遍必填校验
        assert {finding.rule_id for finding in ctx.findings} == {"FR-M3-15"}
        assert {finding.severity for finding in ctx.findings} == {"major"}
        assert len(ctx.findings) == 4
        assert all("no_such_field" in finding.message for finding in ctx.findings)
        assert all(finding.location.unit_id is not None for finding in ctx.findings)

    def test_a_unit_type_outside_the_genre_is_reported(self, registry: DomainModelRegistry) -> None:
        profile = api_profile()
        narrow = profile.model_copy(
            update={
                "genre": GenreSpec(
                    chapter_skeleton=profile.genre.chapter_skeleton,
                    allowed_unit_types=["heading"],
                    conventions=[],
                )
            }
        )
        ctx = _ctx(registry, narrow)
        evaluate(api_objects(), narrow.projections[1], ctx)  # 一张表 vs 只允许 heading
        assert [finding.rule_id for finding in ctx.findings] == ["FR-M3-15"]
        assert "table" in ctx.findings[0].message

    def test_grouping_a_plain_field_reports_the_lossy_mapping(
        self, registry: DomainModelRegistry
    ) -> None:
        """分组后仍声明逐条字段时，另外 N-1 条的取值没有出口——从产物上看不出来。"""
        profile = api_profile()
        ctx = _ctx(registry, profile)
        evaluate(api_objects(), profile.projections[0], ctx)
        assert [finding.severity for finding in ctx.findings] == ["minor"]
        assert ctx.findings[0].rule_id == "FR-M3-14"
        assert "'path'" in ctx.findings[0].message

    def test_findings_from_children_bubble_up_to_the_outer_context(
        self, registry: DomainModelRegistry
    ) -> None:
        """子上下文共享同一个收集器；否则嵌套里的待确认项会无声消失。"""
        child = ProjectionSpec(
            unit="table",
            for_each="endpoint.parameters",
            layout="one_table_per",
            columns=["name"],
            required=["name", "ghost"],
        )
        proj = ProjectionSpec(
            unit="table",
            for_each="endpoint",
            layout="one_table_per",
            columns=["path"],
            children=[child],
        )
        ctx = _ctx(registry)
        evaluate(api_objects(), proj, ctx)
        assert ctx.findings, "子投影的必填缺值应当出现在最外层上下文的 findings 里"


# -- 确定性 ---------------------------------------------------------------


class TestDeterminism:
    def test_the_same_input_gives_the_same_output(self, registry: DomainModelRegistry) -> None:
        profile = api_profile()
        first = _shaped(evaluate(api_objects(), profile.projections[0], _ctx(registry, profile)))
        second = _shaped(evaluate(api_objects(), profile.projections[0], _ctx(registry, profile)))
        assert first == second

    def test_input_order_does_not_leak_into_the_output(self, registry: DomainModelRegistry) -> None:
        profile = api_profile()
        forward = _shaped(evaluate(api_objects(), profile.projections[0], _ctx(registry, profile)))
        backward = _shaped(
            evaluate(list(reversed(api_objects())), profile.projections[0], _ctx(registry, profile))
        )
        assert forward == backward


# -- 失败姿态 -------------------------------------------------------------


class TestErrors:
    def test_an_unregistered_for_each_fails_loudly(self, registry: DomainModelRegistry) -> None:
        """放行的表现是"投影安静地返回零个单元"——一份少了一整章的文档，报告全绿。"""
        proj = ProjectionSpec(unit="table", for_each="ghost", layout="one_table_per", columns=["a"])
        with pytest.raises(EngineError) as caught:
            evaluate(api_objects(), proj, _ctx(registry))
        assert caught.value.code == ErrorCode.DOMAIN_KIND_UNKNOWN

    def test_a_dotted_for_each_needs_a_declared_member_type(
        self, registry: DomainModelRegistry
    ) -> None:
        proj = ProjectionSpec(
            unit="table", for_each="endpoint.path", layout="one_table_per", columns=["a"]
        )
        with pytest.raises(EngineError) as caught:
            evaluate(api_objects(), proj, _ctx(registry))
        assert caught.value.code == ErrorCode.PROFILE_INVALID

    def test_a_non_collection_field_is_rejected(self, registry: DomainModelRegistry) -> None:
        objects = [
            DomainObject(
                kind="endpoint",
                key=["m", "/a"],
                fields={"path": "/a", "parameters": {"name": "not-a-list"}},
            )
        ]
        proj = ProjectionSpec(
            unit="table",
            for_each="endpoint.parameters",
            layout="one_table_per",
            columns=["name"],
        )
        with pytest.raises(EngineError) as caught:
            evaluate(objects, proj, _ctx(registry))
        assert caught.value.code == ErrorCode.DOMAIN_TYPE_MISMATCH

    def test_nested_members_are_validated_on_the_spot(self, registry: DomainModelRegistry) -> None:
        """嵌套成员进不了接入阶段，所以必须在这里校验。"""
        objects = [
            DomainObject(
                kind="endpoint",
                key=["m", "/a"],
                fields={"path": "/a", "parameters": [{"type": "str"}]},  # 缺 name
            )
        ]
        proj = ProjectionSpec(
            unit="table",
            for_each="endpoint.parameters",
            layout="one_table_per",
            columns=["name"],
        )
        with pytest.raises(EngineError) as caught:
            evaluate(objects, proj, _ctx(registry))
        assert caught.value.code == ErrorCode.DOMAIN_KEY_MISSING


# -- 类型无关（同一套代码跑另一套领域） ------------------------------------


class TestAnotherDomain:
    def test_a_non_software_domain_runs_through_the_same_code(
        self, registry: DomainModelRegistry
    ) -> None:
        profile = kitchen_profile()
        ctx = _ctx(registry, profile)
        units = evaluate(kitchen_objects(), profile.projections[0], ctx)
        assert [unit.text for unit in units] == ["日式", "家常"]
        assert units[0].children[0].fields["items"] == ["味噌", "豆腐"]
        assert ctx.findings == []

    def test_aggregate_works_on_a_disjoint_domain(self, registry: DomainModelRegistry) -> None:
        profile = kitchen_profile()
        units = evaluate(kitchen_objects(), profile.projections[0], _ctx(registry, profile))
        household = next(unit for unit in units if unit.text == "家常")
        assert household.fields["sum(minutes)"] == 55.0
        assert household.fields["count"] == 2
