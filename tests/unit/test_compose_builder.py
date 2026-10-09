"""装配器端到端（DD-8.1，判据「内容 IR 可产出」）。"""

from __future__ import annotations

import json

import pytest

from docfidelity.compose import SlotPolicy, compose
from docfidelity.contracts.errors import EngineError, ErrorCode
from docfidelity.contracts.fact import FactSnapshot
from docfidelity.contracts.ir import FORBIDDEN_IR_TOKENS
from docfidelity.contracts.profile import DocTypeProfile
from docfidelity.model import DomainModelRegistry
from tests.fixtures.compose_fixture import (
    api_models,
    api_objects,
    api_profile,
    api_snapshot,
    diagram_models,
    diagram_profile,
    diagram_snapshot,
    kitchen_models,
    kitchen_profile,
    kitchen_snapshot,
    snapshot_of,
    terminology,
)

pytestmark = pytest.mark.unit


def _api_registry() -> DomainModelRegistry:
    return DomainModelRegistry(api_models())


def _compose_api(
    snapshot: FactSnapshot | None = None,
    profile: DocTypeProfile | None = None,
    **kwargs: object,
):
    active = profile or api_profile()
    return compose(
        snapshot or api_snapshot(),
        active,
        domain=_api_registry(),
        doc_key="detail",
        template_ref="TPL-API@1.0",
        **kwargs,  # type: ignore[arg-type]
    )


# -- 判据：内容 IR 可产出 -------------------------------------------------


def test_snapshot_becomes_content_ir() -> None:
    result = _compose_api()
    ir = result.ir

    assert [unit.unit_id for unit in ir.units] == ["chapter#0", "chapter#1"]
    assert [unit.text for unit in ir.units] == ["总览", "接口清单"]
    assert ir.project_id == "demo"
    assert ir.doc_key == "detail"
    assert ir.profile_id == "api-compact"
    assert ir.template_ref == "TPL-API@1.0"
    assert ir.source_digests == {"openapi": "d0"}
    assert ir.unresolved == []
    assert result.findings == []


def test_chapter_content_is_nested_under_its_heading() -> None:
    result = _compose_api()

    overview, listing = result.ir.units
    assert [unit.type for unit in overview.children] == ["para"] * 4
    assert [unit.text for unit in overview.children] == [
        "创建订单",
        "查询订单",
        "取消订单",
        "用户列表",
    ]
    assert [unit.type for unit in listing.children] == ["heading", "heading", "table"]


def test_section_headings_sit_one_level_below_their_chapter() -> None:
    """一级章节里的"每模块一节"必须是二级标题，否则渲染期选到的样式会把层次压平。"""
    result = _compose_api()
    listing = result.ir.units[1]

    assert [unit.level for unit in listing.children[:2]] == [2, 2]


def test_unit_ids_are_unique() -> None:
    """产物看上去正常、只有对外引用指向错单元——唯一会喊的就是这条断言。"""
    ir = _compose_api().ir
    ids = [unit.unit_id for unit in ir.walk()]

    assert ir.duplicate_unit_ids() == []
    assert len(set(ids)) == len(ids)
    assert ids


def test_compose_is_deterministic() -> None:
    first = _compose_api().ir.model_dump(mode="json")
    second = _compose_api().ir.model_dump(mode="json")

    assert first == second


def test_nested_table_rows_come_from_the_group() -> None:
    """子投影逐条求值：每个接口拿到**自己**的参数表，不是全组并成一张。"""
    result = _compose_api()
    order_section = result.ir.units[1].children[0]

    tables = [unit for unit in order_section.children if unit.type == "table"]
    assert len(tables) == 3
    assert tables[0].fields["columns"] == ["name", "type", "required", "desc"]
    assert [row[0] for row in tables[0].fields["rows"]] == ["body", "idempotent"]
    assert [row[0] for row in tables[1].fields["rows"]] == ["id"]
    assert tables[2].fields["rows"] == []


# -- 追溯 ---------------------------------------------------------------


def test_traces_cover_every_endpoint_and_leave_no_orphan() -> None:
    result = _compose_api()

    assert result.traces.orphans == []
    assert result.traces.unused == []
    assert "endpoint:order//orders" in {edge.object_ref for edge in result.traces.edges}
    assert all(edge.source == "openapi" for edge in result.traces.edges)


def test_skeleton_headings_are_not_counted_as_orphans() -> None:
    result = _compose_api()

    assert "chapter#0" not in result.traces.orphans
    assert "chapter#1" not in result.traces.orphans


# -- 槽位 ---------------------------------------------------------------


def test_required_slot_without_content_is_major() -> None:
    base = api_profile()
    profile = base.model_copy(update={"projections": base.projections[1:]})

    result = _compose_api(profile=profile)

    messages = [item.message for item in result.findings]
    assert any("overview" in message and "未填充" in message for message in messages)
    assert result.counts_by_severity()["blocker"] == 0


def test_slot_policy_escalates_to_blocker() -> None:
    base = api_profile()
    profile = base.model_copy(update={"projections": base.projections[1:]})

    result = _compose_api(profile=profile, slot_policy=SlotPolicy(True))

    assert result.counts_by_severity()["blocker"] >= 1
    assert result.blockers()


