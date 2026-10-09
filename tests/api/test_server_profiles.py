"""文档类型档案用例与端点（FR-M2-23~27；DD-13.2 的 ``profiles/*``）。

五条引脚：

① **登记前必须过校验**——引用了未登记的模板包即拒（``E_PROFILE_TEMPLATE_MISSING``）、
   引用了未登记的领域类型也即拒（``E_DOMAIN_KIND_UNKNOWN``），因为放行等于让投影安静地
   返回空集合，最终表现为"文档少了一整章而报告全绿"；
② **做不了的校验必须说出来**——但生产路径下**没有做不了的**：领域模型库与模板库都
   给全时 ``skipped`` 必须是**空的**，空才是"全部查过了"（硬规则 11）。库是空的那种
   情况传的是**空注册表**而不是 ``None``，故它表现为一次实实在在的拒绝，不是跳过；
③ **版本号是引用键**——同 ``profile_id@version`` 换内容即冲突（``E_VERSION_CONFLICT``
   带出旧/新指纹），内容相同则幂等；覆盖是一次**显式选择**（``on_conflict="overwrite"``）；
④ **废弃只标不删**，且理由必填（无理由的废弃在三个月后与误操作无法区分）；
⑤ **导出件字节可复现**，并携带"适用判据"（FR-M2-25 / 需求 2.6.1）。

登记一份档案**先要有一份领域模型库**（第 1 条按 FR-M2-23 在登记期校验 ``for_each``）。
故本文件里凡是要成功登记的用例都先调 :func:`~tests.fixtures.server_fixture.seed_domain_models`；
``register`` 小工具按 HTTP 层的做法**从库里读**这两份输入，而不是自己拼一个。

另有一组"两台机器上都会过"的对立断言：``repo`` 夹具参数化两种仓储，
同一次登记在内存与落库实现下给出同一结果。
"""

from __future__ import annotations

import io
import zipfile
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from docfidelity.contracts.errors import EngineError, ErrorCode
from docfidelity.contracts.profile import Bindings, DocTypeProfile, ProjectionSpec
from docfidelity.pipeline import fingerprint_profile
from server.main import create_app
from server.profiles import PROFILE_FILENAME, ProfileRegistration
from server.services import (
    AdjudicationMixin,
    ArtifactMixin,
    DomainModelMixin,
    MaintenanceMixin,
    ProfileMixin,
    Services,
    SourceMixin,
    TemplateImportMixin,
    TemplateLibraryMixin,
)
from server.settings import ServerSettings
from server.storage import MemoryRepository, RegisteredTemplate, Repository
from tests.fixtures.domain_fixture import api_profile
from tests.fixtures.server_fixture import (
    Harness,
    build_harness,
    build_registry,
    seed_domain_models,
    seed_domain_models_via_http,
)

pytestmark = pytest.mark.api


@pytest.fixture
def harness(tmp_path: Path, repo: Repository) -> Harness:
    return build_harness(tmp_path, repository=repo)


def expect_error(code: str, fn: Callable[[], object]) -> EngineError:
    with pytest.raises(EngineError) as caught:
        fn()
    assert caught.value.code == code, caught.value.message
    return caught.value


# --------------------------------------------------------------- 夹具与小工具
def unbound_profile() -> DocTypeProfile:
    """不引用任何模板包的档案：用于"只测档案这一件事"的用例。

    ``api_profile`` 绑着 ``TPL-API@1.0``，而空模板库下这个引用必然缺失——那是有意的
    正面/反面夹具，故要一个"能独立通过"的变体时显式解绑，而不是去放宽校验。
    """
    return api_profile().model_copy(
        update={"bindings": Bindings(template_packages=[], rule_sets=["base_style"])}
    )


def seed_template(harness: Harness, ref: str = "TPL-API@1.0") -> None:
    """往模板库放一条登记（只给引用即可——档案校验读的就是 ``ref``）。

    直接落仓储而不是走导入，是因为本文件测的是**档案**；"模板怎么进库"已由
    ``test_server_templates.py`` 覆盖，再走一遍导入只会让本文件的失败原因变模糊。
    """
    template_id, _, version = ref.partition("@")
    harness.repository.add_template(
        RegisteredTemplate(
            template_id=template_id,
            version=version,
            package_hash="seed",
            source_hash="seed",
            package_dir=str(harness.settings.templates_root / template_id / version),
            registered_at=datetime(2026, 9, 20, 9, 0, tzinfo=timezone.utc),
        )
    )


