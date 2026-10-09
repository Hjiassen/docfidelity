"""落库仓储（P0-14 第 2 轮；DD-13.1 的 ``server/storage``）。

本文件有两个职责，各自对应一条会真出事的风险：

1. **与内存版语义等价**。等价不是"差不多"：错误码 / ``where`` / ``remediation``
   逐字一致、名称唯一真的会拦、版本号由仓储派生、读出来的对象改不动库里那份。
   ``protocols`` 的模块文档把"内存版比落库版宽松"点名为最难查的一类 bug
   （测试全绿、上线就炸，炸在事务与唯一约束上）。故这一组用例**同时跑两种实现**
   （``repo`` 夹具参数化）——两边过同一批断言，"等价"才是被验证的事实。
2. **schema 只有一份**。生产建表走 Alembic，测试/演示走 ``Base.metadata``。两条路径
   必然有漂移风险，唯一的防漂移手段是让它们在同一批断言下对齐
   （``test_migration_schema_matches_orm_metadata``）。

另有一条只属于落库版的陷阱：**SQLite 不保存 ``tzinfo``**。用 ``DateTime(timezone=True)``
读回来是 naive datetime，而内存版给的是 aware——于是 ``expires_at <= now()`` 抛
``can't compare offset-naive and offset-aware datetimes``，保留期回收从"能跑"变成
"每次 500"。``test_aware_moments_survive_the_round_trip`` 专门钉住它。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import Engine, inspect

from docfidelity.contracts.config import ProjectConfig
from docfidelity.contracts.errors import EngineError, ErrorCode
from docfidelity.contracts.fact import SourceConfig
from docfidelity.contracts.job import Artifact, Job, Stage
from docfidelity.contracts.qa import Report
from docfidelity.pipeline import ARTIFACTS_DIRNAME
from server.storage import (
    Base,
    ConfigVersion,
    FactSourceRecord,
    MemoryRepository,
    Project,
    RegisteredProfile,
    Repository,
    SqlAlchemyRepository,
    UploadRecord,
    build_repository,
    create_engine_for,
    create_schema,
    is_memory_url,
    migration_config,
    run_migrations,
)
from server.uploads import UploadReceipt
from tests.fixtures.server_fixture import build_harness, build_registry

pytestmark = pytest.mark.api

NOW = datetime(2026, 9, 23, 9, 0, 0, tzinfo=timezone.utc)

#: Alembic 自己维护的版本表；它**不在** ORM 元数据里，比对时排除。
ALEMBIC_TABLE = "alembic_version"


# ------------------------------------------------------------------ 夹具
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


def config(project_id: str = "p_1", *, author: str = "t") -> ProjectConfig:
    return ProjectConfig(project_id=project_id, author=author)


def artifact(artifact_id: str = "a_1", job_id: str = "j_1") -> Artifact:
    return Artifact(
        artifact_id=artifact_id,
        job_id=job_id,
        kind="docx",
        relative_path="artifacts/out.docx",
        size_bytes=12,
        sha256="abc",
        created_at=NOW,
    )


def upload(upload_id: str = "u_1") -> UploadRecord:
    return UploadRecord(
        upload_id=upload_id,
        receipt=UploadReceipt(
            filename="t.docx",
            suffix=".docx",
            size_bytes=4,
            content_type="application/zip",
            digest="d",
        ),
        stored_path=f"/var/uploads/{upload_id}/t.docx",
        created_at=NOW,
    )


def database_url(tmp_path: Path, name: str = "df.db") -> str:
    return f"sqlite:///{(tmp_path / name).as_posix()}"


def migrated_engine(tmp_path: Path, name: str = "df.db") -> Engine:
    """走 **Alembic** 建表的引擎（生产路径）。"""
    url = database_url(tmp_path, name)
    run_migrations(url)
    return create_engine_for(url)


@pytest.fixture
def engine(tmp_path: Path) -> Engine:
    return migrated_engine(tmp_path)


@pytest.fixture
def sqla(engine: Engine) -> SqlAlchemyRepository:
    return SqlAlchemyRepository(engine)


@pytest.fixture(params=["memory", "sqlalchemy"])
def repo(request: pytest.FixtureRequest, engine: Engine) -> Repository:
    """**同一批断言跑两种实现**——"语义等价"由此成为被验证的事实，而不是文档里的一句话。"""
    if request.param == "memory":
        return MemoryRepository()
    return SqlAlchemyRepository(engine)


def expect_error(code: str, fn: Callable[[], object]) -> EngineError:
    with pytest.raises(EngineError) as caught:
        fn()
    assert caught.value.code == code, caught.value.message
    return caught.value


# ======================================================= 一、跨实现等价
def test_missing_entities_report_the_same_error_shape(repo: Repository) -> None:
    """错误文案是界面直接渲染的：两种仓储给出两种文案，等于"换个部署提示就变了"。"""
    expected = {
        "project_id": expect_error(ErrorCode.NOT_FOUND, lambda: repo.get_project("p_none")),
        "job_id": expect_error(ErrorCode.NOT_FOUND, lambda: repo.get_job("j_none")),
        "artifact_id": expect_error(ErrorCode.NOT_FOUND, lambda: repo.get_artifact("a_none")),
        "upload_id": expect_error(ErrorCode.NOT_FOUND, lambda: repo.get_upload("u_none")),
    }
    for key, error in expected.items():
        assert error.where[key].endswith("_none")
        assert error.remediation == ["确认标识拼写；列表接口可查到当前可用标识"]

    assert repo.find_project("p_none") is None
    assert repo.find_job("j_none") is None
    assert repo.find_artifact("a_none") is None
    assert repo.find_report("j_none") is None


def test_duplicate_active_name_is_rejected(repo: Repository) -> None:
    repo.add_project(project("p_1", "同名"))
    error = expect_error(
        ErrorCode.PROJECT_NAME_TAKEN, lambda: repo.add_project(project("p_2", "同名"))
    )
    assert error.where["existing_project_id"] == "p_1"


def test_archiving_frees_the_name(repo: Repository) -> None:
    repo.add_project(project("p_1", "同名"))
    repo.save_project(project("p_1", "同名", archived=True))
    assert repo.add_project(project("p_2", "同名")).name == "同名"


def test_renaming_onto_itself_is_allowed(repo: Repository) -> None:
    """改名到"它自己现在的名字"必须成功——否则界面上的保存按钮会偶尔无故报错。"""
    repo.add_project(project("p_1", "旧名"))
    assert repo.save_project(project("p_1", "旧名")).name == "旧名"


def test_saving_an_unknown_project_is_rejected(repo: Repository) -> None:
    expect_error(ErrorCode.NOT_FOUND, lambda: repo.save_project(project("p_ghost")))


def test_archived_projects_are_hidden_by_default(repo: Repository) -> None:
    repo.add_project(project("p_1", "在册"))
    repo.add_project(project("p_2", "已归档", archived=True))
    assert [item.project_id for item in repo.list_projects()] == ["p_1"]
    assert {item.project_id for item in repo.list_projects(include_archived=True)} == {
        "p_1",
        "p_2",
    }


def test_projects_are_listed_in_creation_order(repo: Repository) -> None:
    later = NOW + timedelta(minutes=1)
    repo.add_project(project("p_late", "晚", created_at=later, updated_at=later))
    repo.add_project(project("p_early", "早"))
    assert [item.project_id for item in repo.list_projects()] == ["p_early", "p_late"]


def test_identical_timestamps_break_ties_deterministically(repo: Repository) -> None:
    """同一时刻写入的一批记录，列表次序必须**可预期、且两种实现一致**。

    只按时刻排序时，平局的次序取决于"谁先被插进来"（内存版）或数据库的物理行序
    （落库版）——同一个界面在两种部署下给出不同顺序，而这条规则没有任何人写过。

    平局规则**按列表语义分两类**：

    * **作业队列按提交序**。队列的公平性是"先提交的先跑"，而这恰好不能用 ``job_id``
      兜底：它由 ``doc_key`` 派生，按它排等于按文档名字母序执行。故作业有专门的
      提交序号（落库版是 ``jobs.seq``，内存版是插入计数器）。
    * **展示型列表按标识升序**。项目 / 产物 / 上传件没有"提交序"这层语义，任取一个
      稳定的键即可；选标识是为了它跨方言、可重现。
    """
    # 作业刻意按"与标识相反"的次序提交：若实现用 job_id 兜底，就会排成 a 在前。
    repo.save_job(job("j_b"))
    repo.save_job(job("j_a"))
    for suffix in ("b", "a"):
        repo.add_project(project(f"p_{suffix}", f"项目{suffix}"))
        repo.add_artifact(artifact(f"a_{suffix}", "j_1"))
        repo.add_upload(upload(f"u_{suffix}"))

    assert [item.job_id for item in repo.list_jobs()] == ["j_b", "j_a"]
    assert [item.project_id for item in repo.list_projects()] == ["p_a", "p_b"]
    assert [item.artifact_id for item in repo.list_artifacts("j_1")] == ["a_a", "a_b"]
    assert [item.upload_id for item in repo.list_uploads()] == ["u_a", "u_b"]


def test_config_version_is_derived_by_the_repository(repo: Repository) -> None:
    """版本号由仓储自增，**不接受调用方指定**：并发的两次写入会拿到同一个号，
    而"回滚 = 以历史版本为内容再写一版"依赖它严格递增。"""
    first = repo.append_config(ConfigVersion("p_1", 999, config(), "t", NOW))
    second = repo.append_config(ConfigVersion("p_1", 999, config(), "t", NOW))
    assert (first.version, second.version) == (1, 2)


def test_config_history_is_append_only_and_per_project(repo: Repository) -> None:
    for _ in range(3):
        repo.append_config(ConfigVersion("p_1", 1, config(), "t", NOW))
    repo.append_config(ConfigVersion("p_2", 1, config("p_2"), "t", NOW))
    assert [entry.version for entry in repo.list_configs("p_1")] == [1, 2, 3]
    assert [entry.version for entry in repo.list_configs("p_2")] == [1]
    assert repo.get_config("p_1", 2) is not None
    assert repo.get_config("p_1", 4) is None


def test_latest_config_is_none_before_any_write(repo: Repository) -> None:
    assert repo.latest_config("p_1") is None


def test_latest_config_returns_the_newest_version(repo: Repository) -> None:
    repo.append_config(ConfigVersion("p_1", 1, config(author="old"), None, NOW))
    repo.append_config(ConfigVersion("p_1", 2, config(author="new"), None, NOW))
    loaded = repo.latest_config("p_1")
    assert loaded is not None and loaded.author == "new"


def test_config_reads_are_independent_of_the_stored_object(repo: Repository) -> None:
    """改动读出来的配置，不得影响库里那份（``protocols`` 的纪律 2）。"""
    repo.append_config(ConfigVersion("p_1", 1, config(author="原始"), "t", NOW))
    loaded = repo.latest_config("p_1")
    assert loaded is not None
    loaded.author = "被改了"
    again = repo.latest_config("p_1")
    assert again is not None and again.author == "原始"


def test_config_history_reads_are_independent_of_the_stored_object(repo: Repository) -> None:
    """``list_configs`` / ``get_config`` 与 ``latest_config`` 同一条纪律。

    三个读取口里当时只有 ``latest_config`` 返回深拷贝，另两个把库内对象直接递出去；
    而 ``ConfigVersion`` 只 frozen 了外壳，``config`` 仍是可变模型——于是"取历史版本
    看一圈再回滚"这条路径上，看一眼就能改写已落库的第 2 版。
    """
    repo.append_config(ConfigVersion("p_1", 1, config(author="原始"), "t", NOW))

    listed = repo.list_configs("p_1")
    assert listed and listed[0].config.author == "原始"
    listed[0].config.author = "从列表改的"

    fetched = repo.get_config("p_1", 1)
    assert fetched is not None
    fetched.config.author = "从单取改的"

    assert repo.list_configs("p_1")[0].config.author == "原始"
    latest = repo.latest_config("p_1")
    assert latest is not None and latest.author == "原始"


def test_job_reads_are_independent_of_the_stored_object(repo: Repository) -> None:
    repo.save_job(job())
    loaded = repo.get_job("j_1")
    loaded.child_job_ids.append("伪造的子作业")
    assert repo.get_job("j_1").child_job_ids == []


def test_save_job_persists_the_status_it_was_given(repo: Repository) -> None:
    """仓储**不判定**迁移：给什么状态就存什么状态。

    若某天有人在这里加了"顺手把 queued 改成 running"，合法迁移就有了第二个判定口径，
    而它与 ``JobStateMachine`` 迟早不一致。
    """
    repo.save_job(job("j_1", status="running"))
    assert repo.get_job("j_1").status == "running"


def test_job_filters(repo: Repository) -> None:
    repo.save_job(job("j_1", project_id="p_1", status="queued"))
    repo.save_job(job("j_2", project_id="p_1", status="succeeded"))
    repo.save_job(job("j_3", project_id="p_2", status="queued"))
    assert [item.job_id for item in repo.list_jobs(project_id="p_1")] == ["j_1", "j_2"]
    assert [item.job_id for item in repo.list_jobs(status="queued")] == ["j_1", "j_3"]
    assert [item.job_id for item in repo.list_jobs(project_id="p_2", status="queued")] == ["j_3"]


def test_stages_are_replaced_wholesale(repo: Repository) -> None:
    """整批替换：阶段集合对同一作业是固定的，"只 upsert 不删"会让上一轮多出来的那条
    一直留着，而它的 ``status`` 早已过时。"""
    repo.save_stages("j_1", [Stage(name="ingest", status="succeeded")])
    repo.save_stages("j_1", [Stage(name="ingest", status="succeeded"), Stage(name="compose")])
    assert [stage.name for stage in repo.get_stages("j_1")] == ["ingest", "compose"]
    assert repo.get_stages("j_unknown") == []


def test_artifact_round_trip_and_removal(repo: Repository) -> None:
    repo.add_artifact(artifact())
    assert repo.get_artifact("a_1").kind == "docx"
    assert [item.artifact_id for item in repo.list_artifacts("j_1")] == ["a_1"]
    assert repo.remove_artifact("a_1") is True
    assert repo.remove_artifact("a_1") is False, "重复移除必须报 False 而不是炸"
    assert repo.find_artifact("a_1") is None


def test_upload_round_trip_and_removal(repo: Repository) -> None:
    repo.add_upload(upload())
    assert repo.get_upload("u_1").receipt.filename == "t.docx"
    assert [item.upload_id for item in repo.list_uploads()] == ["u_1"]
    assert repo.remove_upload("u_1") is True
    assert repo.remove_upload("u_1") is False
    expect_error(ErrorCode.NOT_FOUND, lambda: repo.get_upload("u_1"))


def test_report_round_trip_and_overwrite(repo: Repository) -> None:
    first = Report(
        report_id="r_1", job_id="j_1", generated_at=NOW, findings=[], waivers=[], verdict="pass"
    )
    repo.put_report("j_1", first)
    loaded = repo.find_report("j_1")
    assert loaded is not None and loaded.verdict == "pass"
    repo.put_report("j_1", first.model_copy(update={"verdict": "warning"}))
    again = repo.find_report("j_1")
    assert again is not None and again.verdict == "warning", "同一作业至多一份报告"


def test_both_implementations_cover_the_aggregate_protocol(repo: Repository) -> None:
    """聚合面必须真的覆盖六个子协议（少一个，类型检查会在装配处才爆）。"""
    for name in (
        "add_project",
        "get_project",
        "find_project",
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
        assert callable(getattr(repo, name, None)), f"仓储缺少协议成员：{name}"


# ===================================================== 二、只属于落库版
def test_aware_moments_survive_the_round_trip(sqla: SqlAlchemyRepository) -> None:
    """SQLite 不保存 ``tzinfo``。落成 naive 会让 ``expires_at <= now()`` 直接抛异常
    （保留期回收在 SQLite 部署上变成"每次调用都 500"），故这一条必须钉住。"""
    sqla.add_project(project("p_1"))
    stored_project = sqla.get_project("p_1")
    assert stored_project.created_at == NOW
    assert stored_project.created_at.tzinfo is not None
    sqla.add_artifact(artifact())
    assert sqla.get_artifact("a_1").created_at.tzinfo is not None


def test_upload_moments_survive_the_round_trip(sqla: SqlAlchemyRepository) -> None:
    sqla.add_upload(upload())
    stored = sqla.get_upload("u_1")
    assert stored.created_at == NOW and stored.created_at.tzinfo is not None


def test_naive_moments_are_interpreted_as_utc(sqla: SqlAlchemyRepository) -> None:
    """naive 一律按 UTC 解释。原样存会让同一列混进两种含义的时刻，而"这条是本地时间
    还是 UTC"从数据本身再也看不出来。"""
    naive = datetime(2026, 9, 23, 9, 0, 0)
    sqla.add_project(project("p_naive", created_at=naive, updated_at=naive))
    assert sqla.get_project("p_naive").created_at == NOW


