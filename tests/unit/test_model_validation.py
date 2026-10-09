"""领域对象校验与档案双向校验（DD-6.3 / DD-7.5）。"""

from __future__ import annotations

import pytest

from docfidelity.contracts.domain import DomainModel, DomainObject, FieldSchema
from docfidelity.contracts.errors import EngineError, ErrorCode
from docfidelity.contracts.profile import GenreSpec
from docfidelity.model import (
    DomainModelRegistry,
    collect_findings,
    validate_object,
    validate_objects,
    validate_profile,
)
from tests.fixtures.domain_fixture import api_models, api_objects, api_profile

pytestmark = pytest.mark.unit


@pytest.fixture
def registry() -> DomainModelRegistry:
    return DomainModelRegistry(api_models())


def _endpoint(**fields: object) -> DomainObject:
    payload: dict[str, object] = {"module": "order", "path": "/orders", "method": "POST"}
    payload.update(fields)
    return DomainObject(kind="endpoint", key=["order", "/orders"], fields=payload)


class TestValidateObject:
    def test_a_well_formed_object_passes(self, registry: DomainModelRegistry) -> None:
        validate_object(_endpoint(), registry)

    def test_unknown_kind(self, registry: DomainModelRegistry) -> None:
        with pytest.raises(EngineError) as caught:
            validate_object(DomainObject(kind="ghost", key=["x"]), registry)
        assert caught.value.code == ErrorCode.DOMAIN_KIND_UNKNOWN

    def test_key_arity_must_match_the_declaration(self, registry: DomainModelRegistry) -> None:
        with pytest.raises(EngineError) as caught:
            validate_object(DomainObject(kind="endpoint", key=["order"]), registry)
        assert caught.value.code == ErrorCode.DOMAIN_KEY_MISSING
        assert caught.value.where["declared_key"] == ["module", "path"]

    def test_a_blank_key_segment_is_rejected(self, registry: DomainModelRegistry) -> None:
        """标识键留空会让两条不同记录被折叠成一条——必须在这里拦住。"""
        with pytest.raises(EngineError) as caught:
            validate_object(DomainObject(kind="endpoint", key=["order", "   "]), registry)
        assert caught.value.code == ErrorCode.DOMAIN_KEY_MISSING
        assert caught.value.where["blank_key_fields"] == ["path"]

    def test_bool_is_not_an_int(self, registry: DomainModelRegistry) -> None:
        """Python 里 ``True`` 是 ``int`` 的实例。

        直接 ``isinstance(value, int)`` 会让"应为整数、实为布尔"这种错误一路通过，
        直到渲染层把它写成 ``1``——那是错的，而且没人会发现。
        """
        with pytest.raises(EngineError) as caught:
            validate_object(_endpoint(score=True), registry)
        assert caught.value.code == ErrorCode.DOMAIN_TYPE_MISMATCH
        assert caught.value.where["declared_type"] == "int"

    def test_type_mismatch(self, registry: DomainModelRegistry) -> None:
        with pytest.raises(EngineError) as caught:
            validate_object(_endpoint(summary=123), registry)
        assert caught.value.code == ErrorCode.DOMAIN_TYPE_MISMATCH
        assert caught.value.where["field"] == "summary"

    def test_an_out_of_domain_enum_value_is_a_warning_not_a_failure(
        self, registry: DomainModelRegistry
    ) -> None:
        """``enum`` 越界走 Finding 而不是抛错。

        事实源用了个同义词就让人整份接入失败，代价远大于收益；但也不能当作没事——
        它必须以"待确认"的形态被看见。
        """
        obj = _endpoint(auth="oauth2")
        validate_object(obj, registry)
        findings = collect_findings([obj], registry)
        assert [finding.severity for finding in findings] == ["minor"]
        assert "auth" in findings[0].message

    def test_undeclared_fields_are_kept_not_rejected(self, registry: DomainModelRegistry) -> None:
        """领域模型可以比事实源窄（前向兼容，需求 10.6）。"""
        validate_object(_endpoint(future_field=1), registry)

    def test_list_members_must_be_objects(self, registry: DomainModelRegistry) -> None:
        with pytest.raises(EngineError) as caught:
            validate_object(_endpoint(parameters=["nope"]), registry)
        assert caught.value.code == ErrorCode.DOMAIN_TYPE_MISMATCH
        assert caught.value.where["index"] == 0

    def test_list_members_must_carry_their_key(self, registry: DomainModelRegistry) -> None:
        with pytest.raises(EngineError) as caught:
            validate_object(_endpoint(parameters=[{"type": "str"}]), registry)
        assert caught.value.code == ErrorCode.DOMAIN_KEY_MISSING
        assert caught.value.where["member_kind"] == "parameter"

    def test_nested_objects_are_checked_against_their_declaration(self) -> None:
        nested = DomainModel(
            kind="outer",
            title="外层",
            key=["id"],
            fields={
                "id": FieldSchema(type="str"),
                "detail": FieldSchema(
                    type="object", fields={"note": FieldSchema(type="str", required=True)}
                ),
            },
        )
        registry = DomainModelRegistry([nested])
        with pytest.raises(EngineError) as caught:
            validate_object(
                DomainObject(kind="outer", key=["a"], fields={"detail": {"note": 7}}), registry
            )
        assert caught.value.code == ErrorCode.DOMAIN_TYPE_MISMATCH
        assert caught.value.where["nested_field"] == "note"