def test_projection_without_slot_lands_in_the_default_slot_and_is_reported() -> None:
    """DD-8.1 的 ``proj.slot or "default"`` 会静默丢内容——这里必须喊出来。"""
    base = api_profile()
    orphaned = base.projections[0].model_copy(update={"slot": None})
    profile = base.model_copy(update={"projections": [orphaned, *base.projections[1:]]})

    result = _compose_api(profile=profile)

    assert any("没有任何槽位接收" in item.message for item in result.findings)
    assert "slot:default" in result.ir.unresolved


# -- 术语 ---------------------------------------------------------------


def test_glossary_is_applied_to_projected_text() -> None:
    objects = api_objects()
    patched = objects[0].model_copy(
        update={"fields": {**objects[0].fields, "summary": "{{软件名}}分析仪整体软件"}}
    )
    snapshot = snapshot_of([patched, *objects[1:]], source="openapi")

    result = _compose_api(snapshot=snapshot, terminology=terminology())

    assert result.ir.units[0].children[0].text == "XXX分析仪整体软件"
    assert result.ir.unresolved == []


def test_unresolved_variable_is_reported_in_the_ir() -> None:
    objects = api_objects()
    patched = objects[0].model_copy(
        update={"fields": {**objects[0].fields, "summary": "{{未登记项}}"}}
    )
    snapshot = snapshot_of([patched, *objects[1:]], source="openapi")

    result = _compose_api(snapshot=snapshot, terminology=terminology())

    assert "var:未登记项" in result.ir.unresolved


def test_profile_scope_none_disables_the_glossary() -> None:
    base = api_profile()
    profile = base.model_copy(
        update={"bindings": base.bindings.model_copy(update={"terminology_scope": "none"})}
    )
    objects = api_objects()
    patched = objects[0].model_copy(
        update={"fields": {**objects[0].fields, "summary": "{{软件名}}就绪"}}
    )
    snapshot = snapshot_of([patched, *objects[1:]], source="openapi")

    result = _compose_api(snapshot=snapshot, profile=profile, terminology=terminology())

    assert result.ir.units[0].children[0].text == "{{软件名}}就绪"
    assert "var:软件名" in result.ir.unresolved


# -- IR 硬约束 -----------------------------------------------------------


def test_purity_words_hit_the_engine_surface_as_blocker_without_raising() -> None:
    """外部词表命中记 blocker 但不抛——"事实数据里出现类型名"不该升级成构建失败。"""
    base = api_profile()
    doctored = base.projections[0].model_copy(update={"fields": ["详细设计"]})
    profile = base.model_copy(update={"projections": [doctored, *base.projections[1:]]})

    result = _compose_api(profile=profile, purity_words=["详细设计"])

    purity = [item for item in result.findings if item.rule_id == "FR-M5-17"]
    assert [item.severity for item in purity] == ["blocker"]
    assert "详细设计" in purity[0].message


def test_style_trace_in_fact_data_aborts_the_build() -> None:
    """IR 带样式痕迹是**引擎缺陷**，按 DD-8.1 的 ``AssertIRClean`` 直接抛。"""
    objects = api_objects()
    patched = objects[0].model_copy(
        update={"fields": {**objects[0].fields, "summary": "宽度 9000 twips"}}
    )
    snapshot = snapshot_of([patched, *objects[1:]], source="openapi")

    with pytest.raises(EngineError) as error:
        _compose_api(snapshot=snapshot)

    assert error.value.code == ErrorCode.INTERNAL


def test_ir_payload_passes_the_forbidden_token_search() -> None:
    """附录 D「IR 纯净」：IR 序列化输出过禁用词检索。"""
    text = json.dumps(_compose_api().ir.model_dump(mode="json"), ensure_ascii=False)

    assert [token for token in FORBIDDEN_IR_TOKENS if token in text] == []


# -- 类型无关 -----------------------------------------------------------


def test_non_software_domain_runs_through_the_same_engine() -> None:
    result = compose(
        kitchen_snapshot(),
        kitchen_profile(),
        domain=DomainModelRegistry(kitchen_models()),
        doc_key="cookbook",
        template_ref="TPL-COOK@2.0",
    )

    assert [unit.text for unit in result.ir.units] == ["菜系"]
    sections = result.ir.units[0].children
    # order_by="minutes"：味噌汤 10 → 日式，番茄炒蛋 15 / 可乐鸡翅 40 → 家常
    assert [unit.text for unit in sections] == ["日式", "家常"]
    bullets = [child for section in sections for child in section.children]
    # 子投影逐条求值：每道菜拿到**自己**的配料要点，不是把整个菜系的配料并成一串
    assert [unit.type for unit in bullets] == ["bullets"] * 3
    assert [unit.fields["items"] for unit in bullets] == [
        ["味噌", "豆腐"],
        ["番茄", "鸡蛋"],
        ["鸡翅"],
    ]
    assert result.ir.unresolved == []
    assert result.traces.orphans == []


def test_diagram_profile_produces_numbered_figures() -> None:
    result = compose(
        diagram_snapshot(),
        diagram_profile(),
        domain=DomainModelRegistry(diagram_models()),
        doc_key="gallery",
        template_ref="TPL-IMG@1.0",
    )

    figures = [unit for unit in result.ir.walk() if unit.type == "figure"]
    # order_by="name" 按码位排序：未编号草图 / 登录流程图 / 订单时序图
    assert [unit.fields["caption"] for unit in figures] == [
        "草图说明",
        "图 1-1 登录流程",
        "图 1-2 订单时序",
    ]
    assert result.counts_by_severity() == {"blocker": 0, "major": 0, "minor": 0}
    assert result.traces.orphans == []