def test_removed_artifact_stops_being_listed(sqla: SqlAlchemyRepository) -> None:
    """清理回收后登记必须同步消失，否则下载接口会从 ``E_ARTIFACT_EXPIRED``
    （可操作：重新构建）退化成 ``E_NOT_FOUND``（可操作：标识写错了？）。"""
    sqla.add_artifact(artifact())
    assert len(sqla.list_artifacts("j_1")) == 1
    sqla.remove_artifact("a_1")
    assert sqla.list_artifacts("j_1") == []


def test_build_repository_chooses_the_implementation(sqla: SqlAlchemyRepository) -> None:
    assert isinstance(build_repository(None), MemoryRepository)
    assert isinstance(build_repository("sqlite:///:memory:"), SqlAlchemyRepository)


def test_in_memory_url_is_recognised() -> None:
    assert is_memory_url("sqlite:///:memory:")
    assert is_memory_url("sqlite://")
    assert not is_memory_url("sqlite:///./data/df.db")


def test_memory_sqlite_stays_on_one_connection() -> None:
    """内存库默认"一个连接一个库"：不锁连接，"建表用连接 A、写作业用连接 B"会得到
    ``no such table``——一个看起来像"迁移没跑"而实际是"连了另一个库"的错误。"""
    engine = create_engine_for("sqlite:///:memory:")
    create_schema(engine)
    repo = SqlAlchemyRepository(engine)
    repo.add_project(project("p_1"))
    assert repo.get_project("p_1").name == "项目甲"