def register(
    harness: Harness, document: DocTypeProfile | None = None, **kwargs: object
) -> ProfileRegistration:
    """按 HTTP 层的做法登记：**先读库拿到引用集与领域模型**，再交给用例层。

    这两行正是硬规则 22 的口径——校验函数（``pipeline.profiles``）刻意不认识仓储，
    "谁去读库"只有一处。测试若绕开它自己拼 ``known_template_refs`` / ``domain_models``，
    验的就是另一条路径（而那条路径在生产里不存在）。
    """
    refs = [item.ref for item in harness.services.list_templates()]
    domain_models = harness.services.domain_declarations()
    return harness.services.register_profile(
        document=document if document is not None else unbound_profile(),
        known_template_refs=refs,
        domain_models=domain_models,
        **kwargs,  # type: ignore[arg-type]
    )


def with_moved_projection(profile: DocTypeProfile) -> DocTypeProfile:
    """改**投影**的档案变体——即"内容真的变了"的那一种。

    刻意不拿 ``title`` / ``applies`` 当变更点：它们**不参与指纹**（``pipeline.PROFILE_SECTIONS``
    只含 ``genre`` / ``projections`` / ``bindings``；``applies`` 是人写的适用判据说明，
    改措辞不该让 FR-M2-26 的"要重跑哪些文档"多出一批）。用标题去断言"冲突"会验到一个
    错的口径——它本该被判成幂等。
    """
    return profile.model_copy(
        update={
            "projections": [
                ProjectionSpec(unit="section", for_each="endpoint", layout="one_section_per")
            ]
        }
    )


