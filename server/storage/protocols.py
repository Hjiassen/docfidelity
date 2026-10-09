"""仓储协议（DD-13.1 的 ``server/storage``）。

本轮只落**协议 + 内存实现**：SQLAlchemy 模型 / DAO / Alembic 迁移放 P0-14 第 2 轮
（见提交正文与 ``.workbuddy/memory`` 的登记）。这样做的理由不是图省事——协议一旦写对，
换实现是纯替换；而先把表结构定型，反而会因为端点还没试完而反复改表。

两条纪律写在这里，因为它们是**跨实现**的：

1. **本层不判定状态迁移**。:meth:`JobRepository.save_job` 是持久化原语；把作业从
   ``queued`` 变成 ``running`` 这件事只能由 :class:`docfidelity.pipeline.JobStateMachine`
   算好之后再落盘（DD-13.1 / ``contracts.job`` 的硬约束 1）。仓储若自己"顺手改一下
   status"，合法迁移就有了第二个判定口径。
2. **读取返回副本**。仓储返回的对象可以被调用方随意改而不影响已存数据——否则
   "改了一个从仓储取出来的作业"会静默改写"库里"的作业，这类 bug 只在并发下现形。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from docfidelity.contracts.config import ProjectConfig
from docfidelity.contracts.domain import DomainModel
from docfidelity.contracts.fact import SourceConfig
from docfidelity.contracts.job import Artifact, Job, JobStatus, Stage
from docfidelity.contracts.qa import Report

from ..uploads import UploadReceipt

__all__ = [
    "ConfigVersion",
    "DomainModelRepository",
    "FactSourceRecord",
    "ProfileRepository",
    "Project",
    "RegisteredDomainModel",
    "RegisteredProfile",
    "RegisteredTemplate",
    "Repository",
    "SourceRepository",
    "UploadRecord",
]


@dataclass(frozen=True)
class Project:
    """项目。``name`` 在未归档项目内唯一（``E_PROJECT_NAME_TAKEN``）。"""

    project_id: str
    name: str
    created_at: datetime
    updated_at: datetime
    archived: bool = False
    #: 是否为演示项目（``POST /projects/{pid}/demo-init`` 建的）。用于界面标注。
    demo: bool = False


@dataclass(frozen=True)
class ConfigVersion:
    """一次配置写入的留痕（DD-13.6：写入不覆盖历史）。"""

    project_id: str
    version: int
    config: ProjectConfig
    author: str | None
    created_at: datetime


@dataclass(frozen=True)
class UploadRecord:
    """一次通过校验的上传件登记。"""

    upload_id: str
    receipt: UploadReceipt
    stored_path: str
    created_at: datetime
    #: 用途分类：模板 / 档案包 / 对照件。留痕用，不参与判定。
    purpose: str = "template"


class ProjectRepository(Protocol):
    """项目仓储。"""

    def add_project(self, project: Project) -> Project: ...

    def get_project(self, project_id: str) -> Project: ...

    def find_project(self, project_id: str) -> Project | None: ...

    def list_projects(self, *, include_archived: bool = False) -> list[Project]: ...

    def save_project(self, project: Project) -> Project:
        """整体写回（改名 / 归档都走这里）。名称唯一性由实现校验。"""
        ...


class ConfigRepository(Protocol):
    """项目配置仓储（版本留痕）。"""

    def append_config(self, entry: ConfigVersion) -> ConfigVersion: ...

    def latest_config(self, project_id: str) -> ProjectConfig | None: ...

    def list_configs(self, project_id: str) -> list[ConfigVersion]: ...

    def get_config(self, project_id: str, version: int) -> ConfigVersion | None: ...


class JobRepository(Protocol):
    """作业与阶段仓储。"""

    def save_job(self, job: Job) -> Job:
        """写回作业。**调用方必须已用 JobStateMachine 算好状态**（见模块文档）。"""
        ...

    def get_job(self, job_id: str) -> Job: ...

    def find_job(self, job_id: str) -> Job | None: ...

    def list_jobs(
        self,
        *,
        project_id: str | None = None,
        status: JobStatus | None = None,
        kind: str | None = None,
    ) -> list[Job]: ...

    def save_stages(self, job_id: str, stages: Sequence[Stage]) -> None: ...

    def get_stages(self, job_id: str) -> list[Stage]: ...


class ArtifactRepository(Protocol):
    """产物仓储。"""

    def add_artifact(self, artifact: Artifact) -> Artifact: ...

    def get_artifact(self, artifact_id: str) -> Artifact: ...

    def find_artifact(self, artifact_id: str) -> Artifact | None: ...

    def list_artifacts(self, job_id: str) -> list[Artifact]: ...

    def remove_artifact(self, artifact_id: str) -> bool:
        """移除产物登记（清理回收后调用）。返回是否真的存在过。

        没有这一条，"清理"删掉文件后登记还在，下载接口会从 ``E_ARTIFACT_EXPIRED``
        （可操作：重新构建）退化成 ``E_NOT_FOUND``（可操作：标识写错了？）——
        把一次正常的保留期回收，报成一次疑似用户错误。
        """
        ...


class ReportRepository(Protocol):
    """门禁报告仓储（DD-13.2 的 ``GET /jobs/{jid}/report``）。"""

    def put_report(self, job_id: str, report: Report) -> None: ...

    def find_report(self, job_id: str) -> Report | None: ...

    def list_reports(self) -> list[Report]:
        """全部报告（按 ``generated_at`` + ``report_id`` 排序）。

        裁决端点需要它：``finding_id`` 是 ``<报告标识>@<序号>``（``qa`` 产出报告时
        赋值），而报告只按 ``job_id`` 索引——没有这一条，用例层就只能自己遍历作业
        逐个去试，那等于把"报告存在哪"这份知识复制进服务层。顺序与 :meth:`find_report`
        无关，只保证**两次调用给出同一次序**（两种实现口径一致）。
        """
        ...


@dataclass(frozen=True)
class RegisteredTemplate:
    """一个**已登记进模板库**的模板包版本（P05 第 5 步的落点）。

    身份是 ``(template_id, version)``，不是某个自增序号：模板引用在项目配置里写作
    ``template_ref = "<template_id>@<version>"``（``DocumentEntry.template_ref``），
    库里若另有一个内部 id，"界面看到的引用"与"库里存的键"就会长期对不上。

    ``package_dir`` 存**绝对**路径（与 :attr:`UploadRecord.stored_path` 同处理）：
    ``ensure_within`` 接受成形的路径，喂相对片段会被按工作目录解析而误判越界。
    """

    template_id: str
    version: str
    package_hash: str
    source_hash: str
    package_dir: str
    registered_at: datetime
    doc_type: str | None = None
    source_filename: str | None = None
    notes: str | None = None
    registered_by: str | None = None
    deprecated: bool = False
    deprecated_reason: str | None = None
    #: 谁办的废弃。**留痕**，不参与任何判定（同 ``Job.initiated_by``）。
    deprecated_by: str | None = None

    @property
    def ref(self) -> str:
        return f"{self.template_id}@{self.version}"

    @property
    def parsed(self) -> bool:
        """是否已解析出契约。

        **由 ``package_hash`` 推出来**，不另存一个标记：未解析的登记（DD-7.1 的
        "仅登记原件、稍后重试"）在 ``_register_into`` 里只写 ``source_hash``，
        ``package_hash`` 留空。再存一个布尔量就是同一件事的第二份记录，
        而两份记录迟早不一致——表现是"库说已解析、读包却缺文件"。
        """
        return bool(self.package_hash)


class TemplateRepository(Protocol):
    """模板库仓储（登记元数据；包本体在磁盘上，见 ``server/templates.py``）。"""

    def add_template(self, entry: RegisteredTemplate) -> RegisteredTemplate: ...

    def find_template(self, template_id: str, version: str) -> RegisteredTemplate | None: ...

    def list_templates(self, *, template_id: str | None = None) -> list[RegisteredTemplate]:
        """模板包列表，按 ``(template_id, registered_at, version)`` 升序。

        平局键取 ``version`` 而不是"插入顺序"：同一模板的两个版本可能在**同一时刻**
        登记（脚本批量导入），只按时刻排的次序取决于插入顺序，而落库版与内存版会给出
        两个次序——界面换个部署就换一个版本排列。
        """
        ...

    def save_template(self, entry: RegisteredTemplate) -> RegisteredTemplate:
        """整体写回（登记后的改动用它：废弃 / 补备注）。不存在则抛 ``E_NOT_FOUND``。"""
        ...


class UploadRepository(Protocol):
    """上传件仓储。"""

    def add_upload(self, record: UploadRecord) -> UploadRecord: ...

    def get_upload(self, upload_id: str) -> UploadRecord: ...

    def list_uploads(self) -> list[UploadRecord]: ...

    def remove_upload(self, upload_id: str) -> bool:
        """移除上传件登记（清理回收后调用）。"""
        ...


@dataclass(frozen=True)
class RegisteredProfile:
    """一个**已登记**的文档类型档案版本（``POST /profiles`` 的落点）。

    与 :class:`RegisteredTemplate` 同构：身份是 ``(profile_id, version)``，本体在磁盘
    上的目录里，这里只放"用来列、用来筛、用来排序"的那几个字段。

    ``fingerprint`` 存档案指纹的**整体摘要**（``pipeline.fingerprint_profile`` 产出）。
    存摘要而不是"算了没算"的标记，是因为 FR-M2-26 的判定要拿它跟历史产出比：
    产出当时记的指纹与现在的指纹不同，才说明"这份文档该重跑了"。
    """

    profile_id: str
    version: str
    doc_type: str
    title: str
    profile_dir: str
    fingerprint: str
    registered_at: datetime
    registered_by: str | None = None
    deprecated: bool = False
    deprecated_reason: str | None = None

    @property
    def ref(self) -> str:
        return f"{self.profile_id}@{self.version}"


class ProfileRepository(Protocol):
    """档案仓储（登记元数据；档案本体在磁盘上，见 ``server/profiles.py``）。"""

    def add_profile(self, entry: RegisteredProfile) -> RegisteredProfile:
        """登记一个版本（**upsert 原语**）。

        与 :meth:`TemplateRepository.add_template` 同口径：同 ``(profile_id, version)``
        再写一次就是覆盖。**策略不在这里判**——"重复登记是拒绝还是覆盖"由用例层
        （``Services.register_profile``）决定，仓储只负责写；仓储若自己判，合法策略就
        有了第二个判定口径（本模块纪律 1 的同一条理由）。
        """
        ...

    def find_profile(self, profile_id: str, version: str) -> RegisteredProfile | None: ...

    def list_profiles(
        self,
        *,
        profile_id: str | None = None,
        doc_type: str | None = None,
    ) -> list[RegisteredProfile]:
        """档案列表，按 ``(profile_id, registered_at, version)`` 升序。

        平局键取 ``version`` 的理由与 :meth:`TemplateRepository.list_templates` 完全
        相同（同一时刻批量登记）；两个列表页并排展示，次序口径必须一致。
        """
        ...

    def save_profile(self, entry: RegisteredProfile) -> RegisteredProfile:
        """整体写回（废弃标记走这里）。不存在则抛 ``E_NOT_FOUND``。"""
        ...


@dataclass(frozen=True)
class RegisteredDomainModel:
    """一个**已登记**的领域模型声明（``domain_models`` 表的一行）。

    身份是 ``kind`` **本身**，不带版本维度——这与 :class:`RegisteredProfile` /
    :class:`RegisteredTemplate` 刻意不同，理由不是"少写一个字段"：

    * 领域模型的用途是回答"这个类型**长什么样**"，引用它的地方（档案的 ``for_each``、
    追溯边 ``<kind>:<key>``）写的都是裸 ``kind``，**没有一处能带上版本**。加一个
    ``version`` 就立刻出现"库里有两版 ``endpoint``，``for_each: endpoint`` 指的是哪一版"
    这个任何调用点都答不出的问题。
    * 注册表（``DomainModelRegistry``）的契约本身就是"``kind`` → **单值**映射"：
    同一 ``kind`` 只允许一份声明。库若允许同 ``kind`` 多版本，那把哪一版装进注册表
    就成了第二处判定。

    代价是"改一份声明"看不到历史（不同于模板包与档案的 ``(id, version)`` 留痕）。
    这是**有意的取舍**：领域模型是"词汇表"，不是"产物"——没有一份历史产出会引用
    "``endpoint`` 的第 2 版"。

    ``fingerprint`` 存 :func:`docfidelity.pipeline.fingerprint_domain_model` 的摘要：
    它是"重复登记该判幂等还是冲突"的判据（硬规则 24 的「按内容判」），
    与模板包、档案共用同一条纪律。
    """

    model: DomainModel
    fingerprint: str
    registered_at: datetime
    registered_by: str | None = None
    deprecated: bool = False
    deprecated_reason: str | None = None

    @property
    def kind(self) -> str:
        return self.model.kind

    @property
    def title(self) -> str:
        return self.model.title

    @property
    def ref(self) -> str:
        """引用形式。**就是 ``kind`` 本身**——没有版本可拼（见类文档）。"""
        return self.model.kind


class DomainModelRepository(Protocol):
    """领域模型库仓储（声明很小且没有二进制，**整份落在 ``payload`` 里**）。

    与模板包 / 档案不同，领域模型**没有磁盘本体**：它是一段几百字节的 JSON，
    没有"整包导出给第三方"这条路径（FR-M2-25 是档案的要求）。给它造一个目录布局，
    换来的是"文件和库里的记录哪个才算原件"这个新问题。
    """

    def add_domain_model(self, entry: RegisteredDomainModel) -> RegisteredDomainModel:
        """登记一份声明（**upsert 原语**）。

        与 ``add_profile`` 同口径：同 ``kind`` 再写一次就是覆盖，而"重复登记是拒绝
        还是覆盖"由用例层判——仓储若自己判，合法策略就有了第二个判定口径。
        """
        ...

    def find_domain_model(self, kind: str) -> RegisteredDomainModel | None: ...

    def list_domain_models(self) -> list[RegisteredDomainModel]:
        """全部声明（含已废弃），按 ``(registered_at, kind)`` 升序。

        平局键取 ``kind`` 的理由与 ``list_templates`` / ``list_profiles`` 相同：
        脚本批量登记会让若干声明拿到同一个时刻，只按时刻排的次序取决于插入顺序，
        而两种仓储实现会给出两个次序。
        """
        ...

    def save_domain_model(self, entry: RegisteredDomainModel) -> RegisteredDomainModel:
        """整体写回（废弃标记走这里）。不存在则抛 ``E_NOT_FOUND``。"""
        ...


@dataclass(frozen=True)
class FactSourceRecord:
    """一个项目里的**事实源实例**（``fact_sources.yaml`` 的一行，落库形态）。

    直接持有 :class:`~docfidelity.contracts.fact.SourceConfig` 而不是把它的字段摊平：
    那份配置就是外部数据本身（``root`` / ``options`` / ``bindings`` 都是使用者写的），
    摊平等于在服务端再维护一份同构的模型。

    最近一次探测的结果（``last_probed_at`` / ``last_probe_ok`` / ``last_probe_detail``
    / ``item_count``）是**留痕**，不参与任何判定：来源能不能用只由"现在探一次"回答。
    但 ``None`` 与 ``False`` 必须区分——前者是"从没探过"，后者是"探了、没通"，
    界面上一句"未探测"与一句"上次探测失败"要给不同的下一步动作。
    """

    project_id: str
    config: SourceConfig
    created_at: datetime
    created_by: str | None = None
    last_probed_at: datetime | None = None
    last_probe_ok: bool | None = None
    last_probe_detail: str | None = None
    item_count: int | None = None

    @property
    def source_id(self) -> str:
        return self.config.source_id

    @property
    def source_type(self) -> str:
        return self.config.source_type


class SourceRepository(Protocol):
    """事实源仓储（每项目一份列表）。"""

    def add_source(self, record: FactSourceRecord) -> FactSourceRecord:
        """新增（**upsert 原语**；重复策略由用例层判，同 :meth:`ProfileRepository.add_profile`）。"""
        ...

    def find_source(self, project_id: str, source_id: str) -> FactSourceRecord | None: ...

    def list_sources(self, project_id: str) -> list[FactSourceRecord]:
        """该项目的全部来源，按 ``(created_at, source_id)`` 升序。"""
        ...

    def save_source(self, record: FactSourceRecord) -> FactSourceRecord:
        """整体写回（探测留痕走这里）。不存在则抛 ``E_NOT_FOUND``。"""
        ...

    def remove_source(self, project_id: str, source_id: str) -> bool:
        """删除来源声明。返回是否真的存在过。

        **删除不影响历史产出**：事实包与追溯边里记的是 ``source_id``（一个字符串），
        不是这条记录。所以删掉来源只是"以后不再接这一份"，而不是"以前的文档作废"。
        """
        ...


class Repository(
    ProjectRepository,
    ConfigRepository,
    JobRepository,
    ArtifactRepository,
    ReportRepository,
    UploadRepository,
    TemplateRepository,
    ProfileRepository,
    DomainModelRepository,
    SourceRepository,
    Protocol,
):
    """各类仓储的聚合面（依赖注入只注入这一个对象）。

    聚合而不是分散注入：这些仓储之间**没有**跨库事务，硬拆只会让每个用例函数都
    多收若干个参数，而它们总是一起被用到。
    """