def test_migration_schema_matches_orm_metadata(tmp_path: Path) -> None:
    """生产走 Alembic、测试走 ``Base.metadata``——两条路径必然有漂移风险，
    唯一的防漂移手段就是让它们在同一批断言下对齐。"""
    migrated = migrated_engine(tmp_path, "migrated.db")
    metadata = create_engine_for(database_url(tmp_path, "metadata.db"))
    create_schema(metadata)
    assert _schema_of(migrated) == _schema_of(metadata)


def test_programmatic_migrations_do_not_depend_on_the_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """服务与测试走的是 :func:`migration_config`，它必须把 ``script_location``
    钉成**绝对路径**。

    ``alembic.ini`` 里那一行是相对的，由 Alembic 按**进程 cwd** 解析；照抄它会让
    "从哪个目录启动服务"变成一个隐藏前置条件——在容器里没人能控制那件事。故这里
    换一个 cwd 再跑一次迁移，证明这条路径确实不依赖它。
    """
    config = migration_config(database_url(tmp_path, "abs.db"))
    assert Path(config.get_main_option("script_location")).is_absolute()
    monkeypatch.chdir(tmp_path)
    run_migrations(database_url(tmp_path, "elsewhere.db"))
    assert "jobs" in _schema_of(create_engine_for(database_url(tmp_path, "elsewhere.db")))


