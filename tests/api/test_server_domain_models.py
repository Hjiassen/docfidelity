"""领域模型库用例与端点（FR-M1-13 / FR-M2-23）。

七条引脚：

① **登记按内容判重复**（硬规则 24）：同 ``kind`` 同内容幂等、不同内容冲突
   （``E_VERSION_CONFLICT`` 带出旧/新指纹），覆盖是一次**显式选择**；
② **覆盖清旧废弃标记**：否则会得到一份"登记成功、却在库里哪都看不见"的声明；
③ **没有版本维度**——这是与模板包 / 档案的**对立断言**：同一 ``kind`` 在库里恒为
   一条，第二次登记是**覆盖**而不是并存（理由：引用它的 ``for_each`` 带不上版本）；
④ **``kind`` 先校验再用**：空 / 首尾空白 / 含 ``:`` ``/`` ``\\`` 直接拒，
   且**不静默规整**（规整会让"我写的名字"与"库里的名字"可以不是同一个字符串）；
⑤ **废弃只标不删**、理由必填、幂等；废弃后**不再进注册表**；
⑥ **声明要能原样穿过仓储**：``fields`` / ``comparable`` / ``traceable`` 逐字段读回来
   （``asdict`` 不递归 pydantic 模型，这条是硬规则 28 的那个坑的现场）；
⑦ **两种仓储等价**（``repo`` 夹具参数化），故上面每一条都在内存与落库下各跑一遍。
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from docfidelity.contracts.domain import DomainModel, FieldSchema
from docfidelity.contracts.errors import EngineError, ErrorCode
from docfidelity.pipeline import domain_registry
from server.domain_models import DomainModelRegistration
from server.main import create_app
from server.services import Services
from server.settings import ServerSettings
from server.storage import MemoryRepository, Repository
from tests.fixtures.domain_fixture import api_models
from tests.fixtures.server_fixture import Harness, build_harness, build_registry, seed_domain_models

pytestmark = pytest.mark.api


@pytest.fixture
def harness(tmp_path: Path, repo: Repository) -> Harness:
    return build_harness(tmp_path, repository=repo)


def expect_error(code: str, fn: Callable[[], object]) -> EngineError:
    with pytest.raises(EngineError) as caught:
        fn()
    assert caught.value.code == code, caught.value.message
    return caught.value


def endpoint_model() -> DomainModel:
    """夹具里的 ``endpoint`` 声明（``api_models()`` 的第二个）。"""
    return next(model for model in api_models() if model.kind == "endpoint")


def register(
    harness: Harness, model: DomainModel | None = None, **kwargs: object
) -> DomainModelRegistration:
    return harness.services.register_domain_model(
        model=endpoint_model() if model is None else model,
        **kwargs,  # type: ignore[arg-type]
    )


# ================================================================== 一、登记
class TestRegister:
    def test_registers_a_declaration(self, harness: Harness) -> None:
        receipt = register(harness)
        assert receipt.kind == "endpoint"
        assert receipt.title == "接口"
        assert receipt.fingerprint
        assert receipt.replaced is False
        assert harness.services.get_domain_model("endpoint").title == "接口"

    def test_same_content_is_idempotent(self, harness: Harness) -> None:
        first = register(harness)
        second = register(harness)
        assert first.replaced is False
        assert second.replaced is False
        assert len(harness.services.list_domain_models()) == 1

    def test_changed_content_aborts_with_both_fingerprints(self, harness: Harness) -> None:
        register(harness)
        changed = endpoint_model().model_copy(update={"title": "接口（新）"})
        error = expect_error(ErrorCode.VERSION_CONFLICT, lambda: register(harness, changed))
        # 冲突回执要能支撑界面上那句"引用该类型的档案会立刻改按新声明校验"。
        assert error.where["existing_fingerprint"] != error.where["incoming_fingerprint"]
        assert error.where["kind"] == "endpoint"
        assert error.remediation

    def test_overwrite_replaces_and_flags_it(self, harness: Harness) -> None:
        first = register(harness)
        changed = endpoint_model().model_copy(update={"title": "接口（新）"})
        second = register(harness, changed, on_conflict="overwrite")
        assert second.replaced is True
        assert second.fingerprint != first.fingerprint
        assert harness.services.get_domain_model("endpoint").title == "接口（新）"

    def test_overwrite_clears_the_deprecation_mark(self, harness: Harness) -> None:
        """覆盖是"重新提交"的正常路径：不清标记会得到一份登记成功、却因废弃而在
        列表与注册表里都看不见的声明（硬规则 25）。"""
        register(harness)
        harness.services.deprecate_domain_model("endpoint", reason="先废弃")
        assert harness.services.domain_declarations() == []
        register(
            harness,
            endpoint_model().model_copy(update={"title": "接口（新）"}),
            on_conflict="overwrite",
        )
        assert harness.services.get_domain_model("endpoint").deprecated is False
        assert [model.kind for model in harness.services.domain_declarations()] == ["endpoint"]

    def test_the_declaration_round_trips_through_the_repository(self, harness: Harness) -> None:
        """``asdict`` 只递归 dataclass——声明本体必须显式 ``model_dump`` 才存得下。

        漏了那一步的报错发生在 ``json.dumps`` 里，离"忘了转声明"有十几层栈帧远，
        故这里把**每一组声明字段**都读回来核一遍，而不是只看 ``kind`` 在不在。
        """
        register(harness)
        stored = harness.services.get_domain_model("endpoint").model
        original = endpoint_model()
        assert stored == original
        assert stored.key == ["module", "path"]
        assert stored.comparable == ["method", "summary", "auth"]
        assert stored.traceable == ["module", "path"]
        assert stored.fields["method"].values == ["GET", "POST", "PUT", "DELETE"]
        assert stored.fields["parameters"].of == "parameter"

    def test_unknown_conflict_policy_is_rejected(self, harness: Harness) -> None:
        expect_error(ErrorCode.CONFIG_INVALID, lambda: register(harness, on_conflict="merge"))

    @pytest.mark.parametrize("bad", ["", "   ", "a:b", "a/b", "a\\b", " endpoint"])
    def test_the_kind_is_validated_before_it_is_used(self, harness: Harness, bad: str) -> None:
        """``kind`` 同时是 URL 路径片段与 ``for_each`` 的引用词，故**先校验再用**。

        ``a:b`` 破的是追溯边 ``<kind>:<key>`` 的反解（拆点在第一个 ``:``），
        ``a/b`` 破的是 ``GET /domain-models/{kind}``（硬规则 14）；
        ``" endpoint"`` 破的是"我写的名字就是库里的名字"。
        """
        broken = endpoint_model().model_copy(update={"kind": bad})
        expect_error(ErrorCode.DOMAIN_MODEL_INVALID, lambda: register(harness, broken))

    def test_the_kind_is_not_silently_trimmed(self, harness: Harness) -> None:
        """对立断言：不做 trim。悄悄规整会让"登记了 ``endpoint``、``for_each: endpoint ``
        却报类型不存在"这类问题无从解释——报错比猜便宜。"""
        broken = endpoint_model().model_copy(update={"kind": "endpoint "})
        expect_error(ErrorCode.DOMAIN_MODEL_INVALID, lambda: register(harness, broken))
        assert harness.services.list_domain_models() == []


