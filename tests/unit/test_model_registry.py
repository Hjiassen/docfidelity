"""领域模型注册表（DD-6.1 / FR-M1-13）。"""

from __future__ import annotations

import pytest

from docfidelity.contracts.domain import DomainModel, FieldSchema
from docfidelity.contracts.errors import EngineError, ErrorCode
from docfidelity.model import DomainModelRegistry
from tests.fixtures.domain_fixture import api_models, kitchen_models

pytestmark = pytest.mark.unit


@pytest.fixture
def registry() -> DomainModelRegistry:
    return DomainModelRegistry([*api_models(), *kitchen_models()])


def _model(kind: str, title: str = "示例") -> DomainModel:
    return DomainModel(kind=kind, title=title, key=["id"], fields={"id": FieldSchema(type="str")})


class TestLookup:
    def test_get_returns_the_declaration(self, registry: DomainModelRegistry) -> None:
        assert registry.get("endpoint").title == "接口"

    def test_unknown_kind_raises_with_an_actionable_remediation(
        self, registry: DomainModelRegistry
    ) -> None:
        """未知类型必须**抛错**，不能返回空声明。

        返回空声明的后果不是"报错"，而是"投影安静地返回零个单元"，最终表现为
        "生成的文档少了一整章而报告全绿"——这类失败没有任何地方会喊出来。
        """
        with pytest.raises(EngineError) as caught:
            registry.get("no-such-kind")
        assert caught.value.code == ErrorCode.DOMAIN_KIND_UNKNOWN
        assert caught.value.remediation
        assert caught.value.where["registered"] == ["dish", "endpoint", "ingredient", "parameter"]

    def test_has_and_contains_and_len(self, registry: DomainModelRegistry) -> None:
        assert registry.has("endpoint")
        assert not registry.has("no-such-kind")
        assert "endpoint" in registry
        assert 1 not in registry  # type: ignore[operator]  # 非字符串一律不在表中
        assert len(registry) == 4

    def test_kinds_are_sorted(self, registry: DomainModelRegistry) -> None:
        assert registry.kinds() == ["dish", "endpoint", "ingredient", "parameter"]

    def test_kinds_returns_a_fresh_list(self, registry: DomainModelRegistry) -> None:
        """返回值必须与内部状态脱钩。

        返回内部列表的话，调用方一次 ``.clear()`` 就把注册表清空了，而注册表自己
        毫无察觉——之后所有查询都会以"类型未登记"的面目失败。
        """
        registry.kinds().append("bogus")
        assert "bogus" not in registry


class TestRegistration:
    def test_re_registering_the_same_declaration_is_idempotent(self) -> None:
        """配置文件被重复加载是常态，重复登记**相同**声明不该失败。"""
        registry = DomainModelRegistry()
        registry.register(_model("a"))
        registry.register(_model("a"))
        assert len(registry) == 1

    def test_re_registering_a_different_declaration_is_rejected(self) -> None:
        """同一 kind 只能有一份声明。

        允许多份的话，"类型 → 声明"就不再是函数：同一批事实在不同调用点上会被
        按不同 schema 解释，而这种不一致不会在任何地方报错。
        """
        registry = DomainModelRegistry([_model("a", title="甲")])
        with pytest.raises(EngineError) as caught:
            registry.register(_model("a", title="乙"))
        assert caught.value.code == ErrorCode.CONFIG_INVALID
        assert caught.value.where["existing_title"] == "甲"
        assert registry.get("a").title == "甲"


class TestDeclarationAccessors:
    def test_the_three_declaration_groups(self, registry: DomainModelRegistry) -> None:
        assert registry.key_fields("endpoint") == ["module", "path"]
        assert registry.comparable_fields("endpoint") == ["method", "summary", "auth"]
        assert registry.traceable_fields("endpoint") == ["module", "path"]

    def test_field_schema_and_unknown_field(self, registry: DomainModelRegistry) -> None:
        assert registry.field_schema("endpoint", "path") is not None
        assert registry.field_schema("endpoint", "no-such-field") is None

    def test_member_kind_reads_the_of_declaration(self, registry: DomainModelRegistry) -> None:
        assert registry.member_kind("endpoint", "parameters") == "parameter"

    def test_member_kind_none_when_the_field_declares_no_member_type(
        self, registry: DomainModelRegistry
    ) -> None:
        """不是 ``list + of`` 的字段返回 ``None``，由调用方去报错——注册表不猜。"""
        assert registry.member_kind("endpoint", "path") is None
        assert registry.member_kind("endpoint", "no-such-field") is None

    def test_member_kind_walks_nested_object_paths(self) -> None:
        nested = DomainModel(
            kind="outer",
            title="外层",
            key=["id"],
            fields={
                "id": FieldSchema(type="str"),
                "detail": FieldSchema(
                    type="object",
                    fields={
                        "steps": FieldSchema(type="list", of="ingredient"),
                        "note": FieldSchema(type="str"),
                    },
                ),
            },
        )
        registry = DomainModelRegistry([nested, *kitchen_models()])
        assert registry.member_kind("outer", "detail.steps") == "ingredient"
        assert registry.member_kind("outer", "detail.note") is None
        assert registry.member_kind("outer", "detail.nope") is None


class TestTypeAgnostic:
    def test_an_arbitrary_kind_behaves_identically(self) -> None:
        """注册表对从未见过的类型不做任何特殊处理。

        这是"注册表是数据、不是代码"的可执行表述：下面这个 kind 与引擎里的任何
        概念都不搭界，而它拿到的是同一套存取行为。
        """
        registry = DomainModelRegistry([_model("zzz-not-a-real-thing", "任意类型")])
        assert registry.key_fields("zzz-not-a-real-thing") == ["id"]
        assert registry.get("zzz-not-a-real-thing").title == "任意类型"