def _schema_of(engine: Engine) -> dict[str, object]:
    """按**反射**取 schema 形状（表 / 列 / 类型 / 可空 / 主键）。

    反射而不是读元数据对象：迁移建出来的表与 ORM 声明是两份独立来源，只有都从库里
    读回来比，才证明"迁移结果 == ORM 元数据"。``alembic_version`` 是 Alembic 自己的
    记账表，不在 ORM 元数据里，故排除。
    """
    inspector = inspect(engine)
    shape: dict[str, object] = {}
    for table in sorted(inspector.get_table_names()):
        if table == ALEMBIC_TABLE:
            continue
        columns = {
            column["name"]: (str(column["type"]), bool(column["nullable"]))
            for column in inspector.get_columns(table)
        }
        primary_key = tuple(inspector.get_pk_constraint(table)["constrained_columns"])
        shape[table] = (columns, primary_key)
    return shape


def test_orm_metadata_declares_every_expected_table() -> None:
    assert set(Base.metadata.tables) == {
        "projects",
        "config_versions",
        "jobs",
        "stages",
        "artifacts",
        "reports",
        "uploads",
        # 模板登记（P0-15 第 4 轮）：模板库的元数据落库，包本体落磁盘。
        "templates",
        # 档案库与事实源（P0-14 第 3 轮）：档案同 templates 是"带版本的资产"，
        # 本体在 profiles/<pid>/<ver>/；事实源是每项目一份的配置声明，无磁盘本体。
        "doc_type_profiles",
        "fact_sources",
        # 领域模型库（P0-15 第 8 轮）：主键只有 ``kind``——它**没有版本维度**，
        # 与 templates / doc_type_profiles 的 ``id + version`` 刻意不同。
        "domain_models",
    }


