"""用例层（DD-13.1 的"HTTP 层只做请求校验，业务动作都在这里"）。

本文件按"端点的可操作动作"组织，而不是按方法名：每条用例最后都断言一个**错误码**
或一个**状态**——因为界面正是按这两样渲染的。若某条路径只断言"抛异常了"，
那么把 ``E_NOT_FOUND`` 写成 ``E_CONFIG_INVALID`` 也不会被发现，而这两条给使用者的
提示完全不同（"检查项目标识" vs "去初始化配置"）。

两条刻意分辨的相邻码，各有一组用例：
* ``E_NOT_FOUND``（项目不存在）vs ``E_CONFIG_INVALID``（项目在、配置不在）；
* ``E_STATE_INVALID``（这个作业自己不能做这个动作）vs ``E_PRECHECK_FAILED``
  （服务端还没准备好，跟作业无关）。
"""

from __future__ import annotations

import dataclasses
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from docfidelity.contracts.config import ProjectConfig
from docfidelity.contracts.errors import EngineError, ErrorCode
from docfidelity.pipeline import StageRegistry
from server.settings import DETERMINISTIC_REASON, FORCED_DETERMINISTIC_REASON
from tests.fixtures.server_fixture import (
    DEMO_DOC_KEYS,
    ArtifactStage,
    Harness,
    MutableClock,
    build_harness,
    build_registry,
    document_entry,
)

pytestmark = pytest.mark.api

DOCX = b"PK\x03\x04payload"


@pytest.fixture
def harness(tmp_path: Path) -> Harness:
    return build_harness(tmp_path)


@pytest.fixture
def wall_clocked(tmp_path: Path) -> Harness:
    """时钟锚在**真实现在**的 Harness（只给涉及**工作区**保留的用例用）。

    工作区的 ``created_at`` 取自目录 **mtime**——``jobs/<id>/`` 是个目录而不是一条记录，
    没有别的时间可取（``server/maintenance.retained_objects``）。而 mtime 是墙钟，
    故"过不过期"由**真实时刻**决定。拿固定注入时钟去推它的用例会退化成
    "每天 17:00 之前通过"：``FIXED_NOW + 31 天`` 必须晚于 ``真实 mtime + 30 天``，
    而 FIXED_NOW 固定在 2026-09-22T09:00Z、真实 mtime 却是今天——两者只在
    真实 UTC 时刻早于 09:00 时满足。锚到真实现在之后，"往前推 31 天"才总是真的过期。
    """
    return build_harness(tmp_path, clock=MutableClock(moment=datetime.now(tz=timezone.utc)))


def expect_error(code: str, fn: object) -> EngineError:
    with pytest.raises(EngineError) as caught:
        fn()  # type: ignore[operator]
    assert caught.value.code == code, caught.value.message
    return caught.value


# ------------------------------------------------------------------ 项目
def test_project_lifecycle(harness: Harness) -> None:
    created = harness.services.create_project(name="甲")
    assert harness.services.get_project(created.project_id) == created
    renamed = harness.services.rename_project(created.project_id, "甲改")
    assert renamed.name == "甲改"
    assert renamed.created_at == created.created_at, "改名不该重置创建时间"
    assert renamed.updated_at >= created.updated_at
    archived = harness.services.set_archived(created.project_id, archived=True)
    assert archived.archived is True
    assert harness.services.list_projects() == []


def test_duplicate_project_name_is_reported(harness: Harness) -> None:
    harness.services.create_project(name="同名")
    expect_error(ErrorCode.PROJECT_NAME_TAKEN, lambda: harness.services.create_project(name="同名"))


# ------------------------------------------------------------------ 配置
def test_missing_project_and_missing_config_are_different_codes(harness: Harness) -> None:
    """ "项目写错了"和"项目还没配"的可操作动作完全不同（改标识 vs 去初始化）。"""
    error = expect_error(ErrorCode.NOT_FOUND, lambda: harness.services.get_config("p_ghost"))
    assert error.where["project_id"] == "p_ghost"

    project = harness.services.create_project(name="空配置")
    error = expect_error(
        ErrorCode.CONFIG_INVALID, lambda: harness.services.get_config(project.project_id)
    )
    assert error.remediation, "必须给出「接下来做什么」，否则界面只能显示一句错误"


def test_demo_init_is_idempotent_and_marks_the_project(harness: Harness) -> None:
    project = harness.services.create_project(name="演示")
    first = harness.services.demo_init(project.project_id)
    second = harness.services.demo_init(project.project_id)
    assert first == second
    assert [entry.doc_key for entry in first.documents] == list(DEMO_DOC_KEYS)
    assert harness.services.get_project(project.project_id).demo is True
    assert len(harness.services.list_config_versions(project.project_id)) == 1


