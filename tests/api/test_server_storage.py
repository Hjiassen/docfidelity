"""内存仓储（DD-13.1 的 ``server/storage``）。

两条**跨实现**的纪律，各自有一组用例：

1. **本层不判定状态迁移**：``save_job`` 是持久化原语。这里断言"存什么就是什么"，
   以及"读取返回副本"——否则"改了一个从仓储取出来的作业"会静默改写库里的作业。
2. **唯一性约束真的会触发**：项目名唯一不是"将来上数据库再加"的事，用户连点两次
   "新建项目"当场就会撞上，所以内存版也必须拦。

内存版与将来的落库版**语义等价**是刻意要求：如果内存版更宽松，那就是"测试全绿、
上线就炸"，而且炸在事务与唯一约束上——最不好查的那类。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta, timezone

import pytest

from docfidelity.contracts.config import ProjectConfig
from docfidelity.contracts.errors import EngineError, ErrorCode
from docfidelity.contracts.job import Artifact, Job
from server.storage import ConfigVersion, MemoryRepository, Project, UploadRecord
from server.uploads import UploadReceipt

pytestmark = pytest.mark.api

NOW = datetime(2026, 9, 22, 9, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def repo() -> MemoryRepository:
    return MemoryRepository()


def project(project_id: str = "p_1", name: str = "项目甲", **kwargs: object) -> Project:
    fields: dict[str, object] = {"created_at": NOW, "updated_at": NOW}
    fields.update(kwargs)
    return Project(project_id=project_id, name=name, **fields)  # type: ignore[arg-type]


def job(job_id: str = "j_1", *, project_id: str = "p_1", status: str = "queued") -> Job:
    return Job(
        job_id=job_id,
        kind="document",
        project_id=project_id,
        status=status,  # type: ignore[arg-type]
        created_at=NOW,
        inputs_hash="h1",
    )


def config(project_id: str = "p_1") -> ProjectConfig:
    return ProjectConfig(project_id=project_id, author="t")


def expect_error(code: str, fn: Callable[[], object]) -> EngineError:
    """断言某个**可调用**抛出指定错误码。

    收可调用而不是"已经求过值的结果"：``expect_error(code, repo.get_job("x"))``
    会在进入 ``pytest.raises`` 之前就抛出，于是用例失败在一个与被测行为无关的地方。
    """
    with pytest.raises(EngineError) as caught:
        fn()
    assert caught.value.code == code, caught.value.message
    return caught.value


# ------------------------------------------------------------------ 项目
def test_missing_project_reports_not_found(repo: MemoryRepository) -> None:
    error = expect_error(ErrorCode.NOT_FOUND, lambda: repo.get_project("p_none"))
    assert error.where["project_id"] == "p_none"


def test_find_project_returns_none_instead_of_raising(repo: MemoryRepository) -> None:
    assert repo.find_project("p_none") is None


def test_duplicate_name_among_active_projects_is_rejected(repo: MemoryRepository) -> None:
    repo.add_project(project("p_1", "同名"))
    expect_error(ErrorCode.PROJECT_NAME_TAKEN, lambda: repo.add_project(project("p_2", "同名")))


def test_archiving_frees_the_name(repo: MemoryRepository) -> None:
    repo.add_project(project("p_1", "同名"))
    repo.save_project(project("p_1", "同名", archived=True))
    stored = repo.add_project(project("p_2", "同名"))
    assert stored.name == "同名"


def test_renaming_onto_itself_is_allowed(repo: MemoryRepository) -> None:
    """改名到"它自己现在的名字"必须成功——否则界面上的保存按钮会偶尔无故报错。"""
    repo.add_project(project("p_1", "旧名"))
    assert repo.save_project(project("p_1", "旧名")).name == "旧名"


def test_renaming_onto_another_projects_name_is_rejected(repo: MemoryRepository) -> None:
    repo.add_project(project("p_1", "甲"))
    repo.add_project(project("p_2", "乙"))
    expect_error(ErrorCode.PROJECT_NAME_TAKEN, lambda: repo.save_project(project("p_2", "甲")))


def test_saving_an_unknown_project_is_rejected(repo: MemoryRepository) -> None:
    expect_error(ErrorCode.NOT_FOUND, lambda: repo.save_project(project("p_ghost")))


def test_archived_projects_are_hidden_by_default(repo: MemoryRepository) -> None:
    repo.add_project(project("p_1", "在册"))
    repo.add_project(project("p_2", "已归档", archived=True))
    assert [item.project_id for item in repo.list_projects()] == ["p_1"]
    assert {item.project_id for item in repo.list_projects(include_archived=True)} == {
        "p_1",
        "p_2",
    }


def test_listing_is_ordered_by_creation_time(repo: MemoryRepository) -> None:
    later = NOW + timedelta(minutes=1)
    repo.add_project(project("p_late", "晚", created_at=later, updated_at=later))
    repo.add_project(project("p_early", "早"))
    assert [item.project_id for item in repo.list_projects()] == ["p_early", "p_late"]


# ------------------------------------------------------------------ 配置
def test_config_version_is_derived_by_the_repository(repo: MemoryRepository) -> None:
    """版本号由仓储自增，**不接受调用方指定**：并发的两次写入会拿到同一个号。"""
    first = repo.append_config(ConfigVersion("p_1", 999, config(), "t", NOW))
    second = repo.append_config(ConfigVersion("p_1", 999, config(), "t", NOW))
    assert (first.version, second.version) == (1, 2)


def test_latest_config_is_none_before_any_write(repo: MemoryRepository) -> None:
    assert repo.latest_config("p_1") is None


def test_latest_config_returns_the_newest_version(repo: MemoryRepository) -> None:
    repo.append_config(
        ConfigVersion("p_1", 1, ProjectConfig(project_id="p_1", author="old"), None, NOW)
    )
    repo.append_config(
        ConfigVersion("p_1", 2, ProjectConfig(project_id="p_1", author="new"), None, NOW)
    )
    assert repo.latest_config("p_1") is not None
    assert repo.latest_config("p_1").author == "new"  # type: ignore[union-attr]


def test_get_config_by_version(repo: MemoryRepository) -> None:
    repo.append_config(ConfigVersion("p_1", 1, config(), "t", NOW))
    assert repo.get_config("p_1", 1) is not None
    assert repo.get_config("p_1", 2) is None


def test_config_history_is_append_only(repo: MemoryRepository) -> None:
    for _ in range(3):
        repo.append_config(ConfigVersion("p_1", 1, config(), "t", NOW))
    assert [entry.version for entry in repo.list_configs("p_1")] == [1, 2, 3]


def test_config_reads_are_deep_copies(repo: MemoryRepository) -> None:
    """改动读出来的配置，不得影响库里的那份。"""
    repo.append_config(
        ConfigVersion("p_1", 1, ProjectConfig(project_id="p_1", author="原始"), "t", NOW)
    )
    loaded = repo.latest_config("p_1")
    assert loaded is not None
    loaded.author = "被改了"
    assert repo.latest_config("p_1").author == "原始"  # type: ignore[union-attr]


# ------------------------------------------------------------------ 作业
def test_job_reads_are_deep_copies(repo: MemoryRepository) -> None:
    repo.save_job(job())
    loaded = repo.get_job("j_1")
    loaded.child_job_ids.append("伪造的子作业")
    assert repo.get_job("j_1").child_job_ids == []


def test_missing_job_reports_not_found(repo: MemoryRepository) -> None:
    assert repo.find_job("j_none") is None
    expect_error(ErrorCode.NOT_FOUND, lambda: repo.get_job("j_none"))


def test_save_job_persists_the_status_it_was_given(repo: MemoryRepository) -> None:
    """仓储**不判定**迁移：给什么状态就存什么状态。

    这条用例钉住的是"仓储不越权"。若某天有人在这里加了"顺手把 queued 改成 running"，
    合法迁移就有了第二个判定口径，而它与状态机迟早不一致。
    """
    repo.save_job(job("j_1", status="running"))
    assert repo.get_job("j_1").status == "running"


def test_job_filters(repo: MemoryRepository) -> None:
    repo.save_job(job("j_1", project_id="p_1", status="queued"))
    repo.save_job(job("j_2", project_id="p_1", status="succeeded"))
    repo.save_job(job("j_3", project_id="p_2", status="queued"))
    assert [item.job_id for item in repo.list_jobs(project_id="p_1")] == ["j_1", "j_2"]
    assert [item.job_id for item in repo.list_jobs(status="queued")] == ["j_1", "j_3"]
    assert [item.job_id for item in repo.list_jobs(project_id="p_2", status="queued")] == ["j_3"]


def test_stages_are_replaced_wholesale_and_copied(repo: MemoryRepository) -> None:
    from docfidelity.contracts.job import Stage

    repo.save_stages("j_1", [Stage(name="ingest", status="succeeded")])
    repo.save_stages("j_1", [Stage(name="ingest", status="succeeded"), Stage(name="compose")])
    assert [stage.name for stage in repo.get_stages("j_1")] == ["ingest", "compose"]
    assert repo.get_stages("j_unknown") == []


# ------------------------------------------------------------------ 产物
def test_artifact_round_trip_and_removal(repo: MemoryRepository) -> None:
    artifact = Artifact(
        artifact_id="j_1:artifacts/out.docx",
        job_id="j_1",
        kind="docx",
        relative_path="artifacts/out.docx",
        size_bytes=12,
        sha256="abc",
        created_at=NOW,
    )
    repo.add_artifact(artifact)
    assert repo.get_artifact(artifact.artifact_id).kind == "docx"
    assert [item.artifact_id for item in repo.list_artifacts("j_1")] == [artifact.artifact_id]
    assert repo.remove_artifact(artifact.artifact_id) is True
    assert repo.remove_artifact(artifact.artifact_id) is False, "重复移除必须报 False 而不是炸"
    assert repo.find_artifact(artifact.artifact_id) is None


def test_missing_artifact_reports_not_found(repo: MemoryRepository) -> None:
    expect_error(ErrorCode.NOT_FOUND, lambda: repo.get_artifact("a_none"))


# ------------------------------------------------------------------ 上传件
def test_upload_round_trip_and_removal(repo: MemoryRepository) -> None:
    record = UploadRecord(
        upload_id="u_1",
        receipt=UploadReceipt(
            filename="t.docx",
            suffix=".docx",
            size_bytes=4,
            content_type="application/zip",
            digest="d",
        ),
        stored_path="/var/uploads/u_1/t.docx",
        created_at=NOW,
    )
    repo.add_upload(record)
    assert repo.get_upload("u_1").receipt.filename == "t.docx"
    assert len(repo.list_uploads()) == 1
    assert repo.remove_upload("u_1") is True
    assert repo.remove_upload("u_1") is False
    expect_error(ErrorCode.NOT_FOUND, lambda: repo.get_upload("u_1"))


# ------------------------------------------------------------------ 报告
def test_report_round_trip(repo: MemoryRepository) -> None:
    from docfidelity.contracts.qa import Report

    report = Report(
        report_id="r_1", job_id="j_1", generated_at=NOW, findings=[], waivers=[], verdict="pass"
    )
    repo.put_report("j_1", report)
    loaded = repo.find_report("j_1")
    assert loaded is not None and loaded.verdict == "pass"
    assert repo.find_report("j_none") is None


def test_all_seven_protocols_are_implemented() -> None:
    """聚合面必须真的覆盖六个子协议（少一个，类型检查会在装配处才爆）。"""
    from server.storage import Repository

    instance = MemoryRepository()
    for name in (
        "add_project",
        "get_project",
        "list_projects",
        "save_project",
        "append_config",
        "latest_config",
        "list_configs",
        "get_config",
        "save_job",
        "get_job",
        "find_job",
        "list_jobs",
        "save_stages",
        "get_stages",
        "add_artifact",
        "get_artifact",
        "find_artifact",
        "list_artifacts",
        "remove_artifact",
        "put_report",
        "find_report",
        "add_upload",
        "get_upload",
        "list_uploads",
        "remove_upload",
    ):
        assert callable(getattr(instance, name, None)), f"仓储缺少协议成员：{name}"
    # ``Repository`` 是 ``Protocol``（不可 ``isinstance``）；引用一次以确保它可导入，
    # 真正的一致性由 mypy 在 ``build_state`` 的装配处检查。
    assert Repository is not None