# ============================================================ 二、身份（无版本）
class TestIdentity:
    def test_two_declarations_of_the_same_kind_never_coexist(self, harness: Harness) -> None:
        """**与模板包 / 档案的对立断言**：那两者是 ``id@version``，同 id 多版本并存；
        领域模型没有版本维度，同一 ``kind`` 恒为一条——第二次登记只有两种结局：
        幂等，或者冲突（再或者显式覆盖）。绝无"并存"。
        """
        register(harness)
        changed = endpoint_model().model_copy(update={"title": "接口（新）"})
        expect_error(ErrorCode.VERSION_CONFLICT, lambda: register(harness, changed))
        assert len(harness.services.list_domain_models()) == 1
        assert harness.services.get_domain_model("endpoint").title == "接口"

    def test_a_different_title_is_a_different_declaration(self, harness: Harness) -> None:
        """``title`` 也进指纹：它进注册表、进报告，改它就不是同一份声明了。"""
        register(harness)
        expect_error(
            ErrorCode.VERSION_CONFLICT,
            lambda: register(harness, endpoint_model().model_copy(update={"title": "接口 v2"})),
        )

    def test_kinds_are_independent(self, harness: Harness) -> None:
        parameter = next(model for model in api_models() if model.kind == "parameter")
        register(harness, parameter)
        register(harness)
        assert [item.kind for item in harness.services.list_domain_models()] == [
            "endpoint",
            "parameter",
        ]