def test_put_config_appends_a_version_and_never_overwrites(harness: Harness) -> None:
    project = harness.services.create_project(name="配置")
    config = ProjectConfig(
        project_id=project.project_id, author="写的人", documents=[document_entry("overview")]
    )
    first = harness.services.put_config(project.project_id, config, author="甲")
    second = harness.services.put_config(project.project_id, config, author="乙")
    assert (first.version, second.version) == (1, 2)
    assert [
        entry.author for entry in harness.services.list_config_versions(project.project_id)
    ] == [
        "甲",
        "乙",
    ]
    assert harness.services.get_config(project.project_id).author == "写的人"


def test_put_config_rejects_a_project_id_mismatch(harness: Harness) -> None:
    """路径与请求体指向不同项目时不能"以其中一个为准"——那会让写入落到意料之外的项目。"""
    project = harness.services.create_project(name="甲")
    other = harness.services.create_project(name="乙")
    config = ProjectConfig(project_id=other.project_id, author="t")
    error = expect_error(
        ErrorCode.CONFIG_INVALID,
        lambda: harness.services.put_config(project.project_id, config, author=None),
    )
    assert error.where["path_project_id"] == project.project_id
    assert error.where["body_project_id"] == other.project_id


def test_config_version_in_the_body_is_ignored_by_the_repository(harness: Harness) -> None:
    """客户端传什么版本号都不算数——版本由服务端派生（否则并发写入会撞号）。"""
    project = harness.services.create_project(name="版本")
    config = ProjectConfig(project_id=project.project_id, author="t", version=99)
    assert harness.services.put_config(project.project_id, config, author=None).version == 1


# ------------------------------------------------------------------ 上传
def test_accept_upload_writes_a_file_and_records_it(harness: Harness) -> None:
    record = harness.services.accept_upload(filename="t.docx", payload=DOCX)
    assert Path(record.stored_path).is_file()
    assert Path(record.stored_path).read_bytes() == DOCX
    assert Path(
        record.stored_path
    ).is_absolute(), "存相对路径会被 ensure_within 按工作目录解析，从而误判越界"
    assert harness.services.list_uploads() == [record]


def test_accept_upload_rejects_an_unknown_purpose(harness: Harness) -> None:
    """``purpose`` 只用于留痕，但自由字符串会让"模板包"与"模板包 "在留痕里变成两类。"""
    error = expect_error(
        ErrorCode.UPLOAD_TYPE,
        lambda: harness.services.accept_upload(filename="t.docx", payload=DOCX, purpose="随便"),
    )
    assert error.where["allowed"] == ["profile", "reference", "template"]


def test_accept_upload_populates_zip_members_so_the_macro_gate_fires(harness: Harness) -> None:
    """回归：``accept_upload`` 曾不传 ``members``，第④道宏闸门等于不存在。"""
    import io
    import zipfile

    from docfidelity.contracts.errors import ErrorCode as Codes

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("word/vbaProject.bin", b"\x00")
    expect_error(
        Codes.UPLOAD_TYPE,
        lambda: harness.services.accept_upload(filename="t.docx", payload=buffer.getvalue()),
    )


# --------------------------------------------------------------- 发起构建
def test_submit_build_rejects_a_project_without_config(harness: Harness) -> None:
    project = harness.services.create_project(name="未配置")
    expect_error(
        ErrorCode.CONFIG_INVALID, lambda: harness.services.submit_build(project.project_id)
    )


def test_submit_build_rejects_an_unknown_doc_key(harness: Harness) -> None:
    project_id = harness.new_project()
    error = expect_error(
        ErrorCode.PRECHECK_FAILED,
        lambda: harness.services.submit_build(project_id, doc_keys=["没有这份"]),
    )
    assert error.where["missing"] == ["没有这份"]


def test_submit_build_rejects_a_project_without_enabled_documents(harness: Harness) -> None:
    project = harness.services.create_project(name="全禁用")
    harness.services.put_config(
        project.project_id,
        ProjectConfig(
            project_id=project.project_id,
            author="t",
            documents=[document_entry("overview", enabled=False)],
        ),
        author="t",
    )
    expect_error(
        ErrorCode.PRECHECK_FAILED, lambda: harness.services.submit_build(project.project_id)
    )