def test_data_survives_a_new_repository_instance(engine: Engine) -> None:
    """**落库的全部意义**：换一个仓储实例（等价于换一个进程）还能读到作业、
    阶段、产物与报告。内存版过不了这一条，这正是两者的分水岭。"""
    first = SqlAlchemyRepository(engine)
    first.add_project(project("p_1"))
    first.save_job(job("j_1"))
    first.save_stages("j_1", [Stage(name="ingest", status="succeeded")])
    first.add_artifact(artifact())
    first.put_report(
        "j_1",
        Report(report_id="r_1", job_id="j_1", generated_at=NOW, verdict="pass"),
    )
    first.add_profile(
        RegisteredProfile(
            profile_id="prof_1",
            version="1.0",
            doc_type="spec",
            title="规格说明书体例",
            profile_dir="/ws/profiles/prof_1/1.0",
            fingerprint="fp_1",
            registered_at=NOW,
        )
    )
    first.add_source(
        FactSourceRecord(
            project_id="p_1",
            config=SourceConfig(source_id="src_1", source_type="markdown", root="facts"),
            created_at=NOW,
            last_probed_at=NOW,
            last_probe_ok=True,
            item_count=3,
        )
    )

    second = SqlAlchemyRepository(engine)
    assert second.get_project("p_1").name == "项目甲"
    assert second.get_job("j_1").status == "queued"
    assert [stage.name for stage in second.get_stages("j_1")] == ["ingest"]
    assert [item.artifact_id for item in second.list_artifacts("j_1")] == ["a_1"]
    report = second.find_report("j_1")
    assert report is not None and report.verdict == "pass"
    profile = second.find_profile("prof_1", "1.0")
    assert profile is not None and profile.fingerprint == "fp_1"
    source = second.find_source("p_1", "src_1")
    # 来源的配置本体（pydantic）必须完整往返——`asdict` 不递归模型，漏了
    # `model_dump` 这一步写进去的就是一个 repr 字符串，读回来直接校验失败。
    assert source is not None and source.config.root == "facts"
    assert source.last_probe_ok is True