# ================================================================== 一、登记
class TestRegister:
    def test_registers_and_writes_the_document_on_disk(self, harness: Harness) -> None:
        seed_domain_models(harness)
        receipt = register(harness)
        assert receipt.ref == "api-reference@1.0"
        assert receipt.fingerprint
        # 档案本体落在磁盘上，且**含已算好的指纹**——收件方据此复算核对。
        target = Path(harness.settings.profiles_root) / "api-reference" / "1.0" / PROFILE_FILENAME
        assert target.is_file()
        stored = DocTypeProfile.model_validate_json(target.read_text(encoding="utf-8"))
        assert stored.fingerprint is not None
        assert stored.fingerprint.digest == receipt.fingerprint
        assert stored.profile_id == "api-reference"

    def test_an_empty_domain_library_blocks_the_registration(self, harness: Harness) -> None:
        """**本文件最要紧的一条**：``for_each`` 引用的领域类型未登记 → **拒绝登记**。

        FR-M2-23 的原话是"未登记即拒绝登记"，理由是放行等于让投影安静地返回空集合，
        最终表现为"文档少了一整章而报告全绿"。

        "库是空的"传的是**空注册表**而不是 ``None``（硬规则 11：空集 ≠ 没提供），
        所以这里必须是一次实实在在的失败，而不是一条 ``skipped``。
        """
        error = expect_error(ErrorCode.DOMAIN_KIND_UNKNOWN, lambda: register(harness))
        assert error.where["for_each"] == "endpoint"
        assert error.where["registered"] == []
        assert error.remediation

    def test_all_checks_run_once_both_libraries_are_populated(self, harness: Harness) -> None:
        """两份输入都给全时 ``skipped`` 必须**空**——空才是"全部查过了"。

        这条与上一条是一对：把"做不了"与"做了没问题"分开，靠的不是措辞，
        而是**两种输入走两条路**（空注册表 vs ``None``）。
        """
        seed_domain_models(harness)
        receipt = register(harness)
        assert receipt.skipped == []
        assert receipt.findings == []

    def test_rejects_a_binding_to_an_unregistered_template(self, harness: Harness) -> None:
        """模板库是空的 → ``TPL-API@1.0`` 缺失 → 拒。"""
        seed_domain_models(harness)
        error = expect_error(
            ErrorCode.PROFILE_TEMPLATE_MISSING,
            lambda: register(harness, api_profile()),
        )
        assert "TPL-API@1.0" in error.where["missing"]
        assert error.remediation

    def test_accepts_the_same_profile_once_the_template_is_registered(
        self, harness: Harness
    ) -> None:
        """正对照：把那份模板包放进库，同一次提交就通过了。"""
        seed_domain_models(harness)
        seed_template(harness)
        receipt = register(harness, api_profile())
        assert receipt.ref == "api-reference@1.0"

    def test_empty_profile_id_is_rejected(self, harness: Harness) -> None:
        blank = unbound_profile().model_copy(update={"profile_id": "   "})
        expect_error(ErrorCode.PROFILE_INVALID, lambda: register(harness, blank))

    def test_profile_id_is_validated_before_being_a_path(self, harness: Harness) -> None:
        """硬约束 14：id 当路径片段用**先校验**（唯一实现 ``path_segment``）。"""
        for bad in ("a:b", "a/b", "a\\b"):
            broken = unbound_profile().model_copy(update={"profile_id": bad})
            expect_error(ErrorCode.CONFIG_INVALID, lambda b=broken: register(harness, b))

    def test_unknown_conflict_policy_is_rejected(self, harness: Harness) -> None:
        expect_error(
            ErrorCode.CONFIG_INVALID,
            lambda: register(harness, on_conflict="merge"),
        )

    def test_same_content_is_registered_idempotently(self, harness: Harness) -> None:
        seed_domain_models(harness)
        first = register(harness)
        second = register(harness)
        assert first.replaced is False
        assert second.replaced is False
        assert [item.version for item in harness.services.list_profiles()] == ["1.0"]

    def test_changed_content_aborts_with_both_fingerprints(self, harness: Harness) -> None:
        seed_domain_models(harness)
        register(harness)
        error = expect_error(
            ErrorCode.VERSION_CONFLICT,
            lambda: register(harness, with_moved_projection(unbound_profile())),
        )
        # 冲突回执必须能支撑界面上那句"旧版可能已被既有文档引用"。
        assert error.where["existing_fingerprint"] != error.where["incoming_fingerprint"]
        assert error.where["registered_at"]
        assert error.where["profile_ref"] == "api-reference@1.0"

    def test_overwrite_replaces_and_flags_it(self, harness: Harness) -> None:
        seed_domain_models(harness)
        first = register(harness)
        changed = with_moved_projection(unbound_profile())
        second = register(harness, changed, on_conflict="overwrite")
        assert second.replaced is True
        assert second.fingerprint != first.fingerprint
        stored = harness.services.profile_document("api-reference")
        assert stored.projections == changed.projections

    def test_a_new_version_coexists_with_the_old_one(self, harness: Harness) -> None:
        seed_domain_models(harness)
        register(harness)
        register(
            harness, unbound_profile().model_copy(update={"title": "接口参考 v2"}), version="2.0"
        )
        refs = [item.ref for item in harness.services.list_profiles()]
        assert refs == ["api-reference@1.0", "api-reference@2.0"]
        # ``version`` 留空取最新一版（同刻登记时以版本号为平局键）。
        assert harness.services.get_profile("api-reference").version == "2.0"

    def test_out_of_range_slot_unit_types_are_reported_as_findings(self, harness: Harness) -> None:
        """``findings`` 与 ``skipped`` 是两件事：前者是"查了、结论是小毛病"。

        第 1、3 条校验跑起来之后，这条才第一次**真的**可能非空——槽位声明了一个
        ``allowed_unit_types`` 之外的单元类型（档案内部自相矛盾）。

        它**不**阻塞登记：可能只是档案还没写完，但要让人看见。于是回执的形状是
        ``skipped == []``（全查了）且 ``findings != []``（查出来的毛病）——
        两者一起才说明"这次校验真的跑过，而且有结论"。
        """
        seed_domain_models(harness)
        base = unbound_profile()
        strict = base.model_copy(
            update={
                "genre": base.genre.model_copy(update={"allowed_unit_types": ["heading", "para"]})
            }
        )
        receipt = register(harness, strict)
        assert receipt.skipped == []
        assert any("table" in text for text in receipt.findings), receipt.findings