def test_submit_build_points_at_the_missing_field(harness: Harness) -> None:
    """缺必填引用时要报出**是哪一份的哪一项**，而不是笼统一句"配置不完整"。"""
    project = harness.services.create_project(name="缺引用")
    harness.services.put_config(
        project.project_id,
        ProjectConfig(
            project_id=project.project_id,
            author="t",
            documents=[document_entry("overview", template_ref="")],
        ),
        author="t",
    )
    error = expect_error(
        ErrorCode.PRECHECK_FAILED, lambda: harness.services.submit_build(project.project_id)
    )
    assert error.where["problems"] == [{"doc_key": "overview", "field": "template_ref"}]


def test_submit_build_refuses_when_stages_are_not_registered(tmp_path: Path) -> None:
    """缺阶段要在**发起前**拦：否则作业以 ``failed`` 收场，而界面会把它读成
    "这份文档没构建成功"——真正的原因是服务端配置不全。"""
    harness = build_harness(
        tmp_path, stages=StageRegistry({"ingest": ArtifactStage(name="ingest")})
    )
    project_id = harness.new_project()
    error = expect_error(
        ErrorCode.PRECHECK_FAILED, lambda: harness.services.submit_build(project_id)
    )
    assert error.where["missing_stages"] == ["compose", "render", "refresh", "qa"]
    assert error.where["registered_stages"] == ["ingest"]


def test_submit_build_creates_no_job_when_a_precheck_fails(harness: Harness) -> None:
    project_id = harness.new_project()
    expect_error(
        ErrorCode.PRECHECK_FAILED,
        lambda: harness.services.submit_build(project_id, doc_keys=["没有这份"]),
    )
    assert harness.services.list_jobs() == []


# --------------------------------------------------------------- 生命周期
def test_cancel_does_not_let_an_old_success_pull_the_parent_back(tmp_path: Path) -> None:
    """取消后**不重新聚合**：否则一份早已成功的子作业会把父作业从 ``canceled``
    拉回 ``partial_failure``——用户点了"取消"，状态却弹回"部分失败"。"""
    harness = build_harness(tmp_path, stages=build_registry(qa={"failures": 1}))
    project_id = harness.new_project()
    orchestration_id = harness.build(project_id)
    parent = harness.repository.get_job(orchestration_id)
    assert parent.status == "partial_failure", "前提：一失败一成功"

    harness.services.cancel(orchestration_id)
    assert harness.repository.get_job(orchestration_id).status == "canceled"
    assert [child.status for child in harness.services.job_children(orchestration_id)] == [
        "canceled",
        "succeeded",
    ], "已成功的子作业没有到 canceled 的迁移，故保持不变"


def test_retry_on_a_succeeded_job_reports_not_resumable(harness: Harness) -> None:
    project_id = harness.new_project()
    child = harness.repository.get_job(harness.build(project_id)).child_job_ids[0]
    error = expect_error(
        ErrorCode.NOT_RESUMABLE, lambda: harness.services.retry(child, mode="stage")
    )
    assert error.where["allowed_actions"] == []
    assert error.where["resumable_actions"] == ["restart", "resume_from", "retry_stage"]


def test_retry_rejects_an_unknown_mode(harness: Harness) -> None:
    project_id = harness.new_project()
    child = harness.repository.get_job(harness.build(project_id)).child_job_ids[0]
    error = expect_error(
        ErrorCode.CONFIG_INVALID, lambda: harness.services.retry(child, mode="等下再说")
    )
    assert error.where["allowed"] == ["full", "stage"]


def test_cancel_on_a_succeeded_job_reports_state_invalid(harness: Harness) -> None:
    """``cancel`` 报 ``E_STATE_INVALID``（作业自己的状态不允许），不是 ``E_JOB_CONFLICT``。"""
    project_id = harness.new_project()
    child = harness.repository.get_job(harness.build(project_id)).child_job_ids[0]
    error = expect_error(ErrorCode.STATE_INVALID, lambda: harness.services.cancel(child))
    assert error.where["action"] == "cancel"
    assert error.where["status"] == "succeeded"


def test_resume_from_an_unknown_stage_reports_not_found(harness: Harness) -> None:
    """阶段名是**闭集**：自创一个必须报错，而不是被当成某个默认阶段。"""
    project_id = harness.new_project()
    child = harness.repository.get_job(
        harness.services.submit_build(project_id).job_id
    ).child_job_ids[0]
    error = expect_error(
        ErrorCode.NOT_FOUND,
        lambda: harness.services.resume(child, from_stage="印刷"),  # type: ignore[arg-type]
    )
    assert error.where["known"] == ["ingest", "compose", "render", "refresh", "qa"]