class TestValidateObjects:
    def test_errors_carry_the_object_ref(self, registry: DomainModelRegistry) -> None:
        """批量校验失败必须能指认到具体条目——否则接入报告的定位等于没有。"""
        good, bad = _endpoint(), _endpoint(path="/x", summary=1)
        bad = bad.model_copy(update={"key": ["order", "/x"]})
        with pytest.raises(EngineError) as caught:
            validate_objects([good, bad], registry)
        assert caught.value.where["object"] == "endpoint:order//x"

    def test_all_fixture_objects_validate(self, registry: DomainModelRegistry) -> None:
        validate_objects(api_objects(), registry)


class TestCollectFindings:
    def test_a_missing_declared_required_field_is_major(
        self, registry: DomainModelRegistry
    ) -> None:
        obj = _endpoint(method="")
        findings = collect_findings([obj], registry)
        assert [finding.severity for finding in findings] == ["major"]
        assert "method" in findings[0].message

    def test_an_unknown_kind_is_skipped_not_crashed(self, registry: DomainModelRegistry) -> None:
        """软告警路径不该因为一个未登记的类型就中断整批收集。"""
        assert collect_findings([DomainObject(kind="ghost", key=["x"])], registry) == []

    def test_findings_follow_the_input_order(self, registry: DomainModelRegistry) -> None:
        first = _endpoint(path="/a", method="")
        first = first.model_copy(update={"key": ["order", "/a"]})
        second = _endpoint(path="/b", method="")
        second = second.model_copy(update={"key": ["order", "/b"]})
        findings = collect_findings([first, second], registry)
        assert "/a" in findings[0].message
        assert "/b" in findings[1].message


class TestValidateProfile:
    def test_the_fixture_profile_is_accepted(self, registry: DomainModelRegistry) -> None:
        assert validate_profile(api_profile(), registry) == []

    def test_an_unregistered_for_each_refuses_registration(
        self, registry: DomainModelRegistry
    ) -> None:
        """必须**拒绝登记**，而不是记一条告警放行。

        放行的表现是"投影安静地返回零个单元"，最终变成"生成的文档少了一整章，
        而所有报告都是绿的"——这类失败没有任何地方会喊出来。
        """
        profile = api_profile()
        broken = profile.model_copy(
            update={
                "projections": [profile.projections[0].model_copy(update={"for_each": "ghost"})]
            }
        )
        with pytest.raises(EngineError) as caught:
            validate_profile(broken, registry)
        assert caught.value.code == ErrorCode.DOMAIN_KIND_UNKNOWN
        assert "projections[0]" in caught.value.message

    def test_an_unregistered_child_for_each_is_also_caught(
        self, registry: DomainModelRegistry
    ) -> None:
        """子投影同样要查——DD-7.5 明写 ``children[].for_each``。"""
        profile = api_profile()
        child = profile.projections[0].children[0].model_copy(update={"for_each": "ghost.field"})
        broken = profile.model_copy(
            update={
                "projections": [profile.projections[0].model_copy(update={"children": [child]})]
            }
        )
        with pytest.raises(EngineError) as caught:
            validate_profile(broken, registry)
        assert "children[0]" in caught.value.message

    def test_a_slot_type_outside_allowed_unit_types_is_reported(
        self, registry: DomainModelRegistry
    ) -> None:
        profile = api_profile()
        narrower = GenreSpec(
            chapter_skeleton=profile.genre.chapter_skeleton,
            allowed_unit_types=["heading", "para"],
            conventions=profile.genre.conventions,
        )
        findings = validate_profile(profile.model_copy(update={"genre": narrower}), registry)
        assert len(findings) == 1
        assert findings[0].severity == "major"
        assert findings[0].rule_id == "FR-M2-23"
        # 两个槽位里只有 apis 声明了 table，overview 的 para 仍在允许范围内。
        assert "apis" in findings[0].message
        assert "overview" not in findings[0].message

    def test_an_empty_allowed_unit_types_means_unconstrained(
        self, registry: DomainModelRegistry
    ) -> None:
        """没声明允许范围就是没约束，不是"什么都不允许"。"""
        profile = api_profile()
        open_genre = GenreSpec(
            chapter_skeleton=profile.genre.chapter_skeleton,
            allowed_unit_types=[],
            conventions=[],
        )
        assert validate_profile(profile.model_copy(update={"genre": open_genre}), registry) == []