# ================================================================== 二、列表
class TestList:
    def test_filters_by_doc_type(self, harness: Harness) -> None:
        seed_domain_models(harness)
        register(harness)
        other = unbound_profile().model_copy(
            update={"profile_id": "cookbook", "doc_type": "cookbook", "title": "菜谱"}
        )
        register(harness, other)
        assert [item.ref for item in harness.services.list_profiles(doc_type="cookbook")] == [
            "cookbook@1.0"
        ]

    def test_deprecated_versions_are_hidden_when_asked(self, harness: Harness) -> None:
        seed_domain_models(harness)
        register(harness)
        harness.services.deprecate_profile("api-reference", "1.0", reason="被 2.0 取代")
        assert harness.services.list_profiles(include_deprecated=False) == []
        assert len(harness.services.list_profiles()) == 1


# ================================================================== 三、废弃
class TestDeprecate:
    def test_requires_a_reason(self, harness: Harness) -> None:
        seed_domain_models(harness)
        register(harness)
        expect_error(
            ErrorCode.PROFILE_INVALID,
            lambda: harness.services.deprecate_profile("api-reference", "1.0", reason="  "),
        )

    def test_marks_the_entry_and_keeps_the_body(self, harness: Harness) -> None:
        """只标不删：档案版本是历史产物的引用键，删掉它等于让"这份文档按哪份体例产出"
        永久失去答案。故本体必须还在磁盘上。"""
        seed_domain_models(harness)
        register(harness)
        entry = harness.services.deprecate_profile(
            "api-reference", "1.0", reason="体例已换", operator="森"
        )
        assert entry.deprecated is True
        assert entry.deprecated_reason == "体例已换"
        target = Path(harness.settings.profiles_root) / "api-reference" / "1.0" / PROFILE_FILENAME
        assert target.is_file()

    def test_is_idempotent_and_keeps_the_first_reason(self, harness: Harness) -> None:
        seed_domain_models(harness)
        register(harness)
        harness.services.deprecate_profile("api-reference", "1.0", reason="第一次")
        again = harness.services.deprecate_profile("api-reference", "1.0", reason="第二次")
        assert again.deprecated_reason == "第一次"

    def test_overwrite_clears_the_deprecation_mark(self, harness: Harness) -> None:
        """覆盖是"重新提交"的正常路径：不清标记会得到一份**不可用**的新档案
        （界面按废弃过滤，于是它列不出来，而库里明明有）。"""
        seed_domain_models(harness)
        register(harness)
        harness.services.deprecate_profile("api-reference", "1.0", reason="先废弃")
        receipt = register(
            harness, with_moved_projection(unbound_profile()), on_conflict="overwrite"
        )
        assert receipt.replaced is True
        assert harness.services.get_profile("api-reference").deprecated is False

    def test_unknown_version_is_not_found(self, harness: Harness) -> None:
        seed_domain_models(harness)
        register(harness)
        expect_error(
            ErrorCode.NOT_FOUND,
            lambda: harness.services.deprecate_profile("api-reference", "9.9", reason="无此版本"),
        )


# ================================================================== 四、导出
class TestExport:
    def _entries(self, harness: Harness) -> dict[str, bytes]:
        name, payload = harness.services.export_profile("api-reference")
        assert name == "api-reference@1.0.zip"
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            return {item.filename: archive.read(item.filename) for item in archive.infolist()}

    def test_zip_carries_the_document_and_the_applicability_note(self, harness: Harness) -> None:
        seed_domain_models(harness)
        register(harness)
        entries = self._entries(harness)
        assert PROFILE_FILENAME in entries
        assert "适用判据.md" in entries
        # 说明是**给人读的**：标题里带档案标识与名称，转手几次后还能认出这是哪份档案。
        note = entries["适用判据.md"].decode("utf-8")
        assert "api-reference" in note
        assert "接口参考" in note
        assert "三条判据" in note

    def test_exported_document_recomputes_to_the_same_fingerprint(self, harness: Harness) -> None:
        """收件方的核对动作：拿包里的 ``profile.json`` 复算指纹，对得上才是同一份。"""
        seed_domain_models(harness)
        receipt = register(harness)
        entries = self._entries(harness)
        document = DocTypeProfile.model_validate_json(entries[PROFILE_FILENAME].decode("utf-8"))
        assert fingerprint_profile(document).digest == receipt.fingerprint

    def test_is_byte_identical_across_exports(self, harness: Harness) -> None:
        """导出件是"交给第三方复现"的凭据：同一份包两次导出**字节相同**。

        故 zip 时刻被钉死、条目按名排序（唯一实现 ``platform.fs.zip_directory``）。
        """
        seed_domain_models(harness)
        register(harness)
        _, first = harness.services.export_profile("api-reference")
        _, second = harness.services.export_profile("api-reference")
        assert first == second

    def test_unknown_profile_is_not_found(self, harness: Harness) -> None:
        expect_error(ErrorCode.NOT_FOUND, lambda: harness.services.export_profile("nope"))