def test_resume_requires_upstream_checkpoints(harness: Harness) -> None:
    """ "从 X 续跑"而 X 的上游没有断点时**必须拒**：否则它会静默退化成"从头跑"，
    而那正是用户最不想发生的事（他选的正是"别重跑前面"）。"""
    project_id = harness.new_project()
    child = harness.repository.get_job(
        harness.services.submit_build(project_id).job_id
    ).child_job_ids[0]
    error = expect_error(
        ErrorCode.CHECKPOINT_MISSING, lambda: harness.services.resume(child, from_stage="render")
    )
    assert error.where["missing"] == ["ingest", "compose"]
    assert error.where["from_stage"] == "render"


def test_resume_resets_downstream_of_the_resume_point(tmp_path: Path) -> None:
    """续跑点的语义是"从这里重来"：它自己及其下游的断点一律丢。"""
    harness = build_harness(tmp_path, stages=build_registry(render={"failures": 1}))
    project_id = harness.new_project()
    orchestration_id = harness.build(project_id)
    child = harness.repository.get_job(orchestration_id).child_job_ids[0]
    harness.services.resume(child, from_stage="compose")
    assert harness.repository.get_job(child).status == "running"
    assert harness.runner.has_checkpoint(child, "ingest") is True, "上游断点必须留着"
    assert harness.runner.plan_resume(child) == ("compose", "render", "refresh", "qa")


def test_rerun_failed_only_touches_failed_children(tmp_path: Path) -> None:
    harness = build_harness(
        tmp_path,
        stages=build_registry(render={"artifacts": ("out.docx",)}, qa={"failures": 1}),
    )
    project_id = harness.new_project()
    orchestration_id = harness.build(project_id)
    children = harness.services.job_children(orchestration_id)
    failed, succeeded = children[0], children[1]
    assert (failed.status, succeeded.status) == ("failed", "succeeded")

    reset = harness.services.rerun_failed(orchestration_id)
    assert [item.job_id for item in reset] == [failed.job_id]
    assert harness.repository.get_job(failed.job_id).status == "queued"
    # 已成功的那个：产物与断点都不动。
    assert harness.repository.get_job(succeeded.job_id).status == "succeeded"
    assert harness.repository.list_artifacts(succeeded.job_id)
    assert harness.runner.has_checkpoint(succeeded.job_id, "ingest") is True

    harness.run_all()
    assert harness.repository.get_job(orchestration_id).status == "succeeded"


def test_rerun_failed_on_a_document_job_reports_state_invalid(harness: Harness) -> None:
    project_id = harness.new_project()
    child = harness.repository.get_job(harness.build(project_id)).child_job_ids[0]
    error = expect_error(ErrorCode.STATE_INVALID, lambda: harness.services.rerun_failed(child))
    assert error.where["kind"] == "document"


# ------------------------------------------------------------ 产物与报告
def test_artifact_path_returns_the_file_and_blocks_expired_ones(tmp_path: Path) -> None:
    harness = build_harness(tmp_path, stages=build_registry(render={"artifacts": ("out.docx",)}))
    project_id = harness.new_project()
    child = harness.repository.get_job(harness.build(project_id)).child_job_ids[0]
    artifact = harness.repository.list_artifacts(child)[0]
    assert harness.services.artifact_path(artifact.artifact_id).is_file()

    harness.clock.advance(days=harness.settings.retention_artifacts_days + 1)
    error = expect_error(
        ErrorCode.ARTIFACT_EXPIRED, lambda: harness.services.artifact_path(artifact.artifact_id)
    )
    assert error.where["artifact_id"] == artifact.artifact_id


def test_artifact_path_reports_not_found_when_the_file_is_gone(tmp_path: Path) -> None:
    """登记还在、文件没了 → ``E_NOT_FOUND``（可操作动作是"重新构建"）。"""
    harness = build_harness(tmp_path, stages=build_registry(render={"artifacts": ("out.docx",)}))
    project_id = harness.new_project()
    child = harness.repository.get_job(harness.build(project_id)).child_job_ids[0]
    artifact = harness.repository.list_artifacts(child)[0]
    harness.services.artifact_path(artifact.artifact_id).unlink()
    error = expect_error(
        ErrorCode.NOT_FOUND, lambda: harness.services.artifact_path(artifact.artifact_id)
    )
    assert error.where["relative_path"] == artifact.relative_path


def test_report_missing_is_its_own_code(harness: Harness) -> None:
    project_id = harness.new_project()
    child = harness.repository.get_job(harness.build(project_id)).child_job_ids[0]
    expect_error(ErrorCode.REPORT_MISSING, lambda: harness.services.get_report(child))


