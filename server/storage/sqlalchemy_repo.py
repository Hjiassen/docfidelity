"""SQLAlchemy 仓储（P0-14 第 2 轮；DD-13.1 的 ``server/storage``）。

与 :class:`server.storage.MemoryRepository` 的**语义等价**是本文档的第一目标
----------------------------------------------------------------------------
换实现不该改变行为，包括那些"看起来只是细节"的行为：

* **读取返回独立对象**——由"存 JSON、读出重新 ``model_validate``"天然满足；
* **未找到抛 ``E_NOT_FOUND``**，且 ``where`` / ``remediation`` 与内存版逐字一致
  （错误文案是界面直接渲染的，两种仓储给出两种文案会让"换个部署，提示就变了"）；
* **未归档项目名唯一**——内存版会真的拦（用户连点两次"新建项目"），落库版也必须拦；
* **配置版本号由仓储派生**（同项目内自增），不接受调用方指定——否则两个并发写入会拿到
  同一个号，而"回滚 = 以历史版本为内容再写一版"依赖版本号严格递增。

为什么不用自增主键承载版本号
----------------------------
版本号是**项目内**的序号。做成全局自增会让"项目的第 3 版"与"全库第 17 条"混为一谈，
而 `list_configs` 的语义正是"这个项目的第 1..n 版"。

并发口径
--------
版本号在**同一个事务里**先查 max 再插入，靠 ``(project_id, version)`` 复合主键兜底：
真撞上时由数据库拒绝（而不是悄悄写上两条同号记录）。SQLite 是单写者，天然串行；
PostgreSQL 下这一对语句的窗口极小，且撞车只会变成一次可重试的失败，不会产生坏数据。
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import asdict
from datetime import datetime
from typing import Any, cast

from sqlalchemy import CursorResult, Delete, Engine, delete, func, select
from sqlalchemy.orm import Session, sessionmaker

from docfidelity.contracts.config import ProjectConfig
from docfidelity.contracts.domain import DomainModel
from docfidelity.contracts.errors import EngineError, ErrorCode
from docfidelity.contracts.fact import SourceConfig
from docfidelity.contracts.job import Artifact, Job, JobStatus, Stage
from docfidelity.contracts.qa import Report

from ..uploads import UploadReceipt
from .engine import session_factory_for
from .protocols import (
    ConfigVersion,
    FactSourceRecord,
    Project,
    RegisteredDomainModel,
    RegisteredProfile,
    RegisteredTemplate,
    UploadRecord,
)
from .tables import (
    ArtifactRow,
    ConfigVersionRow,
    DomainModelRow,
    FactSourceRow,
    JobRow,
    ProfileRow,
    ProjectRow,
    ReportRow,
    StageRow,
    TemplateRow,
    UploadRow,
)

__all__ = ["SqlAlchemyRepository"]


def _not_found(what: str, key: str, where_key: str) -> EngineError:
    """与 ``MemoryRepository._not_found`` **逐字一致**（文案是界面直接渲染的）。"""
    return EngineError(
        ErrorCode.NOT_FOUND,
        f"找不到{what}：{key}",
        where={where_key: key},
        remediation=["确认标识拼写；列表接口可查到当前可用标识"],
    )


class SqlAlchemyRepository:
    """落库仓储。构造即持有一个引擎；schema 由 Alembic 建（见 ``engine``）。"""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        self._session: sessionmaker[Session] = session_factory_for(engine)

    @property
    def engine(self) -> Engine:
        return self._engine

    # ------------------------------------------------------------ 内部工具
    @staticmethod
    def _assert_name_free(session: Session, name: str, *, exclude: str | None) -> None:
        """未归档项目内名称唯一。已归档的重名允许——归档就是"腾出这个名字"。"""
        statement = select(ProjectRow).where(
            ProjectRow.name == name, ProjectRow.archived.is_(False)
        )
        if exclude is not None:
            statement = statement.where(ProjectRow.project_id != exclude)
        existing = session.scalars(statement).first()
        if existing is not None:
            raise EngineError(
                ErrorCode.PROJECT_NAME_TAKEN,
                f"项目名已被占用：{name!r}",
                where={"name": name, "existing_project_id": existing.project_id},
                remediation=["换一个项目名", "或先归档重名的项目"],
            )

    @staticmethod
    def _deleted(session: Session, statement: Delete) -> bool:
        """执行 ``DELETE`` 并回答"是否真的删到了行"。

        ``Session.execute`` 的静态类型是 ``Result[Any]``（它也可能装着一批行），而 DML
        实际返回的是 ``CursorResult``——``rowcount`` 只在后者上。这个转换是**必要的、
        不是绕过检查**：本方法只接受 ``Delete``，故转换的前提由签名保证。
        """
        return bool(cast("CursorResult[Any]", session.execute(statement)).rowcount)

    @staticmethod
    def _project_of(row: ProjectRow) -> Project:
        return Project(
            project_id=row.project_id,
            name=row.name,
            created_at=row.created_at,
            updated_at=row.updated_at,
            archived=row.archived,
            demo=row.demo,
        )

    @staticmethod
    def _upload_payload(record: UploadRecord) -> str:
        return json.dumps(
            {
                "receipt": asdict(record.receipt),
                "stored_path": record.stored_path,
                "created_at": record.created_at.isoformat(),
            }
        )

    @staticmethod
    def _upload_of(row: UploadRow) -> UploadRecord:
        data = json.loads(row.payload)
        receipt = UploadReceipt(**data["receipt"])
        return UploadRecord(
            upload_id=row.upload_id,
            receipt=receipt,
            stored_path=str(data["stored_path"]),
            created_at=datetime.fromisoformat(data["created_at"]),
            purpose=row.purpose,
        )

    @staticmethod
    def _template_payload(entry: RegisteredTemplate) -> str:
        data = asdict(entry)
        data["registered_at"] = entry.registered_at.isoformat()
        return json.dumps(data)

    @staticmethod
    def _template_of(row: TemplateRow) -> RegisteredTemplate:
        data = json.loads(row.payload)
        data["registered_at"] = datetime.fromisoformat(data["registered_at"])
        # 抽取列优先于载荷：它们是**可查询的真值**，载荷只是整份记录的快照。两者不一致
        # 时以列为准，否则"改过废弃标记"会读回旧值——而那正是本表抽列的目的。
        data["template_id"] = row.template_id
        data["version"] = row.version
        data["doc_type"] = row.doc_type
        data["deprecated"] = row.deprecated
        return RegisteredTemplate(**data)

    @staticmethod
    def _profile_payload(entry: RegisteredProfile) -> str:
        data = asdict(entry)
        data["registered_at"] = entry.registered_at.isoformat()
        return json.dumps(data)

    @staticmethod
    def _profile_of(row: ProfileRow) -> RegisteredProfile:
        data = json.loads(row.payload)
        data["registered_at"] = datetime.fromisoformat(data["registered_at"])
        # 与 ``_template_of`` 同口径：抽取列是可查询的真值，载荷是快照，不一致时以列为准。
        data["profile_id"] = row.profile_id
        data["version"] = row.version
        data["doc_type"] = row.doc_type
        data["deprecated"] = row.deprecated
        return RegisteredProfile(**data)

    @staticmethod
    def _domain_model_payload(entry: RegisteredDomainModel) -> str:
        data = asdict(entry)
        # ``asdict`` 只递归 dataclass，而 ``model`` 是 pydantic 契约模型——不显式
        # ``model_dump`` 就会在 ``json.dumps`` 里炸，且报错离"忘了转声明"很远
        # （同 ``_source_payload`` 的注释）。
        data["model"] = entry.model.model_dump(mode="json")
        data["registered_at"] = entry.registered_at.isoformat()
        return json.dumps(data)

    @staticmethod
    def _domain_model_of(row: DomainModelRow) -> RegisteredDomainModel:
        data = json.loads(row.payload)
        data["model"] = DomainModel.model_validate(data["model"])
        data["registered_at"] = datetime.fromisoformat(data["registered_at"])
        # 与 ``_profile_of`` 同口径：抽取列是可查询的真值，载荷是快照，不一致时以列为准。
        data["deprecated"] = row.deprecated
        return RegisteredDomainModel(**data)

    @staticmethod
    def _source_payload(record: FactSourceRecord) -> str:
        data = asdict(record)
        # ``asdict`` 不会把 pydantic 模型变成可 JSON 化的结构（它只递归 dataclass），
        # 所以那份配置要显式 ``model_dump``。漏了这一步的报错发生在 ``json.dumps`` 里，
        # 而它离"忘了转配置"这个原因有十几层栈帧远。
        data["config"] = record.config.model_dump(mode="json")
        data["created_at"] = record.created_at.isoformat()
        data["last_probed_at"] = (
            None if record.last_probed_at is None else record.last_probed_at.isoformat()
        )
        return json.dumps(data)

    @staticmethod
    def _source_of(row: FactSourceRow) -> FactSourceRecord:
        data = json.loads(row.payload)
        data["config"] = SourceConfig.model_validate(data["config"])
        data["created_at"] = datetime.fromisoformat(data["created_at"])
        probed = data["last_probed_at"]
        data["last_probed_at"] = None if probed is None else datetime.fromisoformat(probed)
        data["project_id"] = row.project_id
        return FactSourceRecord(**data)

    # ---------------------------------------------------------------- 项目
    def add_project(self, project: Project) -> Project:
        with self._session.begin() as session:
            self._assert_name_free(session, project.name, exclude=None)
            session.add(
                ProjectRow(
                    project_id=project.project_id,
                    name=project.name,
                    created_at=project.created_at,
                    updated_at=project.updated_at,
                    archived=project.archived,
                    demo=project.demo,
                )
            )
        return project

    def get_project(self, project_id: str) -> Project:
        found = self.find_project(project_id)
        if found is None:
            raise _not_found("项目", project_id, "project_id")
        return found

    def find_project(self, project_id: str) -> Project | None:
        with self._session() as session:
            row = session.get(ProjectRow, project_id)
            return None if row is None else self._project_of(row)

    def list_projects(self, *, include_archived: bool = False) -> list[Project]:
        statement = select(ProjectRow)
        if not include_archived:
            statement = statement.where(ProjectRow.archived.is_(False))
        statement = statement.order_by(ProjectRow.created_at, ProjectRow.project_id)
        with self._session() as session:
            return [self._project_of(row) for row in session.scalars(statement)]

    def save_project(self, project: Project) -> Project:
        with self._session.begin() as session:
            row = session.get(ProjectRow, project.project_id)
            if row is None:
                raise _not_found("项目", project.project_id, "project_id")
            self._assert_name_free(session, project.name, exclude=project.project_id)
            row.name = project.name
            row.created_at = project.created_at
            row.updated_at = project.updated_at
            row.archived = project.archived
            row.demo = project.demo
        return project

    # ---------------------------------------------------------------- 配置
    def append_config(self, entry: ConfigVersion) -> ConfigVersion:
        with self._session.begin() as session:
            highest = session.scalar(
                select(func.max(ConfigVersionRow.version)).where(
                    ConfigVersionRow.project_id == entry.project_id
                )
            )
            version = (highest or 0) + 1
            config = entry.config.model_copy(deep=True, update={"version": version})
            session.add(
                ConfigVersionRow(
                    project_id=entry.project_id,
                    version=version,
                    author=entry.author,
                    created_at=entry.created_at,
                    payload=config.model_dump_json(),
                )
            )
        return ConfigVersion(
            project_id=entry.project_id,
            version=version,
            config=config,
            author=entry.author,
            created_at=entry.created_at,
        )

    def latest_config(self, project_id: str) -> ProjectConfig | None:
        statement = (
            select(ConfigVersionRow)
            .where(ConfigVersionRow.project_id == project_id)
            .order_by(ConfigVersionRow.version.desc())
            .limit(1)
        )
        with self._session() as session:
            row = session.scalars(statement).first()
            return (
                None
                if row is None
                else ProjectConfig.model_validate_json(row.payload).model_copy(
                    update={"version": row.version}
                )
            )

    def list_configs(self, project_id: str) -> list[ConfigVersion]:
        statement = (
            select(ConfigVersionRow)
            .where(ConfigVersionRow.project_id == project_id)
            .order_by(ConfigVersionRow.version)
        )
        with self._session() as session:
            return [self._config_of(row) for row in session.scalars(statement)]

    def get_config(self, project_id: str, version: int) -> ConfigVersion | None:
        with self._session() as session:
            row = session.get(ConfigVersionRow, (project_id, version))
            return None if row is None else self._config_of(row)

    @staticmethod
    def _config_of(row: ConfigVersionRow) -> ConfigVersion:
        return ConfigVersion(
            project_id=row.project_id,
            version=row.version,
            config=ProjectConfig.model_validate_json(row.payload).model_copy(
                update={"version": row.version}
            ),
            author=row.author,
            created_at=row.created_at,
        )

    # ---------------------------------------------------------------- 作业
    def save_job(self, job: Job) -> Job:
        payload = job.model_dump_json()
        with self._session.begin() as session:
            row = session.get(JobRow, job.job_id)
            if row is None:
                # 插入时分配提交序号（单调）。**更新路径不动它**：一个作业的一生只有
                # 一次"提交"，重跑 / 续跑都是同一个作业，重新排到队尾会让 FIFO 失去意义。
                highest = session.scalar(select(func.max(JobRow.seq)))
                session.add(
                    JobRow(
                        job_id=job.job_id,
                        project_id=job.project_id,
                        status=job.status,
                        kind=job.kind,
                        created_at=job.created_at,
                        seq=(highest or 0) + 1,
                        payload=payload,
                    )
                )
            else:
                row.project_id = job.project_id
                row.status = job.status
                row.kind = job.kind
                row.created_at = job.created_at
                row.payload = payload
        return job

    def get_job(self, job_id: str) -> Job:
        found = self.find_job(job_id)
        if found is None:
            raise _not_found("作业", job_id, "job_id")
        return found

    def find_job(self, job_id: str) -> Job | None:
        with self._session() as session:
            row = session.get(JobRow, job_id)
            return None if row is None else Job.model_validate_json(row.payload)

    def list_jobs(
        self,
        *,
        project_id: str | None = None,
        status: JobStatus | None = None,
        kind: str | None = None,
    ) -> list[Job]:
        statement = select(JobRow)
        if project_id is not None:
            statement = statement.where(JobRow.project_id == project_id)
        if status is not None:
            statement = statement.where(JobRow.status == status)
        if kind is not None:
            statement = statement.where(JobRow.kind == kind)
        # FIFO 靠 ``created_at``，平局靠 ``seq``（插入序号）。**不能用 ``job_id`` 兜底**：
        # 它由 ``doc_key`` 派生，按它排等于按文档名字母序执行，而队列的语义是提交序。
        statement = statement.order_by(JobRow.created_at, JobRow.seq)
        with self._session() as session:
            return [Job.model_validate_json(row.payload) for row in session.scalars(statement)]

    def save_stages(self, job_id: str, stages: Sequence[Stage]) -> None:
        """整批替换。先删后插：阶段集合对同一作业是固定的，"只 upsert 不删"会让
        上一轮多出来的那条一直留着，而它的 ``status`` 早已过时。"""
        with self._session.begin() as session:
            session.execute(delete(StageRow).where(StageRow.job_id == job_id))
            for ordinal, stage in enumerate(stages):
                session.add(
                    StageRow(job_id=job_id, ordinal=ordinal, payload=stage.model_dump_json())
                )

    def get_stages(self, job_id: str) -> list[Stage]:
        statement = select(StageRow).where(StageRow.job_id == job_id).order_by(StageRow.ordinal)
        with self._session() as session:
            return [Stage.model_validate_json(row.payload) for row in session.scalars(statement)]

    # ---------------------------------------------------------------- 产物
    def add_artifact(self, artifact: Artifact) -> Artifact:
        with self._session.begin() as session:
            row = session.get(ArtifactRow, artifact.artifact_id)
            if row is None:
                session.add(
                    ArtifactRow(
                        artifact_id=artifact.artifact_id,
                        job_id=artifact.job_id,
                        created_at=artifact.created_at,
                        payload=artifact.model_dump_json(),
                    )
                )
            else:  # pragma: no cover - 产物 id 由路径摘要派生，重名即同一个文件
                row.job_id = artifact.job_id
                row.created_at = artifact.created_at
                row.payload = artifact.model_dump_json()
        return artifact

    def get_artifact(self, artifact_id: str) -> Artifact:
        found = self.find_artifact(artifact_id)
        if found is None:
            raise _not_found("产物", artifact_id, "artifact_id")
        return found

    def find_artifact(self, artifact_id: str) -> Artifact | None:
        with self._session() as session:
            row = session.get(ArtifactRow, artifact_id)
            return None if row is None else Artifact.model_validate_json(row.payload)

    def list_artifacts(self, job_id: str) -> list[Artifact]:
        statement = (
            select(ArtifactRow)
            .where(ArtifactRow.job_id == job_id)
            .order_by(ArtifactRow.created_at, ArtifactRow.artifact_id)
        )
        with self._session() as session:
            return [Artifact.model_validate_json(row.payload) for row in session.scalars(statement)]

    def remove_artifact(self, artifact_id: str) -> bool:
        with self._session.begin() as session:
            return self._deleted(
                session, delete(ArtifactRow).where(ArtifactRow.artifact_id == artifact_id)
            )

    # ---------------------------------------------------------------- 报告
    def put_report(self, job_id: str, report: Report) -> None:
        payload = report.model_dump_json()
        with self._session.begin() as session:
            row = session.get(ReportRow, job_id)
            if row is None:
                session.add(ReportRow(job_id=job_id, payload=payload))
            else:
                row.payload = payload

    def find_report(self, job_id: str) -> Report | None:
        with self._session() as session:
            row = session.get(ReportRow, job_id)
            return None if row is None else Report.model_validate_json(row.payload)

    def list_reports(self) -> list[Report]:
        # 排序放在 Python 里而不是 ``ORDER BY``：平局键 ``report_id`` 在 ``payload``
        # 内（它是契约模型的一部分，没有独立列），而"列表次序"是契约语义，
        # 不该为了能排序而把契约字段摊成列（见 ``tables`` 的模块文档）。
        with self._session() as session:
            rows = list(session.scalars(select(ReportRow)))
        reports = [Report.model_validate_json(row.payload) for row in rows]
        return sorted(reports, key=lambda r: (r.generated_at, r.report_id))

    # -------------------------------------------------------------- 上传件
    def add_upload(self, record: UploadRecord) -> UploadRecord:
        with self._session.begin() as session:
            row = session.get(UploadRow, record.upload_id)
            if row is None:
                session.add(
                    UploadRow(
                        upload_id=record.upload_id,
                        filename=record.receipt.filename,
                        purpose=record.purpose,
                        created_at=record.created_at,
                        payload=self._upload_payload(record),
                    )
                )
            else:  # pragma: no cover - upload_id 每次上传新生成
                row.filename = record.receipt.filename
                row.purpose = record.purpose
                row.created_at = record.created_at
                row.payload = self._upload_payload(record)
        return record

    def get_upload(self, upload_id: str) -> UploadRecord:
        with self._session() as session:
            row = session.get(UploadRow, upload_id)
            if row is None:
                raise _not_found("上传件", upload_id, "upload_id")
            return self._upload_of(row)

    def list_uploads(self) -> list[UploadRecord]:
        statement = select(UploadRow).order_by(UploadRow.created_at, UploadRow.upload_id)
        with self._session() as session:
            return [self._upload_of(row) for row in session.scalars(statement)]

    def remove_upload(self, upload_id: str) -> bool:
        with self._session.begin() as session:
            return self._deleted(session, delete(UploadRow).where(UploadRow.upload_id == upload_id))

    # -------------------------------------------------------------- 模板库
    def add_template(self, entry: RegisteredTemplate) -> RegisteredTemplate:
        with self._session.begin() as session:
            row = session.get(TemplateRow, (entry.template_id, entry.version))
            if row is None:
                session.add(
                    TemplateRow(
                        template_id=entry.template_id,
                        version=entry.version,
                        doc_type=entry.doc_type,
                        registered_at=entry.registered_at,
                        deprecated=entry.deprecated,
                        payload=self._template_payload(entry),
                    )
                )
            else:
                # 同 ``(template_id, version)`` 再登记一次是"覆盖"的落库路径：调用方
                # （``Services.register_template``）已经决定了要覆盖，这里只负责写。
                row.doc_type = entry.doc_type
                row.registered_at = entry.registered_at
                row.deprecated = entry.deprecated
                row.payload = self._template_payload(entry)
        return entry

    def find_template(self, template_id: str, version: str) -> RegisteredTemplate | None:
        with self._session() as session:
            row = session.get(TemplateRow, (template_id, version))
            return None if row is None else self._template_of(row)

    def list_templates(self, *, template_id: str | None = None) -> list[RegisteredTemplate]:
        statement = select(TemplateRow).order_by(
            TemplateRow.template_id, TemplateRow.registered_at, TemplateRow.version
        )
        if template_id is not None:
            statement = statement.where(TemplateRow.template_id == template_id)
        with self._session() as session:
            return [self._template_of(row) for row in session.scalars(statement)]

    def save_template(self, entry: RegisteredTemplate) -> RegisteredTemplate:
        with self._session.begin() as session:
            row = session.get(TemplateRow, (entry.template_id, entry.version))
            if row is None:
                raise _not_found("模板包", entry.ref, "template_ref")
            row.doc_type = entry.doc_type
            row.registered_at = entry.registered_at
            row.deprecated = entry.deprecated
            row.payload = self._template_payload(entry)
        return entry

    # ------------------------------------------------------------ 档案库
    def add_profile(self, entry: RegisteredProfile) -> RegisteredProfile:
        with self._session.begin() as session:
            row = session.get(ProfileRow, (entry.profile_id, entry.version))
            if row is None:
                session.add(
                    ProfileRow(
                        profile_id=entry.profile_id,
                        version=entry.version,
                        doc_type=entry.doc_type,
                        registered_at=entry.registered_at,
                        deprecated=entry.deprecated,
                        payload=self._profile_payload(entry),
                    )
                )
            else:
                # 与 ``add_template`` 同口径：覆盖与否由调用方决定，这里只负责写。
                row.doc_type = entry.doc_type
                row.registered_at = entry.registered_at
                row.deprecated = entry.deprecated
                row.payload = self._profile_payload(entry)
        return entry

    def find_profile(self, profile_id: str, version: str) -> RegisteredProfile | None:
        with self._session() as session:
            row = session.get(ProfileRow, (profile_id, version))
            return None if row is None else self._profile_of(row)

    def list_profiles(
        self,
        *,
        profile_id: str | None = None,
        doc_type: str | None = None,
    ) -> list[RegisteredProfile]:
        statement = select(ProfileRow).order_by(
            ProfileRow.profile_id, ProfileRow.registered_at, ProfileRow.version
        )
        if profile_id is not None:
            statement = statement.where(ProfileRow.profile_id == profile_id)
        if doc_type is not None:
            statement = statement.where(ProfileRow.doc_type == doc_type)
        with self._session() as session:
            return [self._profile_of(row) for row in session.scalars(statement)]

    def save_profile(self, entry: RegisteredProfile) -> RegisteredProfile:
        with self._session.begin() as session:
            row = session.get(ProfileRow, (entry.profile_id, entry.version))
            if row is None:
                raise _not_found("档案", entry.ref, "profile_ref")
            row.doc_type = entry.doc_type
            row.registered_at = entry.registered_at
            row.deprecated = entry.deprecated
            row.payload = self._profile_payload(entry)
        return entry

    # ---------------------------------------------------------- 领域模型库
    def add_domain_model(self, entry: RegisteredDomainModel) -> RegisteredDomainModel:
        with self._session.begin() as session:
            row = session.get(DomainModelRow, entry.kind)
            if row is None:
                session.add(
                    DomainModelRow(
                        kind=entry.kind,
                        registered_at=entry.registered_at,
                        deprecated=entry.deprecated,
                        payload=self._domain_model_payload(entry),
                    )
                )
            else:
                # 与 ``add_profile`` 同口径：覆盖与否由调用方决定，这里只负责写。
                row.registered_at = entry.registered_at
                row.deprecated = entry.deprecated
                row.payload = self._domain_model_payload(entry)
        return entry

    def find_domain_model(self, kind: str) -> RegisteredDomainModel | None:
        with self._session() as session:
            row = session.get(DomainModelRow, kind)
            return None if row is None else self._domain_model_of(row)

    def list_domain_models(self) -> list[RegisteredDomainModel]:
        statement = select(DomainModelRow).order_by(
            DomainModelRow.registered_at, DomainModelRow.kind
        )
        with self._session() as session:
            return [self._domain_model_of(row) for row in session.scalars(statement)]

    def save_domain_model(self, entry: RegisteredDomainModel) -> RegisteredDomainModel:
        with self._session.begin() as session:
            row = session.get(DomainModelRow, entry.kind)
            if row is None:
                raise _not_found("领域模型", entry.kind, "kind")
            row.registered_at = entry.registered_at
            row.deprecated = entry.deprecated
            row.payload = self._domain_model_payload(entry)
        return entry

    # -------------------------------------------------------------- 事实源
    def add_source(self, record: FactSourceRecord) -> FactSourceRecord:
        with self._session.begin() as session:
            row = session.get(FactSourceRow, (record.project_id, record.source_id))
            if row is None:
                session.add(
                    FactSourceRow(
                        project_id=record.project_id,
                        source_id=record.source_id,
                        connector=record.source_type,
                        created_at=record.created_at,
                        last_probed_at=record.last_probed_at,
                        payload=self._source_payload(record),
                    )
                )
            else:
                row.connector = record.source_type
                row.last_probed_at = record.last_probed_at
                row.payload = self._source_payload(record)
        return record

    def find_source(self, project_id: str, source_id: str) -> FactSourceRecord | None:
        with self._session() as session:
            row = session.get(FactSourceRow, (project_id, source_id))
            return None if row is None else self._source_of(row)

    def list_sources(self, project_id: str) -> list[FactSourceRecord]:
        statement = (
            select(FactSourceRow)
            .where(FactSourceRow.project_id == project_id)
            .order_by(FactSourceRow.created_at, FactSourceRow.source_id)
        )
        with self._session() as session:
            return [self._source_of(row) for row in session.scalars(statement)]

    def save_source(self, record: FactSourceRecord) -> FactSourceRecord:
        with self._session.begin() as session:
            row = session.get(FactSourceRow, (record.project_id, record.source_id))
            if row is None:
                raise _not_found("事实源", record.source_id, "source_id")
            row.connector = record.source_type
            row.last_probed_at = record.last_probed_at
            row.payload = self._source_payload(record)
        return record

    def remove_source(self, project_id: str, source_id: str) -> bool:
        with self._session.begin() as session:
            return self._deleted(
                session,
                delete(FactSourceRow).where(
                    FactSourceRow.project_id == project_id,
                    FactSourceRow.source_id == source_id,
                ),
            )