# ============================================================== 五、HTTP 层
class TestHttp:
    """端点形状与状态码（DD-13.2）。走真 HTTP，验证路由、路径顺序与响应模型。"""

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

    @staticmethod
    def _seed_domain_models(client: TestClient) -> None:
        """领域类型必须先入库：档案的 ``for_each`` 按 FR-M2-23 在登记期就要校验。

        走**真 HTTP**（而不是直接写仓储），因为"端点上能不能登记领域模型"本身也是
        本轮的交付面之一——两个端点在同一条链路上，才验得到"登记完立刻能用来校验档案"。
        """
        seed_domain_models_via_http(client)

    def test_register_list_detail_deprecate_and_export(self, client: TestClient) -> None:
        self._seed_domain_models(client)
        created = client.post(
            "/api/v1/profiles",
            json={"profile": unbound_profile().model_dump(mode="json"), "author": "森"},
        )
        assert created.status_code == 201, created.text
        body = created.json()
        assert body["ref"] == "api-reference@1.0"
        assert body["fingerprint"]
        # 库都给全时 **skipped 必须为空**——空才是"全部查过了"（硬规则 11 的另一半）。
        assert body["skipped"] == []
        assert body["findings"] == []
        assert "profile_dir" not in body, "落点不外泄（DD-13.7）"

        library = client.get("/api/v1/profiles")
        assert library.status_code == 200
        assert library.json()["count"] == 1
        assert library.json()["items"][0]["ref"] == "api-reference@1.0"

        detail = client.get("/api/v1/profiles/api-reference")
        assert detail.status_code == 200
        detail_body = detail.json()
        assert detail_body["document"]["profile_id"] == "api-reference"
        assert [item["version"] for item in detail_body["versions"]] == ["1.0"]

        # ``/export`` 声明在 ``/{pid}`` 之前：否则 ``export`` 会被当成一个档案标识。
        exported = client.get("/api/v1/profiles/api-reference/export")
        assert exported.status_code == 200
        assert exported.headers["content-type"] == "application/zip"
        with zipfile.ZipFile(io.BytesIO(exported.content)) as archive:
            assert "profile.json" in archive.namelist()
            assert "适用判据.md" in archive.namelist()

        deprecated = client.post(
            "/api/v1/profiles/api-reference/versions/1.0/deprecate",
            json={"reason": "被 2.0 取代", "operator": "森"},
        )
        assert deprecated.status_code == 200, deprecated.text
        assert deprecated.json()["deprecated"] is True
        assert deprecated.json()["deprecated_reason"] == "被 2.0 取代"

        hidden = client.get("/api/v1/profiles", params={"include_deprecated": False})
        assert hidden.json()["count"] == 0

    def test_unregistered_template_binding_returns_422(self, client: TestClient) -> None:
        """端点在提交前读库拿引用集（硬规则 22 的同一份判据），故空模板库下必然拒。"""
        self._seed_domain_models(client)
        response = client.post(
            "/api/v1/profiles",
            json={"profile": api_profile().model_dump(mode="json")},
        )
        assert response.status_code == 422, response.text
        assert response.json()["code"] == ErrorCode.PROFILE_TEMPLATE_MISSING

    def test_missing_profile_returns_404(self, client: TestClient) -> None:
        response = client.get("/api/v1/profiles/nope")
        assert response.status_code == 404
        assert response.json()["code"] == ErrorCode.NOT_FOUND

    def test_deprecate_without_a_reason_is_a_422(self, client: TestClient) -> None:
        self._seed_domain_models(client)
        client.post(
            "/api/v1/profiles",
            json={"profile": unbound_profile().model_dump(mode="json")},
        )
        response = client.post(
            "/api/v1/profiles/api-reference/versions/1.0/deprecate",
            json={"reason": ""},
        )
        assert response.status_code == 422, response.text

    def test_version_that_is_not_a_path_segment_is_rejected_on_register(
        self, client: TestClient
    ) -> None:
        """版本号会当目录名用，故**登记前**先校验（硬约束 14，唯一实现 ``path_segment``）。"""
        response = client.post(
            "/api/v1/profiles",
            json={"profile": unbound_profile().model_dump(mode="json"), "version": "a:b"},
        )
        assert response.status_code == 422, response.text
        assert response.json()["code"] == ErrorCode.CONFIG_INVALID

    def test_reading_an_unknown_version_is_a_404_not_a_path_error(self, client: TestClient) -> None:
        """读路径**不做**路径片段校验，因为那里没有路径拼接。

        版本号只与库里的取值**比相等**（命中的那个自带登记时校验过的目录），故 ``a:b``
        在这里是一次普通的"查无此版本"。守卫该在写路径上——把它加在读路径只是多一处
        会与写路径漂移的判据。
        """
        self._seed_domain_models(client)
        client.post(
            "/api/v1/profiles",
            json={"profile": unbound_profile().model_dump(mode="json")},
        )
        response = client.get("/api/v1/profiles/api-reference", params={"version": "a:b"})
        assert response.status_code == 404
        assert response.json()["code"] == ErrorCode.NOT_FOUND

    def test_openapi_publishes_the_profile_group(self, client: TestClient) -> None:
        schema = client.get("/openapi.json").json()
        paths = schema["paths"]
        assert "/api/v1/profiles" in paths
        assert "/api/v1/profiles/{profile_id}/export" in paths
        assert "/api/v1/profiles/{profile_id}/versions/{version}/deprecate" in paths
        # 领域模型是**先于**档案的一层（档案的 for_each 引用它），故它自成一个分组。
        assert "/api/v1/domain-models" in paths
        assert "/api/v1/domain-models/{kind}/deprecate" in paths
        tags = {tag["name"] for tag in schema["tags"]}
        assert "profiles" in tags
        assert "sources" in tags
        assert "domain-models" in tags


