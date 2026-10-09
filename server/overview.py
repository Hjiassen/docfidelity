"""项目状态的数据形状与判定（P02 项目总览 / P14 漂移监控）。

为什么是"纯计算 + 显式入参"的模块函数，而不是又一个 mixin
--------------------------------------------------------
这两个用例都要跨域取数：总览要配置、事实源、作业、待裁决、前置检查，漂移要配置版本
与作业历史。做成 ``Services`` 的 mixin 就得伸手调兄弟 mixin（``self.inbox(...)`` /
``self.precheck(...)``），而 ``services`` 模块立下的边界是"**每个 mixin 只用宿主字段**
repository / settings / now / runner"——那条边界的用处很实在：任何一个 mixin 都能被单独
读懂，不必先把整个 ``Services`` 拼出来。

所以这里的函数收**已经取好的结果**（``pending`` / ``precheck``），只做计算；
``Services`` 上那两个薄方法是唯一的组装点。附带的好处是这些判定可以在单测里
不启 HTTP、不建应用地直接喂数据验证。

界面不自己拼这四个数
--------------------
P02 的一句话是"一屏回答这个项目的文档处于什么状态、我下一步该做什么"。它要的数来自
四处，而"什么算完成一步""最近一次构建算不算成功"都是**业务结论**。让界面拉四份列表
自己数，等于把业务结论摊到浏览器里（NFR-22），并且每加一张卡片就多一处口径。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Final

from docfidelity.contracts.job import ChildProgress, Job
from docfidelity.pipeline import child_progress

if TYPE_CHECKING:
    from .adjudication import PendingSubject
    from .precheck import PrecheckReport
    from .storage import Repository

__all__ = [
    "FACT_LEVEL_NOTE",
    "NO_SNAPSHOT_NOTE",
    "OVERVIEW_PENDING_LIMIT",
    "DriftItem",
    "DriftReport",
    "LatestJobSummary",
    "OverviewStep",
    "ProjectMetrics",
    "ProjectOverview",
    "build_drift_report",
    "build_overview",
]

#: 待裁决待办在总览页显示的最大条数（需求 P02 区域 3 写的是 "Top N"）。
#: 总览页的用处是"扫一眼有没有要处理的"，全部列出来会让它变成第二个 P12。
OVERVIEW_PENDING_LIMIT: Final = 5

#: 严重级别的排序权重：越靠前越严重。``None``（决策类待办没有严重级别）排在已分级的
#: 之后而不是最前——它不是"最严重"，它只是没有级别。
_SEVERITY_ORDER: Final[dict[str, int]] = {"blocker": 0, "major": 1, "minor": 2}
_SEVERITY_UNRANKED: Final = 3
_EPOCH: Final = datetime.fromisoformat("1970-01-01T00:00:00+00:00")

#: P14 的空态文案（需求 P14「状态与空态」原文）。事实快照还没有任何落点，
#: 所以"一次构建都没跑过"与"跑了但没有快照"在这个项目里是同一件事。
NO_SNAPSHOT_NOTE: Final = "需先完成一次构建以建立事实快照"

#: 事实级漂移为什么判不了。**必须显示**：把"查不了"渲染成"没有漂移"，使用者会以为
#: 文档是最新的（硬规则 11：缺能力跳过不得算作通过，界面照实标注）。
FACT_LEVEL_NOTE: Final = (
    "事实级漂移（接口 / 表 / 字段）需要两份事实快照做比对；快照持久化尚未落地，"
    "本页目前只判定「配置已变更但文档未重新产出」。"
)

#: 三步引导的键。**标签在界面**（i18n），完成判定在服务端——界面不自己数。
STEP_TEMPLATE: Final = "template"
STEP_SOURCE: Final = "source"
STEP_BUILD: Final = "build"


@dataclass(frozen=True)
class ProjectMetrics:
    """指标卡的五个数。每个数各自的定义写在这里，界面上照抄名字即可。

    * ``documents``：配置里**启用**的文档条目数（不是全部条目——停用的不算待产出的）；
    * ``templates``：这些条目引用到的**不同模板包版本**数（``template_id@version``）；
    * ``sources``：该项目已登记的事实源实例数；
    * ``artifacts``：该项目全部作业已登记的产物条目数；
    * ``pending_decisions``：待裁决项数（与 ``/inbox`` 同源，故两处永远一致）。
    """

    documents: int = 0
    templates: int = 0
    sources: int = 0
    artifacts: int = 0
    pending_decisions: int = 0


@dataclass(frozen=True)
class OverviewStep:
    """三步引导的一步。``hint`` 只有未完成时给——完成的那一步不需要"接下来做什么"。"""

    key: str
    done: bool
    hint: str | None = None


@dataclass(frozen=True)
class LatestJobSummary:
    """最近一次作业（需求 P02 区域 4）。

    ``progress`` 对编排作业非空（``n/N`` 与当前子作业）；子作业单跑时为 ``None``，
    界面按"单份文档作业"渲染即可，不必自己判断 ``kind``。
    """

    job: Job
    progress: ChildProgress | None = None
    artifact_count: int = 0


@dataclass(frozen=True)
class ProjectOverview:
    """P02 的一屏数据。"""

    project_id: str
    metrics: ProjectMetrics = field(default_factory=ProjectMetrics)
    steps: tuple[OverviewStep, ...] = ()
    pending: tuple[PendingSubject, ...] = ()
    pending_total: int = 0
    last_job: LatestJobSummary | None = None
    #: 配置是否存在。**与"配置为空"不同**：前者要引导去初始化，后者只是还没登记文档。
    has_config: bool = False
    #: 服务端的前置检查结论。界面拿它的 ``ok`` 决定"发起构建"是否可用，
    #: 拿它的 ``skipped`` 显示"哪些检查这一轮没做"（硬规则 15 / 硬规则 11）。
    #: ``None`` = **这个项目还没有配置**，这一步没有可检查的对象——不是"检查没跑"。
    precheck: PrecheckReport | None = None


@dataclass(frozen=True)
class DriftItem:
    """一条漂移项（P14 清单的一行）。"""

    doc_key: str
    #: ``never_built`` = 配置里启用但一次都没成功产出过；
    #: ``changed_not_synced`` = 最后一次成功产出之后配置又改过（需求里的"已变更未同步"）。
    #:
    #: 需求列的三类（已变更未同步 / 已同步未验证 / 长期未动）都是**事实级**的判据，
    #: 需要两份事实快照；当前只有第一类存在配置级的对应物，故只给这一种 + ``never_built``。
    #: 用字符串而不是布尔量：加第三类时不必改载荷形状。
    kind: str
    detail: str
    last_job_id: str | None = None
    last_generated_at: datetime | None = None


@dataclass(frozen=True)
class DriftReport:
    """P14 的一屏数据。"""

    project_id: str
    items: tuple[DriftItem, ...] = ()
    checked_documents: int = 0
    #: 事实级漂移能不能判定。当前恒为 ``False``：见 :data:`FACT_LEVEL_NOTE`。
    fact_level_available: bool = False
    fact_level_note: str = FACT_LEVEL_NOTE


def build_overview(
    *,
    repository: Repository,
    project_id: str,
    pending: Sequence[PendingSubject],
    precheck: PrecheckReport | None,
) -> ProjectOverview:
    """组装 P02 的一屏数据。"""
    repository.get_project(project_id)
    config = repository.latest_config(project_id)
    documents = config.enabled_documents() if config is not None else []
    refs = {entry.template_ref for entry in documents if entry.template_ref}
    registered = {entry.ref for entry in repository.list_templates()}
    sources = repository.list_sources(project_id)
    jobs = repository.list_jobs(project_id=project_id)

    top_pending = _top_pending(pending)
    return ProjectOverview(
        project_id=project_id,
        metrics=ProjectMetrics(
            documents=len(documents),
            templates=len(refs),
            sources=len(sources),
            artifacts=sum(len(job.artifact_ids) for job in jobs),
            pending_decisions=len(pending),
        ),
        steps=_steps(refs=refs, registered=registered, sources=sources, jobs=jobs),
        pending=top_pending,
        pending_total=len(pending),
        last_job=_latest_job(repository, jobs),
        has_config=config is not None,
        precheck=precheck,
    )


def build_drift_report(*, repository: Repository, project_id: str) -> DriftReport:
    """组装 P14 的漂移清单。

    判据只有一条、且是**确定性的时间比较**：某份文档最后一次成功产出的时刻，早于
    最新配置版本的写入时刻 ⇒ 那份文档可能过期。用具名判据而不是"跑一次就知道"，
    是因为漂移页的价值在于**在构建之前**告诉人要重跑什么；等构建跑完再说就晚了。
    """
    repository.get_project(project_id)
    config = repository.latest_config(project_id)
    versions = repository.list_configs(project_id)
    newest = versions[-1] if versions else None
    jobs = repository.list_jobs(project_id=project_id)

    # 每份文档最后一次**成功**产出。``list_jobs`` 升序 ⇒ 循环里后到的覆盖先到的。
    last_success: dict[str, Job] = {}
    for job in jobs:
        if job.kind == "document" and job.status == "succeeded" and job.doc_key:
            last_success[job.doc_key] = job

    documents = config.enabled_documents() if config is not None else []
    items: list[DriftItem] = []
    for entry in documents:
        latest = last_success.get(entry.doc_key)
        if latest is None:
            items.append(
                DriftItem(
                    doc_key=entry.doc_key,
                    kind="never_built",
                    detail="配置里已启用，但还没有一次成功的产出",
                )
            )
            continue
        if newest is not None and latest.created_at < newest.created_at:
            items.append(
                DriftItem(
                    doc_key=entry.doc_key,
                    kind="changed_not_synced",
                    detail=(
                        f"最后一次成功产出（{latest.job_id}）之后，项目配置又写过新版本"
                        f"（v{newest.version}）"
                    ),
                    last_job_id=latest.job_id,
                    last_generated_at=latest.created_at,
                )
            )
    items.sort(key=lambda item: item.doc_key)
    return DriftReport(
        project_id=project_id,
        items=tuple(items),
        checked_documents=len(documents),
    )


def _steps(
    *,
    refs: set[str],
    registered: set[str],
    sources: Sequence[object],
    jobs: Sequence[Job],
) -> tuple[OverviewStep, ...]:
    """三步引导（需求 P02「状态与空态」）。

    "入库模板"这一步的完成判据用了**两条**：配置里引用到了模板包，且那些引用都已入库。
    只看后者会让"一份文档都没配"的项目显示"模板已就绪"——那一步的引导语于是指向一个
    已经打勾的项，使用者会去别处找问题。
    """
    template_done = bool(refs) and refs <= registered
    source_done = bool(sources)
    build_done = any(job.kind == "document" and job.status == "succeeded" for job in jobs)
    return (
        OverviewStep(
            key=STEP_TEMPLATE,
            done=template_done,
            hint=None if template_done else "在 P05 入库模板包，并在项目配置里引用它",
        ),
        OverviewStep(
            key=STEP_SOURCE,
            done=source_done,
            hint=None if source_done else "在 P06 登记这个项目的事实源",
        ),
        OverviewStep(
            key=STEP_BUILD,
            done=build_done,
            hint=None if build_done else "在 P08 发起一次构建",
        ),
    )


def _top_pending(pending: Sequence[PendingSubject]) -> tuple[PendingSubject, ...]:
    """按严重级别取前 N 条（需求 P02 区域 3「Top N，按严重级别排序」）。

    排序放服务端：``severity`` 是字符串（``blocker`` / ``major`` / ``minor`` / ``None``），
    让界面自己按字面量排，就等于有了第二处"什么叫更严重"——而那个顺序一旦分叉，
    最该先处理的一条会被排到中间。

    平局按 ``created_at`` 再按 ``subject_id``：待办列表每次刷新换个顺序，使用者会以为
    "刚才那条不见了"（与 ``AdjudicationMixin.inbox`` 同一条理由）。
    """
    ordered = sorted(
        pending,
        key=lambda item: (
            _SEVERITY_ORDER.get(item.severity or "", _SEVERITY_UNRANKED),
            item.created_at or _EPOCH,
            item.subject_id,
        ),
    )
    return tuple(ordered[:OVERVIEW_PENDING_LIMIT])


def _latest_job(repository: Repository, jobs: Sequence[Job]) -> LatestJobSummary | None:
    """最近一次作业：优先取**编排作业**（需求 P02 区域 4 要的就是"作业号（编排）"）。

    取不到编排作业时才退回普通作业——单份文档也能从 P08 单独发起，那时列表里没有
    编排作业，而"最近作业"这一栏不该是空的。
    """
    if not jobs:
        return None
    chosen = next((job for job in reversed(jobs) if job.is_orchestration), jobs[-1])
    progress = chosen.progress
    if progress is None and chosen.is_orchestration:
        children = [
            child
            for child in (repository.find_job(child_id) for child_id in chosen.child_job_ids)
            if child is not None
        ]
        progress = child_progress(children)
    return LatestJobSummary(
        job=chosen,
        progress=progress,
        artifact_count=len(chosen.artifact_ids),
    )
