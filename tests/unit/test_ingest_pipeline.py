"""归一化、合并与端到端（事实包 → 领域对象 → 投影）的单测。

端到端那条测试是本轮的**判据所在**：给定真实来源文件，接入器产出事实包，合并成快照，
归一为领域对象，再喂给 P0-06 的投影求值器产出内容单元。任何一环断掉，
"事实包可产出"这句话就只是文档里的一个词。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from docfidelity.contracts.errors import EngineError, ErrorCode
from docfidelity.contracts.fact import Fact, FactBundle
from docfidelity.ingest import (
    CONFLICT_RULE_ID,
    ConflictRules,
    PriorityTable,
    build_bundle,
    default_registry,
    merge,
    normalize,
    run_connector,
)
from docfidelity.model import DomainModelRegistry, EvalContext, evaluate
from tests.fixtures.domain_fixture import api_models, api_profile
from tests.fixtures.ingest_fixture import ast_config, build_workspace, openapi_config

pytestmark = pytest.mark.unit

ENDPOINT_KIND = "endpoint"


@pytest.fixture
def registry() -> DomainModelRegistry:
    return DomainModelRegistry([*api_models()])


def _endpoint_fact(
    source: str,
    *,
    path: str = "/orders",
    priority: int = 0,
    verified: bool = True,
    locator: str = "L1",
    **fields: object,
) -> Fact:
    payload: dict[str, object] = {
        "module": "order",
        "path": path,
        "method": "GET",
        "summary": "查询",
    }
    payload.update(fields)
    return Fact(
        source=source,
        kind=ENDPOINT_KIND,
        key=["order", path],
        payload=payload,
        locator=locator,
        priority=priority,
        verified=verified,
    )


# -- 归一化 ----------------------------------------------------------------


def test_normalize_preserves_input_order_and_origin(registry: DomainModelRegistry) -> None:
    facts = [
        _endpoint_fact("a", path="/b"),
        _endpoint_fact("a", path="/a"),
    ]
    objects = normalize(facts, registry)
    assert [obj.key[1] for obj in objects] == ["/b", "/a"]
    assert objects[0].origin is not None
    assert objects[0].origin.source == "a"
    assert objects[0].origin.locator == "L1"


def test_normalize_maps_every_field_verbatim(registry: DomainModelRegistry) -> None:
    objects = normalize([_endpoint_fact("a", extra={"nested": 1})], registry)
    assert objects[0].get("extra") == {"nested": 1}


def test_unknown_kind_fails_with_location(registry: DomainModelRegistry) -> None:
    fact = Fact(source="a", kind="ghost", key=["x"], payload={}, locator="L9")
    with pytest.raises(EngineError) as info:
        normalize([fact], registry)
    assert info.value.code == ErrorCode.DOMAIN_KIND_UNKNOWN
    assert info.value.where["source"] == "a"
    assert info.value.where["locator"] == "L9"


def test_blank_key_fails_with_location(registry: DomainModelRegistry) -> None:
    fact = _endpoint_fact("a", locator="sheet!B7")
    broken = fact.model_copy(update={"key": ["order", ""]})
    with pytest.raises(EngineError) as info:
        normalize([broken], registry)
    assert info.value.code == ErrorCode.DOMAIN_KEY_MISSING
    assert info.value.where["locator"] == "sheet!B7"


def test_key_arity_mismatch_fails(registry: DomainModelRegistry) -> None:
    fact = _endpoint_fact("a")
    broken = fact.model_copy(update={"key": ["order"]})
    with pytest.raises(EngineError) as info:
        normalize([broken], registry)
    assert info.value.code == ErrorCode.DOMAIN_KEY_MISSING


def test_type_mismatch_fails_with_location(registry: DomainModelRegistry) -> None:
    fact = _endpoint_fact("a", locator="openapi#/paths/1", score="ten")
    with pytest.raises(EngineError) as info:
        normalize([fact], registry)
    assert info.value.code == ErrorCode.DOMAIN_TYPE_MISMATCH
    assert info.value.where["locator"] == "openapi#/paths/1"


def test_extra_undeclared_fields_are_kept_not_rejected(registry: DomainModelRegistry) -> None:
    objects = normalize([_endpoint_fact("a", brand_new="v")], registry)
    assert objects[0].get("brand_new") == "v"


# -- 合并 ------------------------------------------------------------------


def _bundle(source: str, facts: list[Fact]) -> FactBundle:
    return build_bundle(source_id=source, source_type="openapi", facts=facts)


def test_single_source_passes_through() -> None:
    snapshot, findings = merge([_bundle("a", [_endpoint_fact("a")])], project_id="p")
    assert findings == []
    assert snapshot.conflicts == []
    assert len(snapshot.merged) == 1
    assert snapshot.merged[0].payload["summary"] == "查询"


def test_equal_values_across_sources_are_not_a_conflict() -> None:
    facts = [_endpoint_fact("a"), _endpoint_fact("b")]
    snapshot, findings = merge([_bundle("a", facts[:1]), _bundle("b", facts[1:])], project_id="p")
    assert snapshot.conflicts == []
    assert findings == []
    assert len(snapshot.merged) == 1


def test_priority_table_resolves_a_conflict() -> None:
    facts = [
        _endpoint_fact("a", summary="来源A", priority=1),
        _endpoint_fact("b", summary="来源B", priority=5),
    ]
    snapshot, findings = merge(
        [_bundle("a", facts[:1]), _bundle("b", facts[1:])],
        project_id="p",
        priorities=PriorityTable(by_source={"a": 1, "b": 5}),
    )
    assert findings == []
    assert len(snapshot.conflicts) == 1
    record = snapshot.conflicts[0]
    assert record.resolution == "priority"
    assert record.chosen == "来源B"
    assert record.field == "summary"
    assert snapshot.merged[0].payload["summary"] == "来源B"
    assert snapshot.merged[0].verified is True


def test_fact_priority_is_used_without_a_table() -> None:
    facts = [
        _endpoint_fact("a", summary="来源A", priority=9),
        _endpoint_fact("b", summary="来源B", priority=2),
    ]
    snapshot, findings = merge([_bundle("a", facts[:1]), _bundle("b", facts[1:])], project_id="p")
    assert findings == []
    assert snapshot.merged[0].payload["summary"] == "来源A"


def test_unresolvable_tie_goes_to_manual_review() -> None:
    facts = [
        _endpoint_fact("a", summary="来源A", priority=3),
        _endpoint_fact("b", summary="来源B", priority=3),
    ]
    snapshot, findings = merge([_bundle("a", facts[:1]), _bundle("b", facts[1:])], project_id="p")
    assert len(snapshot.conflicts) == 1
    assert snapshot.conflicts[0].resolution == "unresolved"
    assert snapshot.conflicts[0].chosen is None
    assert snapshot.merged[0].verified is False
    assert len(findings) == 1
    assert findings[0].rule_id == CONFLICT_RULE_ID
    assert findings[0].severity == "major"
    assert "summary" in findings[0].message


def test_rules_can_force_a_field_into_manual_review() -> None:
    facts = [
        _endpoint_fact("a", summary="来源A", priority=1),
        _endpoint_fact("b", summary="来源B", priority=9),
    ]
    snapshot, findings = merge(
        [_bundle("a", facts[:1]), _bundle("b", facts[1:])],
        project_id="p",
        rules=ConflictRules(by_field={"summary": "unresolved"}),
    )
    assert snapshot.conflicts[0].resolution == "unresolved"
    # 未决字段**整个不出现**在合并结果里：留一个"随便挑的值"比留空危险得多——
    # 留空会让必填校验喊出来，随便挑的值不会。
    assert "summary" not in snapshot.merged[0].payload
    assert len(findings) == 1


def test_no_priority_conflict_records_candidates_for_every_source() -> None:
    facts = [
        _endpoint_fact("a", summary="来源A", priority=1),
        _endpoint_fact("b", summary="来源B", priority=5),
    ]
    snapshot, _ = merge([_bundle("a", facts[:1]), _bundle("b", facts[1:])], project_id="p")
    sources = [candidate.source for candidate in snapshot.conflicts[0].candidates]
    assert sources == ["b", "a"]  # 按优先级降序，顺序由内容决定


def test_unverified_source_makes_the_merged_object_unverified() -> None:
    facts = [
        _endpoint_fact("a", summary="同一值", verified=False),
        _endpoint_fact("b", summary="同一值", verified=True),
    ]
    snapshot, _ = merge([_bundle("a", facts[:1]), _bundle("b", facts[1:])], project_id="p")
    assert snapshot.merged[0].verified is False


def test_merge_is_order_independent() -> None:
    left = _bundle("a", [_endpoint_fact("a", path="/a")])
    right = _bundle("b", [_endpoint_fact("b", path="/b")])
    forward, _ = merge([left, right], project_id="p")
    backward, _ = merge([right, left], project_id="p")
    assert forward.digest == backward.digest
    assert [fact.key[1] for fact in forward.merged] == ["/a", "/b"]


def test_snapshot_records_source_digests() -> None:
    left = _bundle("a", [_endpoint_fact("a")])
    right = _bundle("b", [_endpoint_fact("b", path="/x")])
    snapshot, _ = merge([left, right], project_id="p")
    assert snapshot.source_digests == {"a": left.digest, "b": right.digest}
    assert snapshot.project_id == "p"


def test_assets_are_unioned_across_sources() -> None:
    facts = [
        _endpoint_fact("a").model_copy(update={"asset_names": ["图 1-1.png"]}),
        _endpoint_fact("b").model_copy(update={"asset_names": ["图 1-2.png", "图 1-1.png"]}),
    ]
    snapshot, _ = merge([_bundle("a", facts[:1]), _bundle("b", facts[1:])], project_id="p")
    assert snapshot.merged[0].asset_names == ["图 1-1.png", "图 1-2.png"]


def test_two_operations_sharing_one_identity_key_become_a_conflict() -> None:
    """同一标识键的两条事实不会被静默丢一条。

    这正是"键选窄了"的可发现形态：两个操作共用一个标识键时，它们在语义上是两条
    记录，在合并里却是一条——不报冲突的话，其中一条会凭空消失而产物看着完整。
    """
    facts = [
        _endpoint_fact("a", method="GET"),
        _endpoint_fact("a", method="POST", locator="L2"),
    ]
    snapshot, findings = merge([_bundle("a", facts)], project_id="p")
    assert len(snapshot.merged) == 1
    assert len(snapshot.conflicts) == 1
    assert snapshot.conflicts[0].field == "method"
    assert snapshot.conflicts[0].resolution == "unresolved"
    assert snapshot.merged[0].verified is False
    assert len(findings) == 1


# -- 端到端 ----------------------------------------------------------------


def test_workspace_sources_reach_projection(tmp_path: Path, registry: DomainModelRegistry) -> None:
    """本轮判据：真实来源文件 → 事实包 → 快照 → 领域对象 → 内容单元。"""
    build_workspace(tmp_path)
    connectors = default_registry()

    bundles: list[FactBundle] = []
    for cfg in (
        openapi_config(),
        ast_config(),
    ):
        # 夹具里的 root 是相对路径，这里换成临时工作区下的绝对路径
        resolved = cfg.model_copy(update={"root": str(tmp_path / str(cfg.root))})
        bundles.append(run_connector(connectors.get(str(cfg.source_type)), resolved))

    assert all(bundle.facts for bundle in bundles)
    snapshot, findings = merge(bundles, project_id="demo")
    assert findings == []
    assert len(snapshot.merged) >= 4

    objects = normalize(snapshot.merged, registry)
    profile = api_profile()
    ctx = EvalContext(domain=registry, profile=profile)
    units = []
    for projection in profile.projections:
        units.extend(evaluate(objects, projection, ctx))

    assert units, "投影必须产出内容单元"
    unit_ids = [unit.unit_id for unit in units]
    assert len(set(unit_ids)) == len(unit_ids), "unit_id 必须唯一"
    tables = [unit for unit in units if unit.type == "table"]
    assert tables, "档案里声明了整表投影，应当产出表格单元"
    assert all(unit.trace for unit in tables), "表格单元必须能追溯到领域对象"