def test_report_is_readable_when_qa_produced_one(tmp_path: Path) -> None:
    harness = build_harness(tmp_path, stages=build_registry(qa={"emit_report": True}))
    project_id = harness.new_project()
    child = harness.repository.get_job(harness.build(project_id)).child_job_ids[0]
    assert harness.services.get_report(child).verdict == "pass"


# ------------------------------------------------------------ 设置与边界
def test_settings_view_never_exposes_the_key(harness: Harness) -> None:
    """DD-13.7 的数据边界：响应模型里**没有**密钥字段，不是"记得别放"。"""
    harness.settings.model_api_key = SecretStr("super-secret")  # type: ignore[assignment]
    view = harness.services.settings_view()
    assert "super-secret" not in repr(view)
    assert "model_api_key" not in repr(view)
    assert set(view["model"]) == {"enabled", "name", "base_url"}
    assert set(view["data_boundary"]) == {
        "format_version",
        "deployment",
        "model_enabled",
        "model_base_url",
        "external_calls",
        "storage_path",
    }


def test_data_boundary_derives_external_calls_from_both_switches(harness: Harness) -> None:
    """``external_calls`` 由开关与地址**共同**推出，不另立开关——否则允许自相矛盾的状态。"""
    assert harness.services.settings_view()["data_boundary"]["external_calls"] is False
    harness.settings.model_base_url = "http://127.0.0.1:9/"
    assert harness.services.settings_view()["data_boundary"]["external_calls"] is False
    harness.settings.model_enabled = True
    assert harness.services.settings_view()["data_boundary"]["external_calls"] is True


def test_update_settings_accepts_mutable_fields_in_place(harness: Harness) -> None:
    """就地改而不是换成新对象：``runner`` 持有同一实例，换对象会让"改完设置但 worker
    还用旧值"以"要重启两次才生效"的形式表现出来。"""
    before = harness.settings
    harness.services.update_settings({"deployment": "intranet", "sse_heartbeat_seconds": 5.0})
    assert harness.settings is before, "设置必须就地改，不能换成新对象"
    assert harness.settings.deployment == "intranet"
    assert harness.settings.sse_heartbeat_seconds == 5.0
    assert harness.settings.retention_policy().artifacts_days == 180, "未改动的字段不变"


def test_update_settings_rejects_secrets_and_storage_root(harness: Harness) -> None:
    for patch in ({"model_api_key": "x"}, {"storage_root": "/tmp/elsewhere"}, {"不存在的字段": 1}):
        error = expect_error(
            ErrorCode.CONFIG_INVALID, lambda patch=patch: harness.services.update_settings(patch)
        )
        assert error.where["rejected"] == sorted(patch)


def test_update_settings_validates_the_whole_candidate_before_writing(harness: Harness) -> None:
    """一处不合法就不能写一半——半新半旧的设置比拒绝更难查。

    这里断言的是"**没有半写**"：第一个字段虽然合法，也必须原样保留。抛的是 pydantic 的
    校验异常而不是 ``EngineError``——HTTP 层会把它兜成 500（那是**缺陷**，不是设计），
    所以这条用例只钉住"没写一半"。
    """
    with pytest.raises(ValidationError):
        harness.services.update_settings({"deployment": "intranet", "sse_heartbeat_seconds": -1})
    assert harness.settings.deployment == "local"
    assert harness.settings.sse_heartbeat_seconds == 15.0


def test_probe_model_reports_not_probed_when_disabled(harness: Harness) -> None:
    """把"没开"显示成"连不上"，会让人去查网络——而该做的是打开开关或什么都不做。"""
    probe = harness.services.probe_model()
    assert probe == {
        "enabled": False,
        "base_url": None,
        "reachable": None,
        "detail": DETERMINISTIC_REASON,
    }


def test_probe_model_uses_the_injected_prober(harness: Harness) -> None:
    harness.settings.model_enabled = True
    harness.settings.model_base_url = "http://example.invalid/"
    services = dataclasses.replace(harness.services, model_prober=lambda url: (True, "HTTP 200"))
    assert services.probe_model()["reachable"] is True


def test_probe_model_without_a_base_url_does_not_probe(harness: Harness) -> None:
    harness.settings.model_enabled = True
    assert harness.services.probe_model()["reachable"] is None