# ================================================================== 三、列表
class TestList:
    def test_hides_deprecated_when_asked(self, harness: Harness) -> None:
        seed_domain_models(harness)
        harness.services.deprecate_domain_model("parameter", reason="不再使用")
        assert [item.kind for item in harness.services.list_domain_models()] == [
            "endpoint",
            "parameter",
        ]
        assert [
            item.kind for item in harness.services.list_domain_models(include_deprecated=False)
        ] == ["endpoint"]

    def test_domain_declarations_exclude_deprecated(self, harness: Harness) -> None:
        """**"哪些声明进注册表"只有一个判据**（``domain_declarations``）：废弃的不进。

        它若散在调用点，"档案登记时的校验"与"将来的构建期求值"会各取一份，
        而两者不一致的表现很具体——登记说类型不存在、构建却跑起来了。
        """
        seed_domain_models(harness)
        assert {model.kind for model in harness.services.domain_declarations()} == {
            "endpoint",
            "parameter",
        }
        harness.services.deprecate_domain_model("parameter", reason="不再使用")
        assert [model.kind for model in harness.services.domain_declarations()] == ["endpoint"]

    def test_unknown_kind_is_not_found(self, harness: Harness) -> None:
        error = expect_error(
            ErrorCode.NOT_FOUND, lambda: harness.services.get_domain_model("ghost")
        )
        assert error.remediation


# ================================================================== 四、废弃
class TestDeprecate:
    def test_requires_a_reason(self, harness: Harness) -> None:
        register(harness)
        expect_error(
            ErrorCode.DOMAIN_MODEL_INVALID,
            lambda: harness.services.deprecate_domain_model("endpoint", reason="  "),
        )

    def test_marks_and_keeps_the_declaration(self, harness: Harness) -> None:
        """只标不删：类型是已登记档案的引用键与历史产物的追溯前缀。"""
        register(harness)
        entry = harness.services.deprecate_domain_model(
            "endpoint", reason="被别的类型取代", operator="森"
        )
        assert entry.deprecated is True
        assert entry.deprecated_reason == "被别的类型取代"
        # 声明本体仍在库里（连同字段），界面照旧能打开它。
        assert harness.services.get_domain_model("endpoint").model.fields

    def test_is_idempotent_and_keeps_the_first_reason(self, harness: Harness) -> None:
        register(harness)
        harness.services.deprecate_domain_model("endpoint", reason="第一次")
        again = harness.services.deprecate_domain_model("endpoint", reason="第二次")
        assert again.deprecated_reason == "第一次"

    def test_unknown_kind_cannot_be_deprecated(self, harness: Harness) -> None:
        expect_error(
            ErrorCode.NOT_FOUND,
            lambda: harness.services.deprecate_domain_model("ghost", reason="无此类型"),
        )


# ============================================================ 五、注册表装配
class TestRegistryAssembly:
    def test_conflicting_declarations_for_one_kind_are_rejected(self) -> None:
        """装配这一步**不重判**一致性：唯一实现在 ``DomainModelRegistry.register``
        （``E_CONFIG_INVALID``），接缝只把它转发出来。"""
        endpoint = endpoint_model()
        expect_error(
            ErrorCode.CONFIG_INVALID,
            lambda: domain_registry([endpoint, endpoint.model_copy(update={"title": "别名"})]),
        )

    def test_an_empty_library_yields_an_empty_registry_not_none(self) -> None:
        """空集与"没提供"是两件事：前者是"一个类型都没登记"的**结论**，
        后者才是"这次没能力查"。混同会让缺能力伪装成校验通过（硬规则 11）。"""
        registry = domain_registry([])
        assert len(registry) == 0
        assert registry.kinds() == []

    def test_the_registry_answers_from_the_declarations(self, harness: Harness) -> None:
        seed_domain_models(harness)
        registry = domain_registry(harness.services.domain_declarations())
        assert registry.kinds() == ["endpoint", "parameter"]
        assert registry.key_fields("endpoint") == ["module", "path"]
        assert registry.member_kind("endpoint", "parameters") == "parameter"


