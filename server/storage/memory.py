"""内存仓储（本轮实现；SQLAlchemy + Alembic 落 P0-14 第 2 轮）。

语义上它与将来的落库实现**完全等价**——包括"读取返回副本"与"未找到抛
``E_NOT_FOUND``"。这一点是刻意的：如果内存版比落库版宽松，那么"测试全绿、上线就炸"，
而且炸在事务与唯一约束上，最不好查。

唯一性约束目前只有一条：**未归档项目的名称唯一**。它会真的被触发（用户连点两次
"新建项目"），所以内存版也必须拦——不能因为"只是内存版"就放过。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace

from docfidelity.contracts.config import ProjectConfig
from docfidelity.contracts.errors import EngineError, ErrorCode
from docfidelity.contracts.job import Artifact, Job, JobStatus, Stage
from docfidelity.contracts.qa import Report

from .protocols import (
    ConfigVersion,
    FactSourceRecord,
    Project,
    RegisteredDomainModel,
    RegisteredProfile,
    RegisteredTemplate,
    UploadRecord,
)


class MemoryRepository:
    """进程内仓储。``default`` 构造即空库。"""

    def __init__(self) -> None:
        self._projects: dict[str, Project] = {}
        self._configs: dict[str, list[ConfigVersion]] = {}
        self._jobs: dict[str, Job] = {}
        #: 作业的**提交序号**。FIFO 靠 ``created_at``，而一次多文档构建会让若干子作业
        #: 拿到同一个时刻（注入时钟、或时钟粒度粗于提交间隔）——那时唯一的平局键不能是
        #: ``job_id``（它由 ``doc_key`` 派生，按它排等于按文档名字母序执行）。落库版为
        #: 此有一列 ``jobs.seq``，内存版用这个计数器对齐。
        self._job_order: dict[str, int] = {}
        self._next_job_seq = 0
        self._stages: dict[str, list[Stage]] = {}
        self._artifacts: dict[str, Artifact] = {}
        self._reports: dict[str, Report] = {}
        self._uploads: dict[str, UploadRecord] = {}
        self._templates: dict[tuple[str, str], RegisteredTemplate] = {}
        self._profiles: dict[tuple[str, str], RegisteredProfile] = {}
        #: 键是 ``kind``——领域模型没有版本维度（见 ``protocols.RegisteredDomainModel``）。
        self._domain_models: dict[str, RegisteredDomainModel] = {}
        self._sources: dict[tuple[str, str], FactSourceRecord] = {}

    # ------------------------------------------------------------ 内部工具
    @staticmethod
    def _not_found(what: str, key: str, where_key: str) -> EngineError:
        return EngineError(
            ErrorCode.NOT_FOUND,
            f"找不到{what}：{key}",
            where={where_key: key},
            remediation=["确认标识拼写；列表接口可查到当前可用标识"],
        )

    def _assert_name_free(self, name: str, *, exclude: str | None) -> None:
        for project in self._projects.values():
            if project.archived or project.project_id == exclude:
                continue
            if project.name == name:
                raise EngineError(
                    ErrorCode.PROJECT_NAME_TAKEN,
                    f"项目名已被占用：{name!r}",
                    where={"name": name, "existing_project_id": project.project_id},
                    remediation=["换一个项目名", "或先归档重名的项目"],
                )

    # ---------------------------------------------------------------- 项目
    def add_project(self, project: Project) -> Project:
        self._assert_name_free(project.name, exclude=None)
        self._projects[project.project_id] = project
        return project

    def get_project(self, project_id: str) -> Project:
        found = self.find_project(project_id)
        if found is None:
            raise self._not_found("项目", project_id, "project_id")
        return found

    def find_project(self, project_id: str) -> Project | None:
        return self._projects.get(project_id)

    def list_projects(self, *, include_archived: bool = False) -> list[Project]:
        items = [p for p in self._projects.values() if include_archived or not p.archived]
        # 平局键（``project_id``）不是装饰：同一批创建的项目时间戳相同，只按时刻排的
        # 次序取决于字典插入顺序，而落库版是 ``ORDER BY created_at, project_id``。
        # 两种实现的列表次序不一致 = 界面换个部署就换一个顺序。
        return sorted(items, key=lambda item: (item.created_at, item.project_id))

    def save_project(self, project: Project) -> Project:
        if project.project_id not in self._projects:
            raise self._not_found("项目", project.project_id, "project_id")
        self._assert_name_free(project.name, exclude=project.project_id)
        self._projects[project.project_id] = project
        return project

    # ---------------------------------------------------------------- 配置
    def append_config(self, entry: ConfigVersion) -> ConfigVersion:
        history = self._configs.setdefault(entry.project_id, [])
        # 版本号由仓储派生（自增），不接受调用方指定——否则两个并发写入会拿到同一个号，
        # 而"回滚 = 以历史版本为内容再写一版"依赖版本号严格递增。
        version = ConfigVersion(
            project_id=entry.project_id,
            version=len(history) + 1,
            config=entry.config.model_copy(deep=True, update={"version": len(history) + 1}),
            author=entry.author,
            created_at=entry.created_at,
        )
        history.append(version)
        return version

    def latest_config(self, project_id: str) -> ProjectConfig | None:
        history = self._configs.get(project_id)
        if not history:
            return None
        return history[-1].config.model_copy(deep=True, update={"version": history[-1].version})

    def list_configs(self, project_id: str) -> list[ConfigVersion]:
        return [self._copy(entry) for entry in self._configs.get(project_id, ())]

    def get_config(self, project_id: str, version: int) -> ConfigVersion | None:
        for entry in self._configs.get(project_id, ()):
            if entry.version == version:
                return self._copy(entry)
        return None

    @staticmethod
    def _copy(entry: ConfigVersion) -> ConfigVersion:
        """``ConfigVersion`` 自身 frozen，但 ``config`` 是可变模型——**共享它等于共享库内对象**：
        调用方随手改一下读回来的配置，"已落库的第 2 版"就被改写了。落库版每次都从 JSON
        重新反序列化，天然是新的；内存版必须显式深拷贝才谈得上等价。"""
        return ConfigVersion(
            project_id=entry.project_id,
            version=entry.version,
            config=entry.config.model_copy(deep=True, update={"version": entry.version}),
            author=entry.author,
            created_at=entry.created_at,
        )

    # ---------------------------------------------------------------- 作业
    def save_job(self, job: Job) -> Job:
        if job.job_id not in self._jobs:
            # 序号只分配一次：一个作业的一生只有一次"提交"，重跑 / 续跑是同一个作业，
            # 重新排到队尾会让 FIFO 失去意义（与落库版的更新路径一致）。
            self._next_job_seq += 1
            self._job_order[job.job_id] = self._next_job_seq
        self._jobs[job.job_id] = job.model_copy(deep=True)
        return job

    def get_job(self, job_id: str) -> Job:
        found = self.find_job(job_id)
        if found is None:
            raise self._not_found("作业", job_id, "job_id")
        return found

    def find_job(self, job_id: str) -> Job | None:
        stored = self._jobs.get(job_id)
        return stored.model_copy(deep=True) if stored is not None else None

    def list_jobs(
        self,
        *,
        project_id: str | None = None,
        status: JobStatus | None = None,
        kind: str | None = None,
    ) -> list[Job]:
        items = [
            job
            for job in self._jobs.values()
            if (project_id is None or job.project_id == project_id)
            and (status is None or job.status == status)
            and (kind is None or job.kind == kind)
        ]
        # 平局按**提交序号**（不是 ``job_id``）：见 ``__init__`` 里 ``_job_order`` 的说明。
        return sorted(
            (job.model_copy(deep=True) for job in items),
            key=lambda j: (j.created_at, self._job_order.get(j.job_id, 0)),
        )

    def save_stages(self, job_id: str, stages: Sequence[Stage]) -> None:
        self._stages[job_id] = [stage.model_copy(deep=True) for stage in stages]

    def get_stages(self, job_id: str) -> list[Stage]:
        return [stage.model_copy(deep=True) for stage in self._stages.get(job_id, ())]

    # ---------------------------------------------------------------- 产物
    def add_artifact(self, artifact: Artifact) -> Artifact:
        self._artifacts[artifact.artifact_id] = artifact.model_copy(deep=True)
        return artifact

    def get_artifact(self, artifact_id: str) -> Artifact:
        found = self.find_artifact(artifact_id)
        if found is None:
            raise self._not_found("产物", artifact_id, "artifact_id")
        return found

    def find_artifact(self, artifact_id: str) -> Artifact | None:
        stored = self._artifacts.get(artifact_id)
        return stored.model_copy(deep=True) if stored is not None else None

    def list_artifacts(self, job_id: str) -> list[Artifact]:
        items = [item for item in self._artifacts.values() if item.job_id == job_id]
        return sorted(
            (item.model_copy(deep=True) for item in items),
            key=lambda a: (a.created_at, a.artifact_id),
        )

    def remove_artifact(self, artifact_id: str) -> bool:
        return self._artifacts.pop(artifact_id, None) is not None

    # ---------------------------------------------------------------- 报告
    def put_report(self, job_id: str, report: Report) -> None:
        self._reports[job_id] = report.model_copy(deep=True)

    def find_report(self, job_id: str) -> Report | None:
        stored = self._reports.get(job_id)
        return stored.model_copy(deep=True) if stored is not None else None

    def list_reports(self) -> list[Report]:
        # 平局键取 ``report_id``（与落库版同一个键）；报告标识由内容 + 时刻派生，
        # 故"同一时刻的两份报告"在两种实现下给出同一次序。
        ordered = sorted(self._reports.values(), key=lambda r: (r.generated_at, r.report_id))
        return [report.model_copy(deep=True) for report in ordered]

    # -------------------------------------------------------------- 上传件
    def add_upload(self, record: UploadRecord) -> UploadRecord:
        self._uploads[record.upload_id] = record
        return record

    def get_upload(self, upload_id: str) -> UploadRecord:
        found = self._uploads.get(upload_id)
        if found is None:
            raise self._not_found("上传件", upload_id, "upload_id")
        return found

    def list_uploads(self) -> list[UploadRecord]:
        return sorted(self._uploads.values(), key=lambda item: (item.created_at, item.upload_id))

    def remove_upload(self, upload_id: str) -> bool:
        return self._uploads.pop(upload_id, None) is not None

    # -------------------------------------------------------------- 模板库
    def add_template(self, entry: RegisteredTemplate) -> RegisteredTemplate:
        self._templates[(entry.template_id, entry.version)] = entry
        return entry

    def find_template(self, template_id: str, version: str) -> RegisteredTemplate | None:
        return self._templates.get((template_id, version))

    def list_templates(self, *, template_id: str | None = None) -> list[RegisteredTemplate]:
        items = [
            entry
            for entry in self._templates.values()
            if template_id is None or entry.template_id == template_id
        ]
        # 平局键取 ``version``（与落库版的 ``ORDER BY`` 逐字对应）：同模板的两个版本可能
        # 在同一时刻登记（脚本批量导入），只按时刻排的次序取决于插入顺序。
        return sorted(items, key=lambda item: (item.template_id, item.registered_at, item.version))

    def save_template(self, entry: RegisteredTemplate) -> RegisteredTemplate:
        key = (entry.template_id, entry.version)
        if key not in self._templates:
            raise self._not_found("模板包", entry.ref, "template_ref")
        self._templates[key] = entry
        return entry

    # ------------------------------------------------------------ 档案库
    def add_profile(self, entry: RegisteredProfile) -> RegisteredProfile:
        self._profiles[(entry.profile_id, entry.version)] = entry
        return entry

    def find_profile(self, profile_id: str, version: str) -> RegisteredProfile | None:
        return self._profiles.get((profile_id, version))

    def list_profiles(
        self,
        *,
        profile_id: str | None = None,
        doc_type: str | None = None,
    ) -> list[RegisteredProfile]:
        items = [
            entry
            for entry in self._profiles.values()
            if (profile_id is None or entry.profile_id == profile_id)
            and (doc_type is None or entry.doc_type == doc_type)
        ]
        # 排序键与落库版逐字对应（含 ``version`` 这个平局键），见协议的说明。
        return sorted(items, key=lambda item: (item.profile_id, item.registered_at, item.version))

    def save_profile(self, entry: RegisteredProfile) -> RegisteredProfile:
        key = (entry.profile_id, entry.version)
        if key not in self._profiles:
            raise self._not_found("档案", entry.ref, "profile_ref")
        self._profiles[key] = entry
        return entry

    # ---------------------------------------------------------- 领域模型库
    def add_domain_model(self, entry: RegisteredDomainModel) -> RegisteredDomainModel:
        self._domain_models[entry.kind] = self._copy_domain_model(entry)
        return entry

    def find_domain_model(self, kind: str) -> RegisteredDomainModel | None:
        stored = self._domain_models.get(kind)
        return None if stored is None else self._copy_domain_model(stored)

    def list_domain_models(self) -> list[RegisteredDomainModel]:
        # 平局键取 ``kind``（与落库版的 ``ORDER BY`` 逐字对应）：脚本批量登记会让若干
        # 声明拿到同一个时刻，只按时刻排的次序取决于插入顺序。
        ordered = sorted(
            self._domain_models.values(), key=lambda item: (item.registered_at, item.kind)
        )
        return [self._copy_domain_model(item) for item in ordered]

    def save_domain_model(self, entry: RegisteredDomainModel) -> RegisteredDomainModel:
        if entry.kind not in self._domain_models:
            raise self._not_found("领域模型", entry.kind, "kind")
        self._domain_models[entry.kind] = self._copy_domain_model(entry)
        return entry

    @staticmethod
    def _copy_domain_model(entry: RegisteredDomainModel) -> RegisteredDomainModel:
        """``RegisteredDomainModel`` 是 frozen 的，但 ``model`` 是可变契约模型——
        共享它等于共享库内对象（同 ``_copy`` 的理由）。落库版每次从 JSON 反序列化，
        天然是新的；内存版必须显式深拷贝才谈得上等价。"""
        return replace(entry, model=entry.model.model_copy(deep=True))

    # -------------------------------------------------------------- 事实源
    def add_source(self, record: FactSourceRecord) -> FactSourceRecord:
        self._sources[(record.project_id, record.source_id)] = record
        return record

    def find_source(self, project_id: str, source_id: str) -> FactSourceRecord | None:
        return self._sources.get((project_id, source_id))

    def list_sources(self, project_id: str) -> list[FactSourceRecord]:
        items = [item for item in self._sources.values() if item.project_id == project_id]
        return sorted(items, key=lambda item: (item.created_at, item.source_id))

    def save_source(self, record: FactSourceRecord) -> FactSourceRecord:
        key = (record.project_id, record.source_id)
        if key not in self._sources:
            raise self._not_found("事实源", record.source_id, "source_id")
        self._sources[key] = record
        return record

    def remove_source(self, project_id: str, source_id: str) -> bool:
        return self._sources.pop((project_id, source_id), None) is not None