# ------------------------------------------- 模型能力（受限解码 + 基线，P0-17）
class TestModelCapabilities:
    """``GET /settings/model/capabilities``：接上模型之后**会怎么跑**。

    与 ``/settings/model/health``（连得上吗）分开：合成一张卡会让"连不上"与
    "没有受限解码 / 没有基线数据"看起来是同一件事，而三者的处置完全不同。
    """

    @staticmethod
    def _capabilities(harness: Harness, tmp_path: Path, by_point: dict[str, float]) -> dict:
        from docfidelity.agents.baseline import BaselineReport, save_report
        from docfidelity.pipeline import BASELINE_FILENAME

        save_report(
            BaselineReport(model="m-1", cases_digest="e" * 8, by_point=by_point),
            tmp_path / BASELINE_FILENAME,
        )
        return harness.services.model_capabilities()

    def test_without_a_report_the_reason_is_spelled_out(self, harness: Harness) -> None:
        """没跑过评测是**正常状态**，但必须与"报告坏了"分开说。

        两者都让路由"按目录声明"放行，处置却完全不同（去跑一次 / 去查资产）。
        合并成一句"基线未生效"会让人去查错的地方。
        """
        got = harness.services.model_capabilities()

        assert got["enabled"] is False
        assert got["baseline"]["loaded"] is False
        assert "未找到" in got["baseline"]["problem"]
        assert got["baseline"]["by_point"] == {}
        # 本机没装受限解码后端时这里是 False，装了就是 True——
        # 断言的是"它是个明确结论"，而不是某个特定环境下的取值。
        assert isinstance(got["grammar"]["supported"], bool)
        assert got["grammar"]["detail"]

    def test_a_loaded_report_is_summarised(self, harness: Harness, tmp_path: Path) -> None:
        got = self._capabilities(harness, tmp_path, {"placement.section_slot": 0.25})

        assert got["baseline"]["loaded"] is True
        assert got["baseline"]["problem"] == ""
        assert got["baseline"]["model"] == "m-1"
        assert got["baseline"]["by_point"] == {"placement.section_slot": 0.25}
        # 未覆盖点必须一并下发：界面据此显示"未测 ≠ 已验证"。
        assert "placement.section_slot" not in got["baseline"]["uncovered"]

    def test_enabled_mirrors_the_switch_not_the_report(
        self, harness: Harness, tmp_path: Path
    ) -> None:
        """``enabled`` 说的是**这次构建会不会调模型**，与评测报告在不在无关。"""
        harness.settings.model_enabled = True
        got = self._capabilities(harness, tmp_path, {"placement.section_slot": 0.25})
        assert got["enabled"] is True

        harness.settings.model_enabled = False
        got = harness.services.model_capabilities()
        assert got["enabled"] is False

    def test_the_response_model_accepts_what_the_service_returns(
        self, harness: Harness, tmp_path: Path
    ) -> None:
        """端点的 handler 只有一行：``ModelCapabilities.model_validate(services…)``。

        缺了这条，"服务端下发的形状"与"``response_model`` 认的形状"各说各话，而两者的
        分歧只在**运行时**炸成 500——服务层用例（直接读 ``services.model_capabilities()``）
        与内核单测都看不见（硬规则 17 之所以要求接口必须有 ``response_model``，前提正是
        它认的形状就是下发的形状）。
        """
        from server.api.schemas import ModelCapabilities

        # 三态各验一次：没报告 / 有报告。两者的字段存在性不同（后者才有 model/digest）。
        for payload in (
            harness.services.model_capabilities(),
            self._capabilities(harness, tmp_path, {"placement.section_slot": 0.25}),
        ):
            validated = ModelCapabilities.model_validate(payload)
            dumped = validated.model_dump(mode="json")
            assert dumped["baseline"]["by_point"] == payload["baseline"]["by_point"]
            # 收窄的那三个字段（by_point/failures/uncovered）序列化后必须**始终在**
            # ——前端据此无条件渲染，不用再判杂 undefined。
            assert {"by_point", "failures", "uncovered"} <= set(dumped["baseline"])
            # 姿态指纹必须下到界面：通过率脱离姿态就是一句不可比的话。
            assert "routing_digest" in dumped["baseline"]
            assert dumped["grammar"]["detail"] == payload["grammar"]["detail"]


# ------------------------------------------------------------ 保留与清理
def test_expired_now_lists_aged_uploads_and_workspaces(wall_clocked: Harness) -> None:
    harness = wall_clocked
    harness.services.accept_upload(filename="t.docx", payload=DOCX)
    project_id = harness.new_project()
    harness.build(project_id)
    listed = {item.kind for item in harness.services.expired_now()}
    assert listed == set(), "刚创建的东西不该出现在过期列表里"

    harness.clock.advance(days=harness.settings.retention_uploads_days + 1)
    kinds = {item.kind for item in harness.services.expired_now()}
    assert "upload" in kinds
    assert (
        "workspace" in kinds
    ), f"工作区保留 {harness.settings.retention_workspace_days} 天，也应已过期"