# ============================================================== 六、HTTP 层
class TestHttp:
    """端点形状与状态码。走真 HTTP，验证路由、路径顺序与响应模型。"""

    @pytest.fixture
    def client(self, tmp_path: Path) -> TestClient:
        root = tmp_path / "http"
        app = create_app(
            settings=ServerSettings(_env_file=None, storage_root=root),
            repository=MemoryRepository(),
            stages=build_registry(),
            root=root,
            start_worker=False,
        )
        return TestClient(app)

    def test_register_list_detail_deprecate(self, client: TestClient) -> None:
        created = client.post(
            "/api/v1/domain-models",
            json={"model": endpoint_model().model_dump(mode="json"), "author": "森"},
        )
        assert created.status_code == 201, created.text
        body = created.json()
        assert body["kind"] == "endpoint"
        assert body["replaced"] is False
        assert "model" not in body, "回执是回执，声明在详情里"

        library = client.get("/api/v1/domain-models")
        assert library.status_code == 200
        listed = library.json()
        assert listed["count"] == 1
        assert listed["active_count"] == 1
        row = listed["items"][0]
        # 声明**整份**透出：界面要回答"有哪些字段、哪些参与比对"，只能从声明里读。
        assert row["field_count"] == len(endpoint_model().fields)
        assert row["model"]["key"] == ["module", "path"]
        assert row["model"]["fields"]["method"]["values"] == ["GET", "POST", "PUT", "DELETE"]
        assert "fingerprint" in row

        detail = client.get("/api/v1/domain-models/endpoint")
        assert detail.status_code == 200
        assert detail.json()["model"]["title"] == "接口"

        deprecated = client.post(
            "/api/v1/domain-models/endpoint/deprecate",
            json={"reason": "被别的类型取代", "operator": "森"},
        )
        assert deprecated.status_code == 200, deprecated.text
        assert deprecated.json()["deprecated"] is True
        assert deprecated.json()["deprecated_reason"] == "被别的类型取代"

        # 废弃的仍在列表里（只标不删），但**不再参与**档案登记校验。
        hidden = client.get("/api/v1/domain-models", params={"include_deprecated": False})
        assert hidden.json()["count"] == 0
        still_there = client.get("/api/v1/domain-models")
        assert still_there.json()["count"] == 1
        assert still_there.json()["active_count"] == 0

    def test_a_changed_declaration_returns_409(self, client: TestClient) -> None:
        payload = {"model": endpoint_model().model_dump(mode="json")}
        assert client.post("/api/v1/domain-models", json=payload).status_code == 201
        changed = endpoint_model().model_copy(update={"title": "接口（新）"})
        response = client.post(
            "/api/v1/domain-models", json={"model": changed.model_dump(mode="json")}
        )
        assert response.status_code == 409, response.text
        assert response.json()["code"] == ErrorCode.VERSION_CONFLICT

    def test_an_invalid_kind_is_a_422(self, client: TestClient) -> None:
        broken = endpoint_model().model_copy(update={"kind": "a:b"})
        response = client.post(
            "/api/v1/domain-models", json={"model": broken.model_dump(mode="json")}
        )
        assert response.status_code == 422, response.text
        assert response.json()["code"] == ErrorCode.DOMAIN_MODEL_INVALID

    def test_deprecate_without_a_reason_is_a_422(self, client: TestClient) -> None:
        client.post(
            "/api/v1/domain-models",
            json={"model": endpoint_model().model_dump(mode="json")},
        )
        response = client.post("/api/v1/domain-models/endpoint/deprecate", json={"reason": ""})
        assert response.status_code == 422, response.text

    def test_an_unknown_kind_is_a_404_on_both_reads(self, client: TestClient) -> None:
        detail = client.get("/api/v1/domain-models/ghost")
        assert detail.status_code == 404
        assert detail.json()["code"] == ErrorCode.NOT_FOUND
        deprecated = client.post(
            "/api/v1/domain-models/ghost/deprecate", json={"reason": "无此类型"}
        )
        assert deprecated.status_code == 404

    def test_request_bodies_reject_unknown_fields(self, client: TestClient) -> None:
        """``_Body`` 的纪律：多传的字段往往意味着前端版本不匹配，一律拒绝。"""
        response = client.post(
            "/api/v1/domain-models",
            json={
                "model": endpoint_model().model_dump(mode="json"),
                "version": "1.0",
            },
        )
        assert response.status_code == 422, response.text


# ============================================================== 七、装配总览
def test_the_domain_library_is_reachable_from_the_same_object(harness: Harness) -> None:
    for name in (
        "register_domain_model",
        "list_domain_models",
        "get_domain_model",
        "domain_declarations",
        "deprecate_domain_model",
    ):
        assert callable(getattr(harness.services, name))


def test_the_library_uses_the_same_conflict_policy_closure() -> None:
    """与模板登记、档案登记**同一份闭集**：三种资产的"重复登记"是同一件事。"""
    from server.domain_models import DOMAIN_MODEL_POLICIES
    from server.templates import CONFLICT_POLICIES

    assert DOMAIN_MODEL_POLICIES is CONFLICT_POLICIES


def test_services_type_is_shared(harness: Harness) -> None:
    """端点层注入的就是这一个对象——没有第二份状态。"""
    assert isinstance(harness.services, Services)


def test_field_schema_is_used_verbatim(harness: Harness) -> None:
    """``FieldSchema`` 原样进库出库（``values`` / ``of`` 都不许在往返里丢）。"""
    model = DomainModel(
        kind="probe",
        title="探针",
        key=["name"],
        fields={
            "name": FieldSchema(type="str", required=True),
            "mode": FieldSchema(type="enum", values=["a", "b"]),
        },
        comparable=["mode"],
        traceable=["name"],
    )
    register(harness, model)
    stored = harness.services.get_domain_model("probe").model
    assert stored.fields["mode"].values == ["a", "b"]
    assert stored.fields["name"].required is True