def test_build_end_to_end_on_a_migrated_database(tmp_path: Path) -> None:
    """端到端：落库仓储下"发起构建 → 跑完 → 产物登记落库"整条链路照常。

    上层（``Services`` / ``JobRunner``）**一行不改**——协议写对的回报就在这里。
    跑完再用**另一个**仓储实例读一次，证明结果真的落了库而不是留在内存里。

    ``render`` 必须显式给产物：默认阶段体一个文件都不写，产物登记这段代码就**根本没跑**，
    那样这条用例即便绿了也只证明了"空目录下不报错"。
    """
    engine = migrated_engine(tmp_path, "e2e.db")
    harness = build_harness(
        tmp_path / "ws",
        stages=build_registry(render={"artifacts": ("out.docx", "preview.png")}),
        repository=SqlAlchemyRepository(engine),
    )
    project_id = harness.new_project()
    orchestration_id = harness.build(project_id, doc_keys=["overview"])

    fresh = SqlAlchemyRepository(engine)
    orchestration = fresh.get_job(orchestration_id)
    assert orchestration.is_orchestration
    child = fresh.get_job(orchestration.child_job_ids[0])
    assert child.status == "succeeded"

    stored = {item.relative_path for item in fresh.list_artifacts(child.job_id)}
    assert stored == {f"{ARTIFACTS_DIRNAME}/out.docx", f"{ARTIFACTS_DIRNAME}/preview.png"}
    assert sorted(child.artifact_ids) == sorted(
        item.artifact_id for item in fresh.list_artifacts(child.job_id)
    )
    assert fresh.list_projects(), "项目也应当还在"


def test_uploads_record_keeps_purpose_and_filename_in_columns(
    sqla: SqlAlchemyRepository, engine: Engine
) -> None:
    """``filename`` / ``purpose`` 抽成列是为了审计与清理能按它们筛；
    这里直接用 SQL 查一次，证明它们真的在列里而不是只躺在 JSON 里。"""
    from sqlalchemy import text

    sqla.add_upload(upload("u_1"))
    sqla.add_upload(
        UploadRecord(
            upload_id="u_2",
            receipt=UploadReceipt(
                filename="p.json",
                suffix=".json",
                size_bytes=2,
                content_type="application/json",
                digest="e",
            ),
            stored_path="/var/uploads/u_2/p.json",
            created_at=NOW,
            purpose="profile",
        )
    )
    with engine.connect() as connection:
        rows = connection.execute(
            text("SELECT upload_id FROM uploads WHERE purpose = :p ORDER BY upload_id"),
            {"p": "profile"},
        ).scalars()
        assert list(rows) == ["u_2"]