def test_cleanup_reclaims_files_and_drops_the_records(harness: Harness) -> None:
    record = harness.services.accept_upload(filename="t.docx", payload=DOCX)
    stored = Path(record.stored_path)
    harness.clock.advance(days=harness.settings.retention_uploads_days + 1)

    plan = harness.services.cleanup(requested=[record.upload_id])
    assert plan.object_ids() == (record.upload_id,)
    assert plan.total_bytes == len(DOCX)
    assert not stored.exists()
    assert harness.services.list_uploads() == [], "文件删了、登记还在，下载接口就会指着一个空路径"
    assert harness.services.expired_now() == []


def test_cleanup_reclaims_a_workspace_directory(wall_clocked: Harness) -> None:
    harness = wall_clocked
    project_id = harness.new_project()
    orchestration_id = harness.build(project_id)
    child = harness.repository.get_job(orchestration_id).child_job_ids[0]
    job_dir = harness.root / "jobs" / child
    assert job_dir.is_dir()

    harness.clock.advance(days=harness.settings.retention_workspace_days + 1)
    # 按 id 精确定位这一个工作区（``jobs/`` 下还有编排作业自己的目录，
    # 它只放事件文件——"第一个工作区对象"未必是这份作业的）。
    target = next(
        item for item in harness.services.expired_now() if item.object_id == f"ws:{child}"
    )
    harness.services.cleanup(requested=[target.object_id])
    assert not job_dir.exists()


def test_cleanup_refuses_objects_that_are_not_expired(harness: Harness) -> None:
    record = harness.services.accept_upload(filename="t.docx", payload=DOCX)
    error = expect_error(
        ErrorCode.CONFIRM_REQUIRED, lambda: harness.services.cleanup(requested=[record.upload_id])
    )
    assert error.where["not_expired"] == [record.upload_id]
    assert harness.services.list_uploads() == [record]


def test_cleanup_refuses_an_empty_request(harness: Harness) -> None:
    error = expect_error(ErrorCode.CONFIRM_REQUIRED, lambda: harness.services.cleanup(requested=[]))
    assert error.remediation


def test_cleanup_reports_unknown_objects(harness: Harness) -> None:
    expect_error(ErrorCode.NOT_FOUND, lambda: harness.services.cleanup(requested=["u_ghost"]))


def test_retained_objects_covers_all_three_kinds(tmp_path: Path) -> None:
    harness = build_harness(tmp_path, stages=build_registry(render={"artifacts": ("out.docx",)}))
    harness.services.accept_upload(filename="t.docx", payload=DOCX)
    project_id = harness.new_project()
    harness.build(project_id)
    kinds = {item.kind for item in harness.services.retained_objects()}
    assert kinds == {"upload", "workspace", "artifact"}


def test_expiring_an_artifact_makes_it_a_cleanup_candidate(tmp_path: Path) -> None:
    """产物过期后可被回收，且**登记一起摘掉**（否则列表里会留下打不开的死条目）。

    这里刻意一次清**整批**而不是挑一个：演示配置有两个文档键，``render`` 因此
    产出两份 ``out.docx``。只清一份就断言"没有产物了"，测的其实是"我恰好挑中了最后一份"。
    """
    harness = build_harness(tmp_path, stages=build_registry(render={"artifacts": ("out.docx",)}))
    project_id = harness.new_project()
    harness.build(project_id)
    harness.clock.advance(days=harness.settings.retention_artifacts_days + 1)

    expired = [item for item in harness.services.expired_now() if item.kind == "artifact"]
    assert len(expired) == len(DEMO_DOC_KEYS), "前提：每个文档各产出一份产物"

    plan = harness.services.cleanup(requested=[item.object_id for item in expired])
    assert len(plan.artifacts) == len(expired)
    assert not [item for item in harness.services.retained_objects() if item.kind == "artifact"]
    assert all(harness.repository.find_artifact(item.object_id) is None for item in expired)


def test_upload_expiry_is_independent_of_artifact_expiry(harness: Harness) -> None:
    """上传件 30 天、产物 180 天：合成一个天数会让其中一类总是过长或过短。

    这里没有构建，故没有工作区与产物——正因如此，"31 天后只剩上传件过期"是干净的判据。
    """
    harness.services.accept_upload(filename="t.docx", payload=DOCX)
    harness.clock.advance(days=31)
    kinds = {item.kind for item in harness.services.expired_now()}
    assert kinds == {"upload"}
    assert harness.settings.retention_artifacts_days > 31, "前提：产物保留期远长于上传件"


