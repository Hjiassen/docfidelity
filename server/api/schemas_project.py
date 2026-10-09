"""项目作用域端点的响应模型：总览（P02）、产物与对比（P11）、漂移（P14）、反向引用。

为什么再起一个模块
------------------
``schemas`` 装主流程（项目 / 配置 / 上传 / 作业 / 裁决 / 模板入库），``schemas_assets``
装库与资产的治理，本模块装**项目作用域上的聚合视图**。三者的读者不同：发起构建的人、
维护资产的人、看项目状态的人。合在一起会越过 NFR-09 的 800 行上限，而按行数硬切会
切出"前半段在 A、后半段在 B"的东西。

``equal`` 与 ``count`` 这类结论一律由服务端给
--------------------------------------------
界面**不得**按 ``added`` / ``removed`` / ``changed`` 是否为空再推一遍"两份一样吗"
（NFR-22）：比对器那边"空集合 = 一致"这条判据的唯一实现已经在
``pipeline.artifacts.compare_documents`` 里，推第二遍就有了两个答案。
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from docfidelity.contracts.job import Artifact, ChildProgress, Job, JobKind, JobStatus
from docfidelity.contracts.qa import Verdict
from docfidelity.pipeline import DiffMode, DocumentDiff, ProfileReference

from ..artifacts import ArtifactComparison, ArtifactGroup, ProjectArtifacts
from ..overview import (
    DriftItem,
    DriftReport,
    LatestJobSummary,
    OverviewStep,
    ProjectMetrics,
    ProjectOverview,
)
from ..precheck import PrecheckReport
from .schemas import InboxItemOut, PrecheckReportOut, _Body

__all__ = [
    "ArtifactCompareOut",
    "ArtifactCompareRequest",
    "ArtifactGroupOut",
    "DocumentDiffOut",
    "DriftItemOut",
    "DriftReportOut",
    "LatestJobOut",
    "OverviewStepOut",
    "ProjectArtifactsOut",
    "ProjectMetricsOut",
    "ProjectOverviewOut",
    "TemplateReferenceOut",
    "TemplateReferencesOut",
]


# ------------------------------------------------------------------ P02 总览
class ProjectMetricsOut(BaseModel):
    """指标卡的五个数。每个数的口径写在 :class:`~server.overview.ProjectMetrics`。"""

    documents: int = 0
    templates: int = 0
    sources: int = 0
    artifacts: int = 0
    pending_decisions: int = 0

    @classmethod
    def of(cls, metrics: ProjectMetrics) -> ProjectMetricsOut:
        return cls(
            documents=metrics.documents,
            templates=metrics.templates,
            sources=metrics.sources,
            artifacts=metrics.artifacts,
            pending_decisions=metrics.pending_decisions,
        )


class OverviewStepOut(BaseModel):
    """三步引导的一步。

    只给 ``key`` 与 ``done``，**不给中文标签**——标签属于界面文案（``web/src/i18n``），
    而"这一步做完了没有"是业务结论。两件事分开放，改文案时才不用发服务端。
    """

    key: str
    done: bool
    hint: str | None = None

    @classmethod
    def of(cls, step: OverviewStep) -> OverviewStepOut:
        return cls(key=step.key, done=step.done, hint=step.hint)


class LatestJobOut(BaseModel):
    """最近一次作业（P02 区域 4）。``progress`` 只对编排作业非空。"""

    job: Job
    progress: ChildProgress | None = None
    artifact_count: int = 0

    @classmethod
    def of(cls, summary: LatestJobSummary) -> LatestJobOut:
        return cls(
            job=summary.job,
            progress=summary.progress,
            artifact_count=summary.artifact_count,
        )


class ProjectOverviewOut(BaseModel):
    """P02 的一屏数据。"""

    project_id: str
    metrics: ProjectMetricsOut = Field(default_factory=ProjectMetricsOut)
    steps: list[OverviewStepOut] = Field(default_factory=list)
    pending: list[InboxItemOut] = Field(default_factory=list)
    pending_total: int = 0
    last_job: LatestJobOut | None = None
    has_config: bool = False
    precheck: PrecheckReportOut | None = None

    @classmethod
    def of(cls, overview: ProjectOverview, precheck: PrecheckReport | None) -> ProjectOverviewOut:
        return cls(
            project_id=overview.project_id,
            metrics=ProjectMetricsOut.of(overview.metrics),
            steps=[OverviewStepOut.of(step) for step in overview.steps],
            pending=[InboxItemOut.of(item) for item in overview.pending],
            pending_total=overview.pending_total,
            last_job=None if overview.last_job is None else LatestJobOut.of(overview.last_job),
            has_config=overview.has_config,
            precheck=None if precheck is None else PrecheckReportOut.of(precheck),
        )


# ------------------------------------------------------------------ P11 产物
class ArtifactGroupOut(BaseModel):
    """一层产物分组（编排作业 → 子作业 → 文档）。

    ``kind`` / ``status`` 用 ``JobKind`` / ``JobStatus`` 而不是 ``str``：这两个值就是
    本组那个作业的两列，写成 ``str`` 会让接口形状丢掉取值集合（生成给前端的类型变成
    ``string``），前端只好手写一份"状态 → 中文"的表——与 ``zh.job.status`` 迟早分叉。

    ``report_verdict`` 为 ``None`` 表示这份作业**没有门禁报告**（需求 P11：该产物
    "未经门禁，仅作草稿"）。界面照抄这个事实，不按作业状态去猜。
    """

    job_id: str
    kind: JobKind
    doc_key: str | None = None
    status: JobStatus
    created_at: datetime
    artifacts: list[Artifact] = Field(default_factory=list)
    children: list[ArtifactGroupOut] = Field(default_factory=list)
    report_verdict: Verdict | None = None

    @classmethod
    def of(cls, group: ArtifactGroup) -> ArtifactGroupOut:
        return cls(
            job_id=group.job_id,
            kind=group.kind,
            doc_key=group.doc_key,
            status=group.status,
            created_at=group.created_at,
            artifacts=list(group.artifacts),
            children=[ArtifactGroupOut.of(child) for child in group.children],
            report_verdict=group.report_verdict,
        )


class ProjectArtifactsOut(BaseModel):
    """项目产物列表。``count`` / ``total_bytes`` 由服务端算（界面不做求和）。"""

    project_id: str
    groups: list[ArtifactGroupOut] = Field(default_factory=list)
    count: int = 0
    total_bytes: int = 0

    @classmethod
    def of(cls, payload: ProjectArtifacts) -> ProjectArtifactsOut:
        return cls(
            project_id=payload.project_id,
            groups=[ArtifactGroupOut.of(group) for group in payload.groups],
            count=payload.count,
            total_bytes=payload.total_bytes,
        )


class DocumentDiffOut(BaseModel):
    """两份产出的差异。

    ``mode`` 用 ``DiffMode``（``qa.compare`` 的三个字面量）而不是 ``str``：界面的下拉框
    与"服务端认的值"因而只有一个来源。``equal`` 是服务端结论（"两份一样吗"），界面照抄；
    ``truncated`` 说明差异条目触到了比对器的上限（``qa.compare.MAX_ITEMS``），
    界面据此提示"只显示前 N 条"。
    """

    mode: DiffMode
    baseline_ref: str = ""
    candidate_ref: str = ""
    normalized: bool = True
    equal: bool = True
    added: list[str] = Field(default_factory=list)
    removed: list[str] = Field(default_factory=list)
    changed: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    truncated: bool = False

    @classmethod
    def of(cls, diff: DocumentDiff) -> DocumentDiffOut:
        return cls(
            mode=diff.mode,
            baseline_ref=diff.baseline_ref,
            candidate_ref=diff.candidate_ref,
            normalized=diff.normalized,
            equal=diff.equal,
            added=list(diff.added),
            removed=list(diff.removed),
            changed=list(diff.changed),
            notes=list(diff.notes),
            truncated=diff.truncated,
        )


class ArtifactCompareRequest(_Body):
    """比对两份 docx 产物。``mode`` 的取值与 ``qa.compare`` 同源（``COMPARE_MODES``）。

    ``mode`` 收成 ``Literal`` 而不是 ``str``：取值清单交给请求模型，界面上的下拉框
    与"服务端认的值"就只有一个来源——否则新增一种分辨率时，两处会各自演化，
    而表现是"界面能选、服务端报不支持"。
    """

    baseline_id: str
    candidate_id: str
    mode: Literal["structure", "text", "contract"] = "structure"


class ArtifactCompareOut(BaseModel):
    """一次比对的结果：差异 + 参与比对的两份产物。"""

    diff: DocumentDiffOut
    baseline: Artifact
    candidate: Artifact

    @classmethod
    def of(cls, comparison: ArtifactComparison) -> ArtifactCompareOut:
        return cls(
            diff=DocumentDiffOut.of(comparison.diff),
            baseline=comparison.baseline,
            candidate=comparison.candidate,
        )


# ------------------------------------------------------------------ P14 漂移
class DriftItemOut(BaseModel):
    """一条漂移项。``kind`` 的两种取值见 ``server.overview.DriftItem``。"""

    doc_key: str
    kind: str
    detail: str
    last_job_id: str | None = None
    last_generated_at: datetime | None = None

    @classmethod
    def of(cls, item: DriftItem) -> DriftItemOut:
        return cls(
            doc_key=item.doc_key,
            kind=item.kind,
            detail=item.detail,
            last_job_id=item.last_job_id,
            last_generated_at=item.last_generated_at,
        )


class DriftReportOut(BaseModel):
    """漂移清单。

    ``fact_level_available`` 与 ``fact_level_note`` 一起用：前者为 ``False`` 时界面
    **必须**把说明显示出来（硬规则 11 —— 缺能力跳过不得渲染成"没有漂移"）。
    """

    project_id: str
    items: list[DriftItemOut] = Field(default_factory=list)
    checked_documents: int = 0
    fact_level_available: bool = False
    fact_level_note: str = ""

    @classmethod
    def of(cls, report: DriftReport) -> DriftReportOut:
        return cls(
            project_id=report.project_id,
            items=[DriftItemOut.of(item) for item in report.items],
            checked_documents=report.checked_documents,
            fact_level_available=report.fact_level_available,
            fact_level_note=report.fact_level_note,
        )


# ------------------------------------------------------- 模板包的反向引用
class TemplateReferenceOut(BaseModel):
    """一条"某档案版本引用了这个模板包"的记录。"""

    profile_id: str
    version: str
    ref: str
    doc_type: str
    title: str
    deprecated: bool = False
    declared_in: list[str] = Field(default_factory=list)

    @classmethod
    def of(cls, item: ProfileReference) -> TemplateReferenceOut:
        return cls(
            profile_id=item.profile_id,
            version=item.version,
            ref=f"{item.profile_id}@{item.version}",
            doc_type=item.doc_type,
            title=item.title,
            deprecated=item.deprecated,
            declared_in=list(item.declared_in),
        )


class TemplateReferencesOut(BaseModel):
    """反向引用清单（P04 概要页签的「引用该包的作业与文档」）。

    ``count`` 与 ``deprecated_count`` 由服务端算：界面要的是"还能不能废弃这一版"，
    而那条判断是"有没有**未废弃**的档案还在引用它"，不是数量本身。
    """

    template_id: str
    version: str | None = None
    items: list[TemplateReferenceOut] = Field(default_factory=list)
    count: int = 0
    active_count: int = 0

    @classmethod
    def of(
        cls, *, template_id: str, version: str | None, items: list[ProfileReference]
    ) -> TemplateReferencesOut:
        return cls(
            template_id=template_id,
            version=version,
            items=[TemplateReferenceOut.of(item) for item in items],
            count=len(items),
            active_count=sum(1 for item in items if not item.deprecated),
        )
