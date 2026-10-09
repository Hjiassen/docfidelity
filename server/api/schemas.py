"""HTTP 请求 / 响应模型（DD-13.2 / DD-13.3）。

为什么这些不放在 ``contracts``
------------------------------
``contracts`` 是**引擎**与外部系统之间的数据契约（模板包、IR、事实包…），
它们会导出成 JSON Schema 供他人实现。本模块这些是**本服务的接口形状**，
随端点演进，不承担"他人按此实现"的义务。混进 ``contracts`` 会让每次调接口
都要重跑 ``export_schemas`` 并动到已发布的 schema。

不变量：**响应体里绝不出现密钥**（DD-13.7），故本模块没有任何"settings 全量"模型。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from docfidelity.contracts.config import DataBoundary, ProjectConfig, RetentionPolicy
from docfidelity.contracts.errors import EngineErrorInfo
from docfidelity.contracts.job import ChildProgress, Job, JobStatus, Stage, StageName
from docfidelity.contracts.qa import (
    Adjudication,
    AdjudicationAction,
    Finding,
    Verdict,
    Waiver,
)
from docfidelity.contracts.template import ReviewAction, ReviewOverride
from docfidelity.pipeline import (
    BaselineStatus,
    ContractSummary,
    GrammarCapability,
    ImportStage,
)

from ..adjudication import PendingSubject
from ..precheck import PrecheckIssue, PrecheckReport
from ..retention import RetentionKind
from ..storage import ConfigVersion, Project, RegisteredTemplate, UploadRecord
from ..templates import TemplateImportRecord, TemplateRegistration

__all__ = [
    "AdjudicationRequest",
    "AdjudicationResponse",
    "CleanupPlanOut",
    "CleanupRequest",
    "DeterministicSettingsOut",
    "ConfigPutResponse",
    "ConfigResponse",
    "ConfigVersionOut",
    "ContractSummaryOut",
    "ExpiredResponse",
    "ImportStageOut",
    "InboxItemOut",
    "InboxResponse",
    "JobDetail",
    "JobListResponse",
    "ModelCapabilities",
    "ModelHealth",
    "ModelSettingsOut",
    "PrecheckIssueOut",
    "PrecheckReportOut",
    "ProjectCreate",
    "ProjectOut",
    "ProjectPatch",
    "RegisterRequest",
    "RegisteredTemplateOut",
    "ResumeRequest",
    "RetainedObjectOut",
    "ReviewDecisionIn",
    "ReviewRequest",
    "SettingsPatch",
    "SettingsView",
    "StagingObjectOut",
    "StagingPurgeOut",
    "StagingPurgeRequest",
    "StagingResponse",
    "SubmitBuild",
    "TemplateImportOut",
    "TemplateLibraryResponse",
    "TemplateRegistrationOut",
    "UploadLimitsOut",
    "UploadOut",
    "ValidateRequest",
    "WorkerSettingsOut",
]


class _Body(BaseModel):
    """请求体基类：未知字段一律拒绝（多传的字段往往意味着前端版本不匹配）。"""

    model_config = ConfigDict(extra="forbid")


# ------------------------------------------------------------------ 项目
class ProjectCreate(_Body):
    name: str = Field(min_length=1, max_length=200)
    demo: bool = False


class ProjectPatch(_Body):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    archived: bool | None = None


class ProjectOut(BaseModel):
    project_id: str
    name: str
    created_at: datetime
    updated_at: datetime
    archived: bool
    demo: bool

    @classmethod
    def of(cls, project: Project) -> ProjectOut:
        return cls(
            project_id=project.project_id,
            name=project.name,
            created_at=project.created_at,
            updated_at=project.updated_at,
            archived=project.archived,
            demo=project.demo,
        )


# ------------------------------------------------------------------ 配置
class ConfigVersionOut(BaseModel):
    version: int
    author: str | None
    created_at: datetime

    @classmethod
    def of(cls, entry: ConfigVersion) -> ConfigVersionOut:
        return cls(version=entry.version, author=entry.author, created_at=entry.created_at)


class ConfigPutResponse(BaseModel):
    """写入配置的响应：只回新版本号与作者（**不回整份配置**）。

    为什么不全量回显：PUT 的语义是"我提交了这份配置"，回显整份会让前端误以为
    服务端做过归一化改写。需要整份配置时读 ``GET /config``。
    """

    project_id: str
    version: int
    author: str | None


# ------------------------------------------------------------------ 作业
class SubmitBuild(_Body):
    doc_keys: list[str] | None = Field(
        default=None, description="只构建这些文档；为空表示构建配置里所有启用的文档"
    )
    author: str | None = Field(
        default=None,
        max_length=200,
        description="发起人（作业列表的「发起人」列）。**只落留痕**，不参与任何判定",
    )


class ValidateRequest(_Body):
    doc_keys: list[str] | None = Field(
        default=None, description="只检查这些文档；为空表示检查配置里所有启用的文档"
    )


class PrecheckIssueOut(BaseModel):
    """一条"不能发起"的原因。"""

    kind: str
    message: str
    remediation: list[str] = Field(default_factory=list)
    where: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def of(cls, issue: PrecheckIssue) -> PrecheckIssueOut:
        return cls(
            kind=issue.kind,
            message=issue.message,
            remediation=list(issue.remediation),
            where=dict(issue.where),
        )


class PrecheckReportOut(BaseModel):
    """前置检查结果（``POST /projects/{pid}/config/validate``）。

    ``ok`` 是**服务端给的结论**（"能不能发起"），界面直接用它决定按钮可用性（NFR-22），
    不得自己按 ``issues`` 是否为空再推一遍。``skipped`` 列出**做不了**的检查项：
    能力尚未落地时如实标注，而不是把它算成"检查通过"（与门禁的同一口径）。
    """

    project_id: str
    ok: bool
    doc_keys: list[str] = Field(default_factory=list)
    checked_documents: int = 0
    issues: list[PrecheckIssueOut] = Field(default_factory=list)
    registered_stages: list[str] = Field(default_factory=list)
    missing_stages: list[str] = Field(default_factory=list)
    skipped: list[str] = Field(default_factory=list)

    @classmethod
    def of(cls, report: PrecheckReport) -> PrecheckReportOut:
        return cls(
            project_id=report.project_id,
            ok=report.ok,
            doc_keys=list(report.doc_keys),
            checked_documents=report.checked_documents,
            issues=[PrecheckIssueOut.of(issue) for issue in report.issues],
            registered_stages=list(report.registered_stages),
            missing_stages=list(report.missing_stages),
            skipped=list(report.skipped),
        )


class JobDetail(BaseModel):
    """作业详情（DD-13.2：「含子作业矩阵」）。

    ``progress`` / ``children`` / ``resume_plan`` 由**服务端**给出，前端不得自行推断
    （DD-14.3 / NFR-22）。``resume_plan`` 就是界面那句"将从 X 阶段续跑"。
    """

    job: Job
    stages: list[Stage] = Field(default_factory=list)
    children: list[Job] = Field(default_factory=list)
    progress: ChildProgress | None = None
    resume_plan: list[StageName] = Field(default_factory=list)


class JobListResponse(BaseModel):
    """作业列表。``items`` 之外另给 ``count``：前端不做业务计算（NFR-22）。"""

    items: list[Job] = Field(default_factory=list)
    count: int = 0


class ResumeRequest(_Body):
    from_stage: StageName | None = Field(
        default=None, description="从该阶段续跑；为空表示从第一个不可复用的阶段接着跑"
    )


class CleanupRequest(_Body):
    objects: list[str] = Field(
        default_factory=list, description="要回收的对象 id（**必须逐个列出**）"
    )


class SettingsPatch(_Body):
    patch: dict[str, Any] = Field(default_factory=dict)


class ModelHealth(BaseModel):
    """模型连通性（只读探测）。``reachable`` 为 ``None`` 表示"没探测"（模型未启用）。"""

    enabled: bool
    base_url: str | None = None
    reachable: bool | None = None
    detail: str = ""


class ModelCapabilities(BaseModel):
    """模型侧能力现状（``GET /settings/model/capabilities``）。

    **与 ``ModelHealth`` 分开是有意的**：那个回答"连得上吗"，这个回答"接上之后会怎么跑"。

    ``grammar`` / ``baseline`` 直接复用内核那两个模型（``agents.sdk_adapter`` 与
    ``agents.baseline`` 的公开形状，经 ``pipeline`` 的决策接缝转出），不在这里抄一份：
    抄一份就是"响应字段与服务端读的字段"两处口径，改一处即静默错位（DD-14.1 的前置条件
    就是"形状本身不重复"）。它们随 ``GET /settings/model/capabilities`` 一起进
    ``openapi.json``，前端照生成即可。
    """

    enabled: bool
    grammar: GrammarCapability
    baseline: BaselineStatus


# -------------------------------------------------------------- 保留/清理
class RetainedObjectOut(BaseModel):
    """保留对象。清理候选列表与清理报告**共用同一形状**：同一份数据在两处出现时
    长得不一样，前端就得写两套解析（DD-14.1 禁止手写重复类型的前提是形状本身不重复）。
    """

    object_id: str
    kind: RetentionKind
    path: str
    size_bytes: int
    created_at: datetime | None = None


class ExpiredResponse(BaseModel):
    expired: list[RetainedObjectOut] = Field(default_factory=list)
    count: int = 0


class CleanupPlanOut(BaseModel):
    """清理执行报告（``RetentionPlan.as_dict`` 的接口形状）。"""

    uploads: list[RetainedObjectOut] = Field(default_factory=list)
    workspace: list[RetainedObjectOut] = Field(default_factory=list)
    artifacts: list[RetainedObjectOut] = Field(default_factory=list)
    count: int = 0
    total_bytes: int = 0


# ------------------------------------------------------ 导入暂存目录（回收）
class StagingObjectOut(BaseModel):
    """一个模板导入暂存目录（``imports/<import_id>/``）。

    ``registered`` / ``reclaimable`` / ``blocked_reason`` 三者各有各的差事，不能合并：
    前者是**事实**（这份导入登记了没有），中者是**服务端的判据**（能不能回收），
    后者是不能回收时的**原文措辞**。界面照抄中者、直接显示后者——它自己不去归纳
    "为什么不能删"（硬规则 15 要堵的正是这条）。
    """

    import_id: str
    path: str
    size_bytes: int
    created_at: datetime | None = None
    readable: bool
    registered: bool
    template_ref: str | None = None
    reclaimable: bool
    blocked_reason: str | None = None


class StagingResponse(BaseModel):
    """``GET /maintenance/staging``。

    **不可回收的也一并列出**：过滤掉它们等于把"这里还有一份没登记完的导入"这件事
    藏起来，而它正是使用者要看的。故计两套数：``count`` 是全量，``reclaimable_count``
    是可回收的——两端数字由服务端给，界面不自己数（NFR-22）。
    """

    imports: list[StagingObjectOut] = Field(default_factory=list)
    count: int = 0
    reclaimable_count: int = 0
    total_bytes: int = 0
    reclaimable_bytes: int = 0
    note: str = Field(default="", description="本模块的回收判据，服务端措辞")


class StagingPurgeRequest(_Body):
    objects: list[str] = Field(
        default_factory=list, description="要回收的导入标识（**必须逐个列出**）"
    )


class StagingPurgeOut(BaseModel):
    """回收回执。**只回被回收的那些**——它同时是"这次删了什么"的凭证。"""

    reclaimed: list[StagingObjectOut] = Field(default_factory=list)
    count: int = 0
    total_bytes: int = 0


# ------------------------------------------------------------ 设置（无密钥）
class ModelSettingsOut(BaseModel):
    """模型设置里**可以出网**的部分。密钥不在其中——它不存在于任何响应模型。"""

    enabled: bool
    name: str | None = None
    base_url: str | None = None


class UploadLimitsOut(BaseModel):
    max_bytes: int
    allowed_suffixes: list[str]


class WorkerSettingsOut(BaseModel):
    concurrency: int


class DeterministicSettingsOut(BaseModel):
    """确定性姿态的**生效值**（DD-16.3：合并结果必须可打印，避免"配了但没生效"）。

    三个字段回答三个不同的问题，缺一个就会让人查错方向：

    * ``mode`` —— 缺省姿态本身（``DOCFIDELITY_DETERMINISTIC_MODE``）；
    * ``office_enabled`` —— **最终生效**的 Office 姿态（显式表态压过缺省姿态）；
    * ``office_reason`` —— 生效值为假时的成因，``None`` 表示"没有关掉它"
      （那种情况下刷新失败是"探测不到 Word"，与配置无关）。

    全部带缺省值，故它们不进 OpenAPI 的 ``required``——加这一组不需要前端同步改动。
    """

    mode: bool = True
    office_enabled: bool = False
    office_reason: str | None = None


class SettingsView(BaseModel):
    """``GET/PUT /settings`` 的响应（DD-13.7 的数据边界落点）。

    ``mutable_fields`` 由**服务端**给出，而不是前端写死一份"哪些字段可改"的清单：
    写死就是两处口径，服务端改了可改字段集而前端还在提交旧字段，表现为
    "保存报错但看不出为什么"。界面只需照着它渲染（7.1.1）。
    """

    data_boundary: DataBoundary
    retention: RetentionPolicy
    model: ModelSettingsOut
    uploads: UploadLimitsOut
    workers: WorkerSettingsOut
    deterministic: DeterministicSettingsOut = Field(default_factory=DeterministicSettingsOut)
    mutable_fields: list[str] = Field(default_factory=list)


# ------------------------------------------------------------------ 上传
class UploadOut(BaseModel):
    """上传件回执。

    **不含 ``stored_path``**：服务端的落点属于部署细节（DD-13.7 数据边界）。
    回一份绝对路径出去，既暴露了目录结构，也诱使调用方按路径去取件——
    而取件有它自己的接口（下载走 ``/artifacts/{aid}/download``）。
    """

    upload_id: str
    filename: str
    suffix: str
    size_bytes: int
    content_type: str
    digest: str
    purpose: str
    created_at: datetime

    @classmethod
    def of(cls, record: UploadRecord) -> UploadOut:
        return cls(
            upload_id=record.upload_id,
            filename=record.receipt.filename,
            suffix=record.receipt.suffix,
            size_bytes=record.receipt.size_bytes,
            content_type=record.receipt.content_type,
            digest=record.receipt.digest,
            purpose=record.purpose,
            created_at=record.created_at,
        )


# ---------------------------------------------------------------- 裁决（P10/P12）
class AdjudicationRequest(_Body):
    """一次裁决提交。

    ``action`` 的类型直接取 :data:`~docfidelity.contracts.qa.AdjudicationAction`：规范动作
    只有三种，多一种就多一条绕过门禁的路径。``reason`` 只有 ``waive`` 强制要求
    （服务端据此报 ``E_WAIVER_REASON_REQUIRED``），故这里**不**做"action 为 waive 时
    reason 必填"的 pydantic 校验——把业务规则写进字段校验会让错误码变成 422 的
    "字段必填"，而使用者真正要理解的是"豁免必须留下理由"。
    """

    action: AdjudicationAction
    operator: str = Field(min_length=1, max_length=200, description="责任人；必填（NFR-15）")
    reason: str | None = Field(default=None, max_length=2000, description="理由；waive 时必填")


class AdjudicationResponse(BaseModel):
    """裁决结果。

    除留痕本身之外，把**服务端算好的**结论一并回给界面：``verdict``（重算后的门禁结论）、
    ``job_status`` 与 ``advance`` / ``note``（作业接下来会怎样）。界面照抄即可，
    不必自己推断"这一下点完以后作业还卡着吗"。
    """

    adjudication: Adjudication
    finding: Finding | None = None
    waiver: Waiver | None = None
    report_id: str
    job_id: str
    job_status: JobStatus
    verdict: Verdict
    open_findings: int = 0
    #: 该作业**仍未办结**的待裁决项数（含刚提交的这一条之外的所有项）。
    remaining_subjects: int = 0
    #: ``advanced`` / ``remaining`` / ``blocked`` / ``configure`` / ``not_awaiting``。
    advance: str = "not_awaiting"
    note: str = ""


class InboxItemOut(BaseModel):
    """一条待裁决项（``GET /inbox``）。

    ``resolvable`` 为 ``False`` 时 ``unresolvable_reason`` 必非空：**能不能点、为什么
    不能点**都要由服务端给出（硬约束 15），界面照抄，不自己判断。
    """

    subject_id: str
    kind: str
    job_id: str
    project_id: str
    title: str
    severity: str | None = None
    detail: str | None = None
    created_at: datetime | None = None
    resolvable: bool = True
    unresolvable_reason: str | None = None

    @classmethod
    def of(cls, item: PendingSubject) -> InboxItemOut:
        return cls(
            subject_id=item.subject_id,
            kind=item.kind,
            job_id=item.job_id,
            project_id=item.project_id,
            title=item.title,
            severity=item.severity,
            detail=item.detail,
            created_at=item.created_at,
            resolvable=item.resolvable,
            unresolvable_reason=item.unresolvable_reason,
        )


class InboxResponse(BaseModel):
    """裁决待办聚合。``count`` 由服务端给出，界面不做业务计算（NFR-22）。"""

    items: list[InboxItemOut] = Field(default_factory=list)
    count: int = 0


# ------------------------------------------------------- 模板入库（P05 第 2~5 步）
class ImportStageOut(BaseModel):
    """一个入库阶段的执行结果（P05 第 2 步的进度条）。

    ``error`` 带上完整的内核错误快照（含 ``remediation``），因为"解析卡在哪一步、
    为什么卡、可以做什么"是这一屏的全部价值；只给一个 ``ok=False`` 等于把
    使用者丢回盲猜。
    """

    stage: str
    ok: bool
    note: str | None = None
    error: EngineErrorInfo | None = None

    @classmethod
    def of(cls, stage: ImportStage) -> ImportStageOut:
        return cls(stage=stage.stage, ok=stage.ok, note=stage.note, error=stage.error)


class ContractSummaryOut(BaseModel):
    """契约摘要卡（P05 第 3 步）。字段与 :class:`~docfidelity.pipeline.ContractSummary`
    一一对应，外加服务端算好的 ``slot_completion``——界面不做除法（NFR-22）。
    """

    style_count: int = 0
    paragraph_style_count: int = 0
    table_style_count: int = 0
    used_paragraph_styles: int = 0
    used_table_styles: int = 0
    numbering_levels: int = 0
    numbering_instances: int = 0
    heading_bindings: int = 0
    block_counts: dict[str, int] = Field(default_factory=dict)
    table_prototypes: int = 0
    slot_count: int = 0
    declared_slot_count: int = 0
    chapter_count: int = 0
    max_heading_level: int = 0
    placeholder_count: int = 0
    placeholder_parts: dict[str, int] = Field(default_factory=dict)
    #: 声明了 ``unit_types`` 的槽位占全部槽位的比例；无槽位时为 1.0（见内核同名属性）。
    slot_completion: float = 1.0

    @classmethod
    def of(cls, summary: ContractSummary) -> ContractSummaryOut:
        return cls(
            style_count=summary.style_count,
            paragraph_style_count=summary.paragraph_style_count,
            table_style_count=summary.table_style_count,
            used_paragraph_styles=summary.used_paragraph_styles,
            used_table_styles=summary.used_table_styles,
            numbering_levels=summary.numbering_levels,
            numbering_instances=summary.numbering_instances,
            heading_bindings=summary.heading_bindings,
            block_counts=dict(summary.block_counts),
            table_prototypes=summary.table_prototypes,
            slot_count=summary.slot_count,
            declared_slot_count=summary.declared_slot_count,
            chapter_count=summary.chapter_count,
            max_heading_level=summary.max_heading_level,
            placeholder_count=summary.placeholder_count,
            placeholder_parts=dict(summary.placeholder_parts),
            slot_completion=summary.slot_completion,
        )


class ReviewDecisionIn(_Body):
    """一条待确认项的处理（P05 第 4 步提交体里的一项）。

    ``action=specify`` 必须带 ``value``，服务端据此报 ``E_REVIEW_VALUE_REQUIRED``——
    与裁决的同一条纪律：**不把这个规则写进 pydantic 校验**，否则错误码会变成 422 的
    "字段必填"，而使用者要理解的是"指定取值必须给出取值"。
    """

    item_id: str = Field(min_length=1, description="待确认项标识，取自导入报告的 finding_id")
    action: ReviewAction
    value: str | None = Field(default=None, max_length=2000, description="action=specify 时必填")
    note: str | None = Field(default=None, max_length=2000, description="处理意见")


class ReviewRequest(_Body):
    """``POST /templates/imports/{id}/decisions`` 的请求体。"""

    operator: str = Field(min_length=1, max_length=200, description="责任人；必填（NFR-15）")
    decisions: list[ReviewDecisionIn] = Field(
        min_length=1, description="至少一条；不需要确认时不必调用本接口"
    )


class RegisterRequest(_Body):
    """``POST /templates/imports/{id}/register`` 的请求体。

    ``template_id`` / ``version`` 可在此**最终定版**：解析未能从文档里取到编号时，
    这一步是补上它的正常入口。``on_conflict`` 只有两种取值，没有"自动改版本号"——
    版本号是配置与历史产物的引用键（见 :mod:`server.templates`）。
    """

    on_conflict: Literal["abort", "overwrite"] = Field(
        default="abort", description="同 id@version 已存在且内容不同时的处置"
    )
    template_id: str | None = Field(default=None, max_length=200, description="最终编号")
    version: str | None = Field(default=None, max_length=100, description="最终版本号")
    operator: str | None = Field(default=None, max_length=200, description="登记人；缺省取发起人")


class TemplateRegistrationOut(BaseModel):
    """一次登记的回执（P05 第 5 步）。"""

    template_id: str
    version: str
    ref: str
    registered_at: datetime
    package_hash: str = ""
    source_hash: str = ""
    doc_type: str | None = None
    registered_by: str | None = None
    replaced: bool = False
    parsed: bool = True
    integrity: list[str] = Field(default_factory=list)

    @classmethod
    def of(cls, item: TemplateRegistration) -> TemplateRegistrationOut:
        return cls(
            template_id=item.template_id,
            version=item.version,
            ref=item.ref,
            registered_at=item.registered_at,
            package_hash=item.package_hash,
            source_hash=item.source_hash,
            doc_type=item.doc_type,
            registered_by=item.registered_by,
            replaced=item.replaced,
            parsed=item.parsed,
            integrity=list(item.integrity),
        )


class TemplateImportOut(BaseModel):
    """一次模板导入的完整视图（P05 第 2~5 步的**唯一**响应形状）。

    **不含 ``package_dir``**：暂存落点属于部署细节（DD-13.7 数据边界），回出去既暴露
    目录结构，也诱使调用方绕开接口去磁盘上取包。

    读者容易忽略的一点：``findings`` 与 ``pending`` 是**同一批项的前后两态**——
    ``findings`` 是报告里全部待确认项，``pending`` 是其中**还没有留痕**的那些。
    界面第 4 步要的正是后者（"还剩几项要确认"），而"待确认项为 0 → 跳过第 4 步"
    这个分支就落在 ``pending`` 是否为空上（服务端给判据，界面不自己数）。
    """

    import_id: str
    created_at: datetime
    #: 最近一次**重解析**的时刻；``None`` = 自导入之后没重解析过。与 ``created_at`` 分开：
    #: 后者是这份导入的身份，重解析不改变身份。
    reparsed_at: datetime | None = None
    source_filename: str
    source_size_bytes: int
    source_hash: str | None = None
    template_id: str = ""
    version: str = ""
    template_id_source: str = ""
    version_source: str = ""
    #: 编号与版本是不是**人给的**（而非从文档/文件名猜的）。界面据此提示"登记前请确认编号"，
    #: 但判据在服务端。
    version_source_explicit: bool = False
    doc_type: str | None = None
    notes: str | None = None
    author: str | None = None
    #: ``True`` = 只登记了原件（解析失败，可稍后重试）；``False`` = 已产出契约。
    register_only: bool = False
    parsed: bool = False
    package_hash: str | None = None
    stages: list[ImportStageOut] = Field(default_factory=list)
    findings: list[Finding] = Field(default_factory=list)
    pending: list[Finding] = Field(default_factory=list)
    pending_count: int = 0
    decisions: list[ReviewOverride] = Field(default_factory=list)
    integrity: list[str] = Field(default_factory=list)
    summary: ContractSummaryOut | None = None
    registration: TemplateRegistrationOut | None = None
    #: 能不能重解析 + 不能时的**服务端措辞**。界面照抄 ``can_reparse`` 置灰按钮、直接显示
    #: ``reparse_blocked_reason``——它不自己归纳"为什么不能重试"（硬规则 15）。
    can_reparse: bool = True
    reparse_blocked_reason: str | None = None

    @classmethod
    def of(cls, record: TemplateImportRecord) -> TemplateImportOut:
        pending = record.pending()
        return cls(
            import_id=record.import_id,
            created_at=record.created_at,
            reparsed_at=record.reparsed_at,
            source_filename=record.source_filename,
            source_size_bytes=record.source_size_bytes,
            source_hash=record.source_hash,
            template_id=record.template_id,
            version=record.version,
            template_id_source=record.template_id_source,
            version_source=record.version_source,
            version_source_explicit=record.version_source_explicit,
            doc_type=record.doc_type,
            notes=record.notes,
            author=record.author,
            register_only=record.register_only,
            parsed=record.parsed,
            package_hash=record.package_hash,
            stages=[ImportStageOut.of(stage) for stage in record.stages],
            findings=list(record.findings),
            pending=pending,
            pending_count=len(pending),
            decisions=list(record.decisions),
            integrity=list(record.integrity),
            summary=None if record.summary is None else ContractSummaryOut.of(record.summary),
            registration=(
                None
                if record.registration is None
                else TemplateRegistrationOut.of(record.registration)
            ),
            can_reparse=record.can_reparse,
            reparse_blocked_reason=record.reparse_blocked_reason,
        )


class RegisteredTemplateOut(BaseModel):
    """模板库里的一条登记（``GET /templates``）。

    **不含 ``package_dir``**（同上：落点不外泄）。``ref`` = ``template_id@version``，
    正是 ``ProjectConfig`` 里 ``template_ref`` 的引用形式，故界面可直接拿它去比对
    "这个版本有没有被配置引用"——但"能不能删"这类判断若要做，仍应由服务端给结论。
    """

    template_id: str
    version: str
    ref: str
    package_hash: str = ""
    source_hash: str = ""
    doc_type: str | None = None
    source_filename: str | None = None
    notes: str | None = None
    registered_by: str | None = None
    registered_at: datetime
    deprecated: bool = False
    deprecated_reason: str | None = None
    deprecated_by: str | None = None

    @classmethod
    def of(cls, item: RegisteredTemplate) -> RegisteredTemplateOut:
        return cls(
            template_id=item.template_id,
            version=item.version,
            ref=item.ref,
            package_hash=item.package_hash,
            source_hash=item.source_hash,
            doc_type=item.doc_type,
            source_filename=item.source_filename,
            notes=item.notes,
            registered_by=item.registered_by,
            registered_at=item.registered_at,
            deprecated=item.deprecated,
            deprecated_reason=item.deprecated_reason,
            deprecated_by=item.deprecated_by,
        )


class TemplateLibraryResponse(BaseModel):
    """模板库列表。``count`` 由服务端给出（NFR-22）。"""

    items: list[RegisteredTemplateOut] = Field(default_factory=list)
    count: int = 0


#: 配置读取的响应就是 :class:`~docfidelity.contracts.config.ProjectConfig` 本身。
ConfigResponse = ProjectConfig
