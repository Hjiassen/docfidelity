"""事实源用例与端点（DD-5.1 / DD-13.2 的 ``projects/{pid}/sources/*``）。

五条引脚：

① **配置期与接入期分工**——``add_source`` 只查"接入器认不认识、``root`` 解不解得开"，
   **不读文件内容**；于是"来源写错了"能在写库之前答出来，而"今天那份文件被移走了"
   不会让登记失败。真去读文件是 ``probe`` / ``items`` 的事；
② **已存在的同 id 不得静默覆盖**——配置相同即幂等（``created=False``），不同则拒；
   覆盖必须是一次有意识的选择（先删再增），而不是一次笔误的后果；
③ **探针永不抛**——它服务界面上的一个按钮，"连不通"要显示成结果而不是 500；
   但探针与条目浏览器用**同一个解析点**（``SourceGuard``），否则"试接入说通了、
   真接入说找不到"会成为常态。护栏在**读文件之前**把关，故它的码（如
   ``E_UPLOAD_TYPE``）会先于"读失败"出现；
④ **``None`` 与 ``False`` 是两件事**——``last_probe_ok`` 的 ``None`` 是"从没探过"、
   ``False`` 是"探了、没通"，界面据此给不同的下一步动作。而条目浏览器**会抛**：
   读不出来要显示成错误，不能回一份空清单（它会被读成"本来就没有对象"）；
⑤ **条目截断要如实说**（``truncated``），悄悄少给会让使用者以为来源里就这么多对象。

``root`` 相对**存储根**解析（``settings.storage_root``）：试接入发生在作业之外，
此时没有作业工作区可参照。故夹具把 ``build_workspace`` 铺在 ``harness.root`` 下。
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from docfidelity.contracts.errors import EngineError, ErrorCode
from docfidelity.contracts.fact import SourceConfig
from server.main import create_app
from server.settings import ServerSettings
from server.storage import FactSourceRecord, MemoryRepository, Repository
from tests.fixtures.ingest_fixture import build_workspace, openapi_config
from tests.fixtures.server_fixture import Harness, build_harness, build_registry

pytestmark = pytest.mark.api

PROJECT_NAME = "演示项目"


@pytest.fixture
def harness(tmp_path: Path, repo: Repository) -> Harness:
    """真铺一个工作区：来源的价值全在"外面给过来的东西读不读得出来"。"""
    return build_harness(build_workspace(tmp_path), repository=repo)


@pytest.fixture
def project_id(harness: Harness) -> str:
    return harness.services.create_project(name=PROJECT_NAME).project_id


def expect_error(code: str, fn: Callable[[], object]) -> EngineError:
    with pytest.raises(EngineError) as caught:
        fn()
    assert caught.value.code == code, caught.value.message
    return caught.value


def add(
    harness: Harness, project_id: str, config: SourceConfig, **kwargs: object
) -> tuple[FactSourceRecord, bool]:
    return harness.services.add_source(project_id, config=config, **kwargs)  # type: ignore[arg-type]


# ================================================================== 一、列表
class TestList:
    def test_an_empty_project_lists_nothing(self, harness: Harness, project_id: str) -> None:
        assert harness.services.list_sources(project_id) == []

    def test_unknown_project_is_not_found(self, harness: Harness) -> None:
        expect_error(ErrorCode.NOT_FOUND, lambda: harness.services.list_sources("p_nope"))

    def test_known_types_are_data_not_an_enum(self, harness: Harness) -> None:
        """可选接入器由**注册表**给出（可由 entry point 扩充），不是一份写死的枚举。

        注册表里的内建五个与 ``SourceType`` 那个契约闭集**不是**同一份东西——后者含
        ``markdown`` / ``proto`` / ``manual`` 而前者没有，两侧的差异由 ``add_source``
        在写库之前报出来（见 :class:`TestAdd`）。
        """
        names = harness.services.source_types()
        assert names == ["assets", "ast", "openapi", "orm", "xlsx"]
        assert "markdown" not in names


# ================================================================== 二、新增
class TestAdd:
    def test_adds_a_source(self, harness: Harness, project_id: str) -> None:
        record, created = add(harness, project_id, openapi_config(), author="森")
        assert created is True
        assert record.source_type == "openapi"  # type: ignore[attr-defined]
        assert record.created_by == "森"  # type: ignore[attr-defined]
        # 从没探过：``None`` 而不是 ``False``。
        assert record.last_probe_ok is None  # type: ignore[attr-defined]
        assert record.last_probed_at is None  # type: ignore[attr-defined]

    def test_same_config_is_idempotent(self, harness: Harness, project_id: str) -> None:
        """幂等不是"成功"：界面要显示"已存在，未改动"，两者的下一步动作不一样。"""
        first, created = add(harness, project_id, openapi_config())
        again, created_again = add(harness, project_id, openapi_config())
        assert created is True and created_again is False
        assert again.created_at == first.created_at  # type: ignore[attr-defined]
        assert len(harness.services.list_sources(project_id)) == 1

    def test_a_different_config_for_the_same_id_is_rejected(
        self, harness: Harness, project_id: str
    ) -> None:
        """**不静默覆盖**：同 id 换配置要拒绝。

        理由是"覆盖"必须是一次有意识的选择——同 id 的来源可能已被既有事实与追溯边
        引用，而一次笔误的后果不该是"那份来源悄悄换成了别的东西"。
        """
        add(harness, project_id, openapi_config())
        error = expect_error(
            ErrorCode.SOURCE_CONFIG_INVALID,
            lambda: add(harness, project_id, _ast_same_id()),
        )
        assert error.where["existing_source_type"] == "openapi"
        assert error.where["incoming_source_type"] == "ast"
        assert error.remediation

    def test_a_type_without_a_connector_is_rejected_before_being_stored(
        self, harness: Harness, project_id: str
    ) -> None:
        """``SourceType`` 闭集含 ``markdown``，而注册表只装了内建的五个。

        两侧的不一致必须**在写库之前**被说出来——否则它会一路走到构建期才现形，
        表现为"来源读不出东西"，而配置看上去完全合法。
        """
        error = expect_error(
            ErrorCode.SOURCE_CONFIG_INVALID,
            lambda: add(
                harness,
                project_id,
                SourceConfig(source_id="docs", source_type="markdown", root="specs"),
            ),
        )
        assert "markdown" in str(error.where["problems"])
        assert error.where["known_types"] == ["assets", "ast", "openapi", "orm", "xlsx"]
        assert harness.services.list_sources(project_id) == []

    def test_a_root_that_escapes_the_storage_root_is_rejected(
        self, harness: Harness, project_id: str
    ) -> None:
        expect_error(
            ErrorCode.SOURCE_CONFIG_INVALID,
            lambda: add(
                harness,
                project_id,
                SourceConfig(source_id="outside", source_type="openapi", root="../../etc"),
            ),
        )

    def test_a_missing_source_id_is_rejected(self, harness: Harness, project_id: str) -> None:
        expect_error(
            ErrorCode.SOURCE_CONFIG_INVALID,
            lambda: add(harness, project_id, SourceConfig(source_id="  ", source_type="openapi")),
        )

    def test_source_id_is_validated_before_being_a_path(self, harness: Harness) -> None:
        """来源标识是**引用键**（事实与冲突记录都用它），不是路径——但仍先校验一遍，
        因为一旦有人在别处拿它拼目录，那时才发现就晚了（硬约束 14 的一贯口径）。"""
        project = harness.services.create_project(name=PROJECT_NAME).project_id
        for bad in ("a:b", "a/b", "a\\b"):
            broken = SourceConfig(source_id=bad, source_type="openapi", root="specs")
            expect_error(ErrorCode.CONFIG_INVALID, lambda b=broken: add(harness, project, b))

    def test_unknown_project_is_not_found(self, harness: Harness) -> None:
        expect_error(
            ErrorCode.NOT_FOUND,
            lambda: add(harness, "p_nope", openapi_config()),
        )


def _ast_same_id() -> SourceConfig:
    """与 ``api_spec`` 同 id、不同配置（换接入器与根）——用于撞"已被占用"。"""
    return SourceConfig(source_id="api_spec", source_type="ast", root="src")


# ================================================================== 三、删除
class TestRemove:
    def test_removes_the_declaration(self, harness: Harness, project_id: str) -> None:
        add(harness, project_id, openapi_config())
        assert harness.services.remove_source(project_id, "api_spec") is True
        assert harness.services.list_sources(project_id) == []

    def test_a_missing_source_is_not_found(self, harness: Harness, project_id: str) -> None:
        expect_error(
            ErrorCode.NOT_FOUND, lambda: harness.services.remove_source(project_id, "nope")
        )

    def test_removal_does_not_touch_history(self, harness: Harness, project_id: str) -> None:
        """删掉的只是"以后不再接这一份"，不是"以前的文档作废"。

        事实包与追溯边里记的是 ``source_id``（一个字符串），不是这条记录——故来源
        可以删，历史产出照旧可追溯。这里钉的是"删完还能用同一个 id 重新声明"。
        """
        add(harness, project_id, openapi_config())
        harness.services.remove_source(project_id, "api_spec")
        record, created = add(harness, project_id, openapi_config())
        assert created is True
        assert record.last_probe_ok is None  # 新的一条记录，不继承旧留痕


# ============================================================== 四、试接入
class TestProbe:
    def test_probes_a_real_source_and_leaves_a_trace(
        self, harness: Harness, project_id: str
    ) -> None:
        add(harness, project_id, openapi_config())
        result = harness.services.probe(project_id, "api_spec")
        assert result.ok is True
        assert result.item_count is not None and result.item_count > 0
        assert result.probed_at == harness.clock.moment

        stored = harness.services.get_source(project_id, "api_spec")
        assert stored.last_probe_ok is True
        assert stored.item_count == result.item_count
        assert stored.last_probed_at == harness.clock.moment
        assert stored.last_probe_detail == result.detail

    def test_an_unreachable_source_returns_ok_false_instead_of_raising(
        self, harness: Harness, project_id: str
    ) -> None:
        """**探针永不抛**：失败要显示成"这条来源现在连不通 + 为什么"，而不是 500。"""
        add(
            harness,
            project_id,
            SourceConfig(source_id="gone", source_type="openapi", root="nowhere"),
        )
        result = harness.services.probe(project_id, "gone")
        assert result.ok is False
        assert result.detail
        # 留痕要记下"探了、没通"——这正是 ``None`` 与 ``False`` 必须分开的用处。
        stored = harness.services.get_source(project_id, "gone")
        assert stored.last_probe_ok is False
        assert stored.last_probed_at == harness.clock.moment

    def test_a_later_successful_probe_overwrites_the_failure_trace(
        self, harness: Harness, project_id: str
    ) -> None:
        """留痕只说"上次是什么样"，故后一次的结论覆盖前一次的。"""
        add(harness, project_id, SourceConfig(source_id="tmp", source_type="openapi", root="x"))
        assert harness.services.probe(project_id, "tmp").ok is False
        harness.services.remove_source(project_id, "tmp")
        add(harness, project_id, openapi_config(source_id="tmp"))
        assert harness.services.probe(project_id, "tmp").ok is True
        assert harness.services.get_source(project_id, "tmp").last_probe_ok is True

    def test_unknown_source_is_not_found(self, harness: Harness, project_id: str) -> None:
        expect_error(ErrorCode.NOT_FOUND, lambda: harness.services.probe(project_id, "nope"))


# ========================================================== 五、条目浏览
class TestItems:
    def test_reads_facts_and_their_kinds(self, harness: Harness, project_id: str) -> None:
        add(harness, project_id, openapi_config())
        page = harness.services.source_items(project_id, "api_spec")
        assert page.total == len(page.facts) == 3
        assert page.kinds() == {"endpoint": 3}
        assert page.digest
        assert page.truncated is False

    def test_truncates_and_says_so(self, harness: Harness, project_id: str) -> None:
        """截断必须能被看出来，否则"这份来源只有 1 条"会被读成事实。"""
        add(harness, project_id, openapi_config())
        page = harness.services.source_items(project_id, "api_spec", limit=1)
        assert len(page.facts) == 1
        assert page.total == 3
        assert page.truncated is True

    def test_a_malformed_source_raises_with_a_file_location(
        self, harness: Harness, project_id: str
    ) -> None:
        """与 ``probe`` 的关键差别：读不出来就是一次失败请求。

        空清单会被读成"这份来源本来就没有对象"——那是对一个从未成立的前提的陈述。
        接入器自己诊断出的问题（这里是"这文件不是 JSON"）**原样上抛**，故带文件与行列
        定位；``E_SOURCE_LOAD_FAILED`` 留给"接入器抛了意料之外的异常"那一种。
        """
        broken = harness.root / "broken"
        broken.mkdir(exist_ok=True)
        (broken / "openapi.json").write_text("这不是 JSON", encoding="utf-8")
        add(
            harness,
            project_id,
            SourceConfig(source_id="bad", source_type="openapi", root="broken/openapi.json"),
        )
        error = expect_error(
            ErrorCode.CONFIG_INVALID,
            lambda: harness.services.source_items(project_id, "bad"),
        )
        assert "openapi.json" in error.message, "定位要指到文件，否则使用者不知道该改哪一份"

    def test_the_source_guard_runs_before_reading(self, harness: Harness, project_id: str) -> None:
        """护栏（``SourceGuard``）在**读文件之前**把关——故"根不存在"报的是接入层的
        类型码而不是"加载失败"。

        这条同时钉住"探针与执行共用同一个解析点"：两者都走护栏，于是
        "试接入说通了、真接入却说找不到"不会成为常态。
        """
        add(
            harness,
            project_id,
            SourceConfig(source_id="gone", source_type="openapi", root="nowhere"),
        )
        expect_error(
            ErrorCode.UPLOAD_TYPE,
            lambda: harness.services.source_items(project_id, "gone"),
        )


# ============================================================== 六、HTTP 层
class TestHttp:
    @pytest.fixture
    def root(self, tmp_path: Path) -> Path:
        return build_workspace(tmp_path / "http")

    @pytest.fixture
    def client(self, root: Path) -> TestClient:
        app = create_app(
            settings=ServerSettings(_env_file=None, storage_root=root),
            repository=MemoryRepository(),
            stages=build_registry(),
            root=root,
            start_worker=False,
        )
        return TestClient(app)

    @pytest.fixture
    def project_id(self, client: TestClient) -> str:
        created = client.post("/api/v1/projects", json={"name": PROJECT_NAME})
        assert created.status_code == 201, created.text
        return created.json()["project_id"]

    def test_add_list_probe_items_and_remove(self, client: TestClient, project_id: str) -> None:
        created = client.post(
            f"/api/v1/projects/{project_id}/sources",
            json={
                "config": openapi_config().model_dump(mode="json"),
                "author": "森",
            },
        )
        assert created.status_code == 201, created.text
        assert created.json()["created"] is True
        assert created.json()["source"]["source_id"] == "api_spec"
        assert created.json()["source"]["last_probe_ok"] is None, "从没探过必须是 None"

        # 幂等：同 id 同配置 → 200 形状照旧但 ``created=False``（界面说"已存在，未改动"）。
        again = client.post(
            f"/api/v1/projects/{project_id}/sources",
            json={"config": openapi_config().model_dump(mode="json")},
        )
        assert again.status_code == 201
        assert again.json()["created"] is False

        listing = client.get(f"/api/v1/projects/{project_id}/sources")
        assert listing.status_code == 200
        body = listing.json()
        assert body["count"] == 1
        assert body["known_types"] == ["assets", "ast", "openapi", "orm", "xlsx"]

        probed = client.post(f"/api/v1/projects/{project_id}/sources/api_spec/probe")
        assert probed.status_code == 200, probed.text
        assert probed.json()["ok"] is True
        assert probed.json()["item_count"] == 3

        items = client.get(f"/api/v1/projects/{project_id}/sources/api_spec/items")
        assert items.status_code == 200
        assert items.json()["total"] == 3
        assert items.json()["kinds"] == {"endpoint": 3}
        assert items.json()["truncated"] is False

        truncated = client.get(
            f"/api/v1/projects/{project_id}/sources/api_spec/items", params={"limit": 1}
        )
        assert truncated.json()["truncated"] is True
        assert truncated.json()["total"] == 3

        removed = client.delete(f"/api/v1/projects/{project_id}/sources/api_spec")
        assert removed.status_code == 200
        assert removed.json() == {"removed": "api_spec"}
        assert client.get(f"/api/v1/projects/{project_id}/sources").json()["count"] == 0

    def test_probe_of_an_unreachable_source_is_a_200_with_ok_false(
        self, client: TestClient, project_id: str
    ) -> None:
        """``ok=False`` 是**正常回答**，不是错误——故状态码是 200 而不是 4xx/5xx。"""
        client.post(
            f"/api/v1/projects/{project_id}/sources",
            json={
                "config": SourceConfig(
                    source_id="gone", source_type="openapi", root="nowhere"
                ).model_dump(mode="json")
            },
        )
        response = client.post(f"/api/v1/projects/{project_id}/sources/gone/probe")
        assert response.status_code == 200, response.text
        assert response.json()["ok"] is False
        assert response.json()["detail"]

    def test_a_type_without_a_connector_returns_422(
        self, client: TestClient, project_id: str
    ) -> None:
        response = client.post(
            f"/api/v1/projects/{project_id}/sources",
            json={
                "config": SourceConfig(source_id="docs", source_type="markdown").model_dump(
                    mode="json"
                )
            },
        )
        assert response.status_code == 422, response.text
        assert response.json()["code"] == ErrorCode.SOURCE_CONFIG_INVALID

    def test_extra_request_fields_are_rejected(self, client: TestClient, project_id: str) -> None:
        """请求体未知字段一律拒（``schemas_assets`` 从 ``schemas`` 借来的那份 ``_Body``）。

        多传了字段却没人报错，是版本不匹配最难查的那种症状。
        """
        response = client.post(
            f"/api/v1/projects/{project_id}/sources",
            json={
                "config": openapi_config().model_dump(mode="json"),
                "owner": "森",
            },
        )
        assert response.status_code == 422, response.text

    def test_missing_source_returns_404(self, client: TestClient, project_id: str) -> None:
        response = client.get(f"/api/v1/projects/{project_id}/sources/nope/items")
        assert response.status_code == 404
        assert response.json()["code"] == ErrorCode.NOT_FOUND

    def test_items_limit_out_of_range_is_a_422(self, client: TestClient, project_id: str) -> None:
        client.post(
            f"/api/v1/projects/{project_id}/sources",
            json={"config": openapi_config().model_dump(mode="json")},
        )
        response = client.get(
            f"/api/v1/projects/{project_id}/sources/api_spec/items", params={"limit": 0}
        )
        assert response.status_code == 422, response.text

    def test_items_of_a_malformed_source_return_a_422(
        self, client: TestClient, project_id: str, root: Path
    ) -> None:
        """读不出来要报错，而不是回一份空清单——空清单会被读成"本来就没有对象"。"""
        broken = root / "broken"
        broken.mkdir(exist_ok=True)
        (broken / "openapi.json").write_text("这不是 JSON", encoding="utf-8")
        client.post(
            f"/api/v1/projects/{project_id}/sources",
            json={
                "config": SourceConfig(
                    source_id="bad", source_type="openapi", root="broken/openapi.json"
                ).model_dump(mode="json")
            },
        )
        response = client.get(f"/api/v1/projects/{project_id}/sources/bad/items")
        assert response.status_code == 422, response.text
        assert response.json()["code"] == ErrorCode.CONFIG_INVALID

    def test_openapi_publishes_the_sources_group(self, client: TestClient) -> None:
        schema = client.get("/openapi.json").json()
        paths = schema["paths"]
        assert "/api/v1/projects/{project_id}/sources" in paths
        assert "/api/v1/projects/{project_id}/sources/{source_id}/probe" in paths
        assert "/api/v1/projects/{project_id}/sources/{source_id}/items" in paths
        assert "sources" in {tag["name"] for tag in schema["tags"]}