def test_clock_advance_keeps_timestamps_consistent(harness: Harness) -> None:
    """可推进时钟的基本保证：``advance`` 之后的时刻严格靠后。"""
    start = harness.clock()
    later = harness.clock.advance(hours=1)
    assert later == start + timedelta(hours=1)
    assert harness.services.now() == later


class TestDeterministicPose:
    """确定性姿态的生效值与成因（TASK-P0-19 / DD-16.2）。

    这些用例守的是一句容易被配错的话："显式表态优先于缺省姿态"。反过来（姿态压过
    显式）会让 ``DOCFIDELITY_OFFICE_ENABLED=true`` 变成一句不起作用的配置——
    "配了但没生效"最典型的来源。
    """

    def test_the_default_pose_keeps_word_out(self, harness: Harness) -> None:
        """默认（姿态开、未显式表态）⇒ 不用 Word，且**说得出为什么**。"""
        assert harness.settings.deterministic_mode is True
        assert harness.settings.office_enabled is None

        assert harness.settings.allow_office() is False
        assert harness.settings.office_reason() == FORCED_DETERMINISTIC_REASON

    def test_an_explicit_true_beats_the_pose(self, harness: Harness) -> None:
        harness.settings.office_enabled = True

        assert harness.settings.allow_office() is True
        assert harness.settings.office_reason() is None, "开着就不该有原因"

    def test_an_explicit_false_has_its_own_reason(self, harness: Harness) -> None:
        """显式关闭与姿态关闭**成因不同**：前者去查配置，后者去改姿态。"""
        harness.settings.office_enabled = False

        reason = harness.settings.office_reason()
        assert harness.settings.allow_office() is False
        assert reason is not None and reason != FORCED_DETERMINISTIC_REASON
        assert "OFFICE_ENABLED" in reason

    def test_turning_the_pose_off_restores_probing(self, harness: Harness) -> None:
        """姿态关掉 ⇒ 回到"探测本机"的历史行为（未显式表态时）。"""
        harness.settings.deterministic_mode = False

        assert harness.settings.allow_office() is True
        assert harness.settings.office_reason() is None

    def test_the_view_reports_the_effective_value_not_the_switch(self, harness: Harness) -> None:
        """``GET /settings`` 报的是**生效值**。

        报开关值等于让人自己去推"两个开关谁压谁"——而那正是配错的地方。故这里同时
        断言响应模型认这个形状（``response_model`` 与实际下发不一致只会在运行期炸 500）。
        """
        from server.api.schemas import SettingsView

        harness.settings.deterministic_mode = False
        harness.settings.office_enabled = False
        view = SettingsView.model_validate(harness.services.settings_view())

        assert view.deterministic.mode is False
        assert view.deterministic.office_enabled is False
        assert view.deterministic.office_reason is not None

    def test_the_settings_view_accepts_the_default_shape(self, harness: Harness) -> None:
        """默认姿态下也要能过响应模型（``office_reason`` 非空那条路径）。"""
        from server.api.schemas import SettingsView

        view = SettingsView.model_validate(harness.services.settings_view())
        assert view.deterministic.mode is True
        assert view.deterministic.office_enabled is False

    def test_the_pose_reaches_the_frozen_payload(self, harness: Harness) -> None:
        """Office 姿态进**冻结输入**（不是留给阶段体自己探测）。

        本机装没装 Word 会改变产物字节（目录页码刷没刷）。留给阶段体探测就等于让产物
        成为"跑在哪台机器上"的函数，NFR-02 的确定性当场失效——而它的症状（换台机器
        基线就对不上）在开发机上跑不出来。故这条落在**载荷**上，不停在"设置读得到"。
        """
        project_id = harness.new_project()
        harness.services.submit_build(project_id)
        child = harness.repository.list_jobs(kind="document")[0]

        assert harness.settings.deterministic_mode is True, "本条验的是默认姿态，前提要先成立"
        payload = harness.runner.build_payload(child)
        assert payload["office_enabled"] is False
        assert payload["office_reason"] == FORCED_DETERMINISTIC_REASON

    def test_an_explicit_switch_reaches_the_frozen_payload(self, harness: Harness) -> None:
        """显式打开 ⇒ 冻结值随之为真，且原因留空（没关掉它就没有"为什么"）。"""
        project_id = harness.new_project()
        harness.settings.office_enabled = True
        harness.services.submit_build(project_id)
        child = harness.repository.list_jobs(kind="document")[0]

        payload = harness.runner.build_payload(child)
        assert payload["office_enabled"] is True
        assert payload["office_reason"] is None
