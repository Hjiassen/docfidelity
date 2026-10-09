"""项目总览 / 漂移 / 产物聚合 / 反向引用（P02 · P14 · P11 · P04 —— TASK-P0-15 第 6 轮）。

这四条端点为什么在同一个文件里
------------------------------
它们的共同点是「**结论由服务端给，界面照抄**」（NFR-22），而这一步是本轮唯一新增的
服务端能力：

* P02 总览：``has_config`` / ``steps`` / ``pending_total`` / ``last_job`` 各是一句结论，
  界面若自己拉四份列表去数，每加一张卡片就多一处口径；
* P14 漂移：``fact_level_available`` 为假时**必须**把 ``fact_level_note`` 显示出来——
  把"查不了"渲染成"没有漂移"，使用者会以为文档是最新的（硬规则 11）；
* P11 产物：``count`` / ``total_bytes`` / ``report_verdict`` 都不许界面再算一遍；
* P04 反向引用：``active_count`` 与 ``deprecated`` 决定"还能不能废弃这一版"。

三条最容易写错的断言，各有一条用例钉住
--------------------------------------
1. **"没有配置"与"配置为空"不是一回事**。前者要引导去初始化，后者只是还没登记文档；
   而总览端点一度在没配置时直接抛 ``E_CONFIG_INVALID``——那让最需要这条引导的新项目
   看到一张错误页（本文件里 ``test_a_project_without_a_config_still_has_an_overview``
   是那条缺陷的回归）。
2. **"引用到了模板包"不等于"模板包已入库"**。只看后者会让一份文档都没配的项目显示
   "模板已就绪"。
3. **"没有报告"不是"报告通过"**。``report_verdict is None`` 才是"未经门禁、仅作草稿"，
   按作业状态去猜会把草稿显示成成品。
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from docfidelity.contracts.config import ProjectConfig
from docfidelity.contracts.errors import EngineError, ErrorCode
from docfidelity.contracts.profile import Bindings
from server.adjudication import PendingSubject
from server.main import create_app
from server.overview import OVERVIEW_PENDING_LIMIT, build_overview
from server.services import DEMO_PROFILE_ID, DEMO_TEMPLATE_REF
from server.settings import ServerSettings
from server.storage import MemoryRepository, RegisteredTemplate
from tests.fixtures.domain_fixture import api_profile
from tests.fixtures.server_fixture import (
    Harness,
    build_harness,
    build_registry,
    document_entry,
    finding,
    project_config,
    seed_demo_assets,
    seed_domain_models,
    seed_domain_models_via_http,
)

pytestmark = pytest.mark.api


@pytest.fixture
def harness(tmp_path: Path) -> Harness:
    return build_harness(tmp_path)


def expect_error(code: str, fn: object) -> EngineError:
    with pytest.raises(EngineError) as caught:
        fn()  # type: ignore[operator]
    assert caught.value.code == code, caught.value.message
    return caught.value


# =========================================================== 一、P02 项目总览
class TestOverview:
    def test_metrics_and_steps_come_from_the_server(self, harness: Harness) -> None:
        """五个指标与三步引导都由服务端算——本用例只是照抄一遍，不做二次推导。"""
        project_id = harness.new_project()
        overview = harness.services.project_overview(project_id)

        assert overview.has_config is True
        assert overview.metrics.documents == 2
        # 两份文档引用的是**同一个** demo-package@1，故是 1 而不是 2
        assert overview.metrics.templates == 1
        assert overview.metrics.sources == 0
        assert overview.metrics.artifacts == 0
        assert overview.metrics.pending_decisions == 0

        steps = {step.key: step for step in overview.steps}
        assert steps["template"].done is True, "演示资产已登记，这一步该打勾"
        assert steps["template"].hint is None, "打过勾的那一步不给'接下来做什么'"
        assert steps["source"].done is False
        assert steps["source"].hint, "没打勾的一步必须给引导语，否则界面只能显示一个空勾选框"
        assert steps["build"].done is False

        assert overview.precheck is not None
        assert overview.precheck.ok is True
        assert overview.last_job is None, "还没发起过构建"

    def test_a_project_without_a_config_still_has_an_overview(self, harness: Harness) -> None:
        """**回归**：刚建好、还没配过文档的项目必须能拿到总览。

        这条端点一度把 ``precheck`` 无条件带上来，而那份判据在没配置时报
        ``E_CONFIG_INVALID``——于是"下一步该做什么"的入口恰好在新项目上变成一张错误页。
        正解是问一次"配了吗"：没配就交 ``None``（``precheck`` 本来就可空），
        让界面渲染"这一步没有可检查的对象"。
        """
        project = harness.services.create_project(name="还没配")
        overview = harness.services.project_overview(project.project_id)

        assert overview.has_config is False
        assert overview.precheck is None, "没配置时不是'检查没跑'，是'没有可检查的对象'"
        assert overview.metrics.documents == 0
        assert overview.last_job is None
        assert [step.done for step in overview.steps] == [False, False, False]
        assert all(step.hint for step in overview.steps), "三步都要给下一步动作"

    def test_the_template_step_needs_a_reference_and_a_registered_package(
        self, harness: Harness
    ) -> None:
        """只看"库里有没有模板"会让一份文档都没配的项目显示"模板已就绪"。

        判据是**两条**：配置里引用到了模板包，且那些引用都已入库。少任何一条，那一步的
        引导语都会指向一个已经打勾的项，使用者于是去别处找问题。
        """
        project_id = harness.services.create_project(name="引用未入库").project_id
        harness.services.demo_init(project_id)  # 有配置与引用，但库里什么都没有

        steps = {s.key: s for s in harness.services.project_overview(project_id).steps}
        assert steps["template"].done is False
        assert steps["template"].hint

        seed_demo_assets(harness)  # 入库之后这一步才该打勾
        steps = {s.key: s for s in harness.services.project_overview(project_id).steps}
        assert steps["template"].done is True

    def test_last_job_prefers_the_orchestration_job(self, harness: Harness) -> None:
        """需求 P02 区域 4 要的就是"作业号（编排）"，且带 ``n/N`` 进度。"""
        project_id = harness.new_project()
        orchestration_id = harness.build(project_id)

        overview = harness.services.project_overview(project_id)
        assert overview.last_job is not None
        assert overview.last_job.job.job_id == orchestration_id
        assert overview.last_job.job.is_orchestration is True
        assert overview.last_job.progress is not None
        assert overview.last_job.progress.total == 2
        assert {s.key: s.done for s in overview.steps}["build"] is True, "构建成功之后第三步该打勾"

    def test_pending_is_ranked_and_capped_on_the_server(self, harness: Harness) -> None:
        """待办按严重级别排序、并截到 ``OVERVIEW_PENDING_LIMIT``。

        排序放服务端而不是界面按字面量排：那是"什么叫更严重"这条知识，两处各写一份之后
        最该先处理的一条会被排到中间。``None``（决策类待办没有级别）排在**已分级之后**——
        它不是"最严重"，它只是没有级别。
        """
        project_id = harness.new_project()
        moment = harness.clock()

        def pending(index: int, severity: str | None) -> PendingSubject:
            return PendingSubject(
                subject_id=f"s{index:02d}",
                kind="finding",
                job_id="j_x",
                project_id=project_id,
                title=f"待办 {index}",
                severity=severity,
                created_at=moment,
            )

        items = [pending(i, None) for i in range(3)]
        items += [pending(10 + i, "minor") for i in range(3)]
        items += [pending(20, "major"), pending(21, "blocker")]

        overview = build_overview(
            repository=harness.repository,
            project_id=project_id,
            pending=items,
            precheck=None,
        )
        assert overview.pending_total == len(items), "总数如实给，界面据此显示'共 N 项'"
        assert len(overview.pending) == OVERVIEW_PENDING_LIMIT
        assert [item.severity for item in overview.pending] == [
            "blocker",
            "major",
            "minor",
            "minor",
            "minor",
        ]


# =========================================================== 二、P14 漂移
class TestDrift:
    def test_a_document_that_was_never_built_is_drift(self, harness: Harness) -> None:
        """一次都没成功产出过的文档要进清单——否则"这两个文档从没生成过"看起来像"都很新"。"""
        project_id = harness.new_project()
        report = harness.services.drift_report(project_id)

        assert [item.kind for item in report.items] == ["never_built", "never_built"]
        assert [item.doc_key for item in report.items] == ["detail", "overview"]
        assert report.checked_documents == 2
        assert report.fact_level_available is False
        assert report.fact_level_note, "判不了的那一类必须带说明，不能渲染成'没有漂移'"

    def test_a_config_written_after_the_last_success_marks_the_document_stale(
        self, harness: Harness
    ) -> None:
        """判据是**确定性的时间比较**：最后一次成功产出的时刻早于最新配置版本的写入时刻。

        漂移页的价值在于"在构建之前就告诉人要重跑什么"；等构建跑完再说就晚了。
        """
        project_id = harness.new_project()
        harness.build(project_id)
        assert harness.services.drift_report(project_id).items == (), "刚产出过就不该有漂移"

        harness.clock.advance(hours=1)
        harness.services.put_config(
            project_id,
            ProjectConfig(
                project_id=project_id,
                author="森",
                documents=[document_entry(key) for key in ("overview", "detail")],
            ),
            author="森",
        )

        report = harness.services.drift_report(project_id)
        assert [item.kind for item in report.items] == ["changed_not_synced"] * 2
        first = report.items[0]
        assert first.last_job_id, "要给'上一次产出是哪个作业'，否则使用者无从对照"
        assert first.last_generated_at is not None
        assert "v2" in first.detail

    def test_an_expired_lookback_is_not_drift(self, harness: Harness) -> None:
        """配置没动、产出没动 ⇒ 清单为空。空清单是**结论**，不是"还没检查"。"""
        project_id = harness.new_project()
        harness.build(project_id)
        harness.clock.advance(days=30)  # 时间走过去，但没有新配置版本
        assert harness.services.drift_report(project_id).items == ()


# =========================================================== 三、P11 产物聚合
class TestArtifacts:
    @staticmethod
    def _docx_harness(tmp_path: Path, template_bytes: bytes, **qa: object) -> Harness:
        return build_harness(
            tmp_path,
            stages=build_registry(
                render={"artifacts": ("out.docx",), "artifact_payload": template_bytes},
                qa={"emit_report": True, **qa},
            ),
        )

    def test_artifacts_are_grouped_by_orchestration_and_child(
        self, tmp_path: Path, template_bytes: bytes
    ) -> None:
        """三层分组：编排作业 → 子作业 → 文档。子作业的 ``doc_key`` 就是"文档"那一层。"""
        harness = self._docx_harness(tmp_path, template_bytes)
        project_id = harness.new_project()
        orchestration_id = harness.build(project_id)

        payload = harness.services.project_artifacts(project_id)
        assert len(payload.groups) == 1, "一次编排只有一个顶层分组"
        top = payload.groups[0]
        assert top.job_id == orchestration_id
        assert top.kind == "orchestration"
        assert len(top.children) == 2
        assert {child.doc_key for child in top.children} == {"overview", "detail"}
        assert all(child.kind == "document" for child in top.children)
        assert payload.count == 2, "产物条目数由服务端算，界面不做求和"
        assert payload.total_bytes == len(template_bytes) * 2

    def test_a_job_without_a_report_is_marked_as_a_draft(
        self, tmp_path: Path, template_bytes: bytes
    ) -> None:
        """**"没有报告"不是"报告通过"**。需求 P11：该产物未经门禁、仅作草稿。

        判据是 ``report_verdict is None`` 这个**服务端事实**。让界面按"作业状态不是
        succeeded"去猜，就是往浏览器里放了一条业务判据——而且猜错的方式很糟：
        猜成"有报告"会让草稿看起来像成品。
        """
        harness = build_harness(
            tmp_path,
            stages=build_registry(
                render={"artifacts": ("out.docx",), "artifact_payload": template_bytes}
            ),
        )
        project_id = harness.new_project()
        harness.build(project_id)

        groups = harness.services.project_artifacts(project_id).groups
        assert [child.report_verdict for child in groups[0].children] == [None, None]

    def test_a_group_with_a_report_carries_its_verdict(
        self, tmp_path: Path, template_bytes: bytes
    ) -> None:
        """判定全是 minor ⇒ 结论 ``warning``。门禁结论取自 ``list_reports()`` 一次建好的索引。"""
        harness = self._docx_harness(tmp_path, template_bytes, findings=(finding(),))
        project_id = harness.new_project()
        harness.build(project_id)

        children = harness.services.project_artifacts(project_id).groups[0].children
        assert [child.report_verdict for child in children] == ["warning", "warning"]

    def test_comparing_the_same_document_from_two_builds_reports_equal(
        self, tmp_path: Path, template_bytes: bytes
    ) -> None:
        """``equal`` 是**服务端结论**，界面不按三个差异集合是否为空再推一遍（NFR-22）。"""
        harness = self._docx_harness(tmp_path, template_bytes)
        project_id = harness.new_project()
        first = harness.build(project_id)
        second = harness.build(project_id)

        def docx_of(orchestration_id: str) -> str:
            child = harness.services.job_children(orchestration_id)[0]
            return next(
                item.artifact_id
                for item in harness.services.list_artifacts(child.job_id)
                if item.kind == "docx"
            )

        comparison = harness.services.artifact_compare(
            baseline_id=docx_of(first), candidate_id=docx_of(second)
        )
        assert comparison.diff.equal is True
        assert comparison.diff.mode == "structure"
        assert (
            comparison.diff.baseline_ref != comparison.diff.candidate_ref
        ), "两份产物的引用必须各是各的——写同一个的话'比了谁'就说不清"

    def test_comparing_something_that_is_not_a_document_is_rejected(self, tmp_path: Path) -> None:
        """判据在服务端，不靠"界面下拉框只列 docx"。

        `_comparable` 拦不住的坏处很具体：直接打 API 的脚本会拿到一份**空的差异报告**，
        而空差异等于"两份一模一样"——那是一句假话。
        """
        harness = build_harness(
            tmp_path, stages=build_registry(render={"artifacts": ("shot.png",)})
        )
        project_id = harness.new_project()
        orchestration_id = harness.build(project_id)
        child = harness.services.job_children(orchestration_id)[0]
        png = harness.services.list_artifacts(child.job_id)[0].artifact_id

        error = expect_error(
            ErrorCode.SCHEMA_INVALID,
            lambda: harness.services.artifact_compare(baseline_id=png, candidate_id=png),
        )
        assert error.where["kind"] == "png"

    def test_an_empty_artifacts_directory_is_not_an_empty_zip(
        self, tmp_path: Path, template_bytes: bytes
    ) -> None:
        """没有产物时整包导出报 ``E_NOT_FOUND``，而不是回一个空 zip。

        空 zip 在浏览器里表现为"下载成功、打开是空的"，使用者会以为产物丢了。
        """
        harness = build_harness(tmp_path)
        project_id = harness.new_project()
        harness.build(project_id)
        orchestration_id = harness.services.list_jobs(project_id=project_id)[0].job_id

        expect_error(
            ErrorCode.NOT_FOUND,
            lambda: harness.services.artifact_package(orchestration_id),
        )


# ======================================================= 四、P04 反向引用
class TestTemplateReferences:
    @staticmethod
    def _profile(template_ref: str = DEMO_TEMPLATE_REF) -> object:
        """一份绑定了 ``template_ref`` 的演示档案（身份改成演示配置里写的值）。"""
        return api_profile().model_copy(
            update={
                "profile_id": DEMO_PROFILE_ID,
                "doc_type": DEMO_PROFILE_ID,
                "bindings": Bindings(template_packages=[template_ref]),
            }
        )

    def test_the_profile_that_declares_the_package_is_found(self, harness: Harness) -> None:
        """反向引用：模板版本 → 引用它的档案版本。

        判定与登记时校验 ``bindings.template_packages`` 的**同一份实现**（"引用的书写
        形式长什么样"只能有一个答案）；本用例把这条链路走通一遍。

        ``on_conflict="overwrite"``：``seed_demo_assets`` 已经用**夹具捷径**塞了一条同
        ``id@version`` 的登记（内容不同），这里要的是**真**登记出来的那一份。
        """
        seed_demo_assets(harness)
        # 这份档案的 for_each 指着 endpoint：领域类型不先入库，登记会被 FR-M2-23 拒掉。
        # 注意还要**显式**把库读出来传进去——``register_profile`` 不认识仓储，
        # 缺省的空序列表示"库是空的"，不是"这次没能力查"。
        seed_domain_models(harness)
        harness.services.register_profile(
            document=self._profile(),  # type: ignore[arg-type]
            known_template_refs=[DEMO_TEMPLATE_REF],
            domain_models=harness.services.domain_declarations(),
            author="森",
            on_conflict="overwrite",
        )

        found = harness.services.template_references("demo-package", "1")
        assert [(item.profile_id, item.version) for item in found] == [(DEMO_PROFILE_ID, "1.0")]
        assert found[0].declared_in, "要标出引用出现在档案的哪个位置，不能只说'有引用'"

    def test_an_unknown_template_id_is_not_found_rather_than_empty(self, harness: Harness) -> None:
        """编号写错（404）与"没人引用"（空清单）要分开：两者在界面上长得像，
        但下一步动作完全不同（改标识 vs 去改档案的绑定）。"""
        expect_error(
            ErrorCode.NOT_FOUND,
            lambda: harness.services.template_references("没有这个包"),
        )

    def test_a_deprecated_profile_still_counts_as_a_reference(self, harness: Harness) -> None:
        """**废弃的档案仍在引用这个包**，漏掉它们会让"这一版还有人用吗"的判据凭空放宽。

        于是"能不能废弃"要看的不是总数而是 ``active_count``——它由服务端算，界面照抄。
        """
        seed_demo_assets(harness)
        seed_domain_models(harness)
        harness.services.register_profile(
            document=self._profile(),  # type: ignore[arg-type]
            known_template_refs=[DEMO_TEMPLATE_REF],
            domain_models=harness.services.domain_declarations(),
            on_conflict="overwrite",
        )
        harness.services.deprecate_profile(DEMO_PROFILE_ID, "1.0", reason="被 2.0 取代")

        found = harness.services.template_references("demo-package", "1")
        assert len(found) == 1
        assert found[0].deprecated is True


# ================================================================ 五、HTTP 层
class TestHttp:
    """路由与状态码（``schemas_project`` 的三个响应模型各有端点）。"""

    @pytest.fixture
    def http(self, tmp_path: Path) -> tuple[TestClient, MemoryRepository]:
        """一并交出仓储：档案登记要"库里已有这个模板包"才放行，而那条登记得先塞进去。

        塞的是**记录**而不是走导入流水线（同 ``seed_demo_assets`` 的理由）：本用例要验的
        是端点形状，不是"模板怎么进库"。
        """
        root = tmp_path / "http-overview"
        repository = MemoryRepository()
        app = create_app(
            settings=ServerSettings(_env_file=None, storage_root=root),
            repository=repository,
            stages=build_registry(),
            root=root,
            start_worker=False,
        )
        return TestClient(app), repository

    def test_overview_of_a_fresh_project_is_200_not_an_error(
        self, http: tuple[TestClient, MemoryRepository]
    ) -> None:
        """**回归**（与上面那条同名缺陷）：新项目调总览必须 200。

        接口层把这条缺陷换了个长相——不是"没有引导语"，而是整页加载失败。
        """
        client, _ = http
        created = client.post("/api/v1/projects", json={"name": "新项目"})
        assert created.status_code == 201, created.text
        project_id = created.json()["project_id"]

        got = client.get(f"/api/v1/projects/{project_id}/overview")
        assert got.status_code == 200, got.text
        body = got.json()
        assert body["has_config"] is False
        assert body["precheck"] is None
        assert body["metrics"]["documents"] == 0
        assert [step["done"] for step in body["steps"]] == [False, False, False]

    def test_drift_of_a_fresh_project_is_an_empty_checked_list(
        self, http: tuple[TestClient, MemoryRepository]
    ) -> None:
        client, _ = http
        created = client.post("/api/v1/projects", json={"name": "新项目"})
        project_id = created.json()["project_id"]

        got = client.get(f"/api/v1/projects/{project_id}/drift")
        assert got.status_code == 200, got.text
        body = got.json()
        assert body["items"] == []
        assert body["checked_documents"] == 0
        assert body["fact_level_available"] is False
        assert body["fact_level_note"], "判不了就要说清楚，别让界面渲染成'没有漂移'"

    def test_template_references_endpoint_reports_counts(
        self, http: tuple[TestClient, MemoryRepository]
    ) -> None:
        """``GET /templates/{tid}/references`` 回 ``count`` / ``active_count``，
        且**不外泄落点**（``profile_dir`` 这类登记元数据不进响应）。"""
        client, repository = http
        repository.add_template(
            RegisteredTemplate(
                template_id="TPL-API",
                version="1.0",
                package_hash="seed",
                source_hash="seed",
                package_dir="seed",  # 本用例只验端点形状，不读包本体
                registered_at=datetime(2026, 9, 20, 9, 0, tzinfo=timezone.utc),
            )
        )
        # 档案的 for_each 在***登记期***就按 FR-M2-23 校验引用的领域类型，故这层
        # 词汇表必须先经过端点登记进来——空库的话下面那条 201 会变成 422。
        seed_domain_models_via_http(client)
        created = client.post(
            "/api/v1/profiles",
            json={
                "profile": api_profile()
                .model_copy(update={"bindings": Bindings(template_packages=["TPL-API@1.0"])})
                .model_dump(mode="json"),
                "version": "1.0",
                "author": "森",
            },
        )
        assert created.status_code == 201, created.text

        got = client.get("/api/v1/templates/TPL-API/references", params={"version": "1.0"})
        assert got.status_code == 200, got.text
        body = got.json()
        assert body["count"] == 1
        assert body["active_count"] == 1
        assert body["items"][0]["profile_id"] == "api-reference"
        assert body["items"][0]["version"] == "1.0"
        assert "profile_dir" not in body["items"][0]

    def test_a_missing_config_version_is_404(
        self, http: tuple[TestClient, MemoryRepository]
    ) -> None:
        """读历史版本：请求的是一个**具体对象**，"它不在库里"与"这个项目还没配"
        是两件要给出不同动作的事（``E_NOT_FOUND`` 而不是 ``E_CONFIG_INVALID``）。"""
        client, _ = http
        created = client.post("/api/v1/projects", json={"name": "没配"})
        project_id = created.json()["project_id"]

        got = client.get(f"/api/v1/projects/{project_id}/config/versions/7")
        assert got.status_code == 404, got.text
        assert got.json()["code"] == "E_NOT_FOUND"

    def test_an_older_config_version_is_readable_and_equals_what_was_written(
        self, http: tuple[TestClient, MemoryRepository]
    ) -> None:
        """P07 的「变更留痕」：拿到的形状与"读最新"完全一致（就是 ``ProjectConfig``）。

        于是界面以它为底再保存就是**回滚**，不需要另一条专用通道。
        """
        client, _ = http
        created = client.post("/api/v1/projects", json={"name": "两版配置"})
        project_id = created.json()["project_id"]
        initial = project_config(project_id).model_dump(mode="json")
        assert client.put(f"/api/v1/projects/{project_id}/config", json=initial).status_code == 200

        first = client.get(f"/api/v1/projects/{project_id}/config/versions/1")
        assert first.status_code == 200, first.text
        assert [doc["doc_key"] for doc in first.json()["documents"]] == ["overview", "detail"]

        # 第二版只留一份文档（PUT 的 body 就是 ``ProjectConfig`` 本身，作者走查询参数）
        body = project_config(project_id, doc_keys=("overview",)).model_dump(mode="json")
        body["version"] = 2
        second = client.put(
            f"/api/v1/projects/{project_id}/config", params={"author": "森"}, json=body
        )
        assert second.status_code == 200, second.text
        assert second.json()["version"] == 2

        versions = client.get(f"/api/v1/projects/{project_id}/config/versions")
        assert [item["version"] for item in versions.json()] == [1, 2]
        # v1 的内容**没有**被 v2 覆盖——这正是"留痕"要回答的那个问题
        v1 = client.get(f"/api/v1/projects/{project_id}/config/versions/1")
        v2 = client.get(f"/api/v1/projects/{project_id}/config/versions/2")
        assert len(v1.json()["documents"]) == 2
        assert len(v2.json()["documents"]) == 1