# ================================================================== 六、装配
def test_all_mixins_compose_into_one_services_object() -> None:
    """全部 mixin 拼成同一个 ``Services``（同一份状态），且**方法名不重名**。

    重名会被 dataclass 的 MRO 悄悄选一个，而那种错在调用处完全看不出来——只会以
    "某个端点行为莫名其妙不对"的形式被发现。故用一次反射钉住，而不是靠人眼过一遍。

    清单写成**类对象**而不是类名字符串：字符串拼错了（改名后没跟着改）只会让这个
    用例悄悄少查一个 mixin，而它恰恰是那种"不出声就没用了"的守卫。
    """
    mixins = {
        MaintenanceMixin,
        AdjudicationMixin,
        TemplateImportMixin,
        TemplateLibraryMixin,
        ProfileMixin,
        DomainModelMixin,
        SourceMixin,
        ArtifactMixin,
    }
    assert mixins <= set(Services.__mro__)

    owners: dict[str, str] = {}
    duplicates: list[str] = []
    for base in Services.__mro__:
        if base not in mixins:
            continue
        for name in vars(base):
            if name.startswith("_") or not callable(vars(base)[name]):
                continue
            if name in owners and owners[name] != base.__name__:
                duplicates.append(f"{name}: {owners[name]} vs {base.__name__}")
            owners[name] = base.__name__
    assert duplicates == [], duplicates


def test_both_new_mixins_are_reachable_from_the_same_object(harness: Harness) -> None:
    """档案与来源的能力挂在同一个对象上——端点层注入的就是它，没有第二份状态。"""
    for name in ("register_profile", "list_profiles", "add_source", "probe", "source_items"):
        assert callable(getattr(harness.services, name))
