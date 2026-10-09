"""作业队列 · 项目锁 · worker（DD-11.4 / DD-11.5 / DD-13.1）。

调度策略只有三条，但每条都对应一个具体的坏结果
------------------------------------------------
① **同项目串行**（DD-11.5）。锁是**租约**（``expires_at``）而不是布尔锁：worker 崩了
   锁不会自己释放，没有过期时间的项目会永久停在"排队中"，而界面上看不出区别。
② **取不到锁就跳过，不改状态**。作业留在 ``queued`` 等下一轮——若改成 ``failed``，
   一次正常的排队会被显示成失败（而它其实什么都没错）。
③ **阶段之间检查取消**。正在执行的阶段体无法被打断（它可能正在写文件），但**两个阶段
   之间**可以。所以取消是"下一阶段不再开始"，而不是"当前阶段被掐断"——后者会留下一个
   写了一半的产物，比不取消更糟。

为什么这里不直接调 ``run_document``
-----------------------------------
``run_document`` 是批量跑完的辅助入口（测试 / CLI 用），它把"逐个阶段"这件事封在里面，
于是调用方拿不到**逐阶段事件**、也没有**阶段之间的取消点**。服务端两样都要。
本模块因此自己循环、逐阶段调 ``run_stage``——而"断点复用 / 失败隔离"的判定仍然只有
``run_stage`` 一处实现，循环只是循环。
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Final, Literal

from docfidelity.contracts.config import ProjectConfig, RoutingConfig
from docfidelity.contracts.errors import EngineError
from docfidelity.contracts.job import (
    STAGE_NAMES,
    Artifact,
    Decision,
    Job,
    JobEvent,
    JobStatus,
    Stage,
    StageName,
)
from docfidelity.contracts.qa import Report
from docfidelity.pipeline import (
    EVENTS_FILENAME,
    STAGE_ORDER,
    ChildSpec,
    DecisionSession,
    EventLog,
    FileCheckpointStore,
    JobStateMachine,
    JsonlEventSink,
    ProjectLockManager,
    StageRegistry,
    Workspace,
    build_session,
    child_progress,
    path_segment,
    plan_resume,
    read_events,
    run_stage,
    submit_orchestration,
    with_aggregate,
)
from docfidelity.platform.fs import iter_files, relative_posix, safe_join
from docfidelity.platform.hashing import digest_payload, sha256_file

from ..settings import ServerSettings
from ..storage import Repository
from .events import WorkspaceEventLog, WorkspaceExecutionLocks, execution_pending
from .payload import freeze_payload, load_payload, persist_payload

__all__ = [
    "ARTIFACT_KIND_BY_SUFFIX",
    "REPORT_OUTPUT_KEY",
    "JobRunner",
    "RunOutcome",
    "SessionFactory",
    "WorkerLoop",
    "WorkspaceEventSink",
    "artifact_id_of",
    "artifact_kind_of",
]

SessionFactory = Callable[[Job, "RoutingConfig | None", EventLog], DecisionSession]
"""决策会话工厂：``(作业, 该项目的路由配置, 事件日志) -> 会话``。

**组合根（``server.api.deps``）是唯一该实现它的地方**：挂哪个适配层、用哪份基线、
要不要加落库镜像，都是部署决策，而 worker 只负责"问一次、用一次"。
把这三件事塞进 worker 会让"换一个推理服务"变成"改编排代码"——那正是
决策一「编排与渲染解耦」要避免的形态，只是换成了模型侧。

**事件日志为什么也在参数里**：决策只有落成事件才会进 SSE 流、断线重连与重启回放。
让它由工厂自己去造，就可能得到一个"决策确实发生了、而界面上从来看不见"的部署——
这种失联不报错、不失败，只是所有决策留痕静默地落进一个随用随丢的内存对象。
把它做成必填参数，让"忘记接上"变成签名上的显式动作。
"""


class WorkerLoop:
    """后台线程：把排队中的作业一直推进到空闲（DD-13.1 的 ``server/jobs``）。

    为什么是线程而不是 ``asyncio`` 任务：阶段体是同步的（读模板、写 docx、下 Office
    域刷新），放进事件循环会把 HTTP 服务一起卡住——表现是"构建一开始，所有接口都超时"。
    线程池里的阻塞，才不会被用户看见。

    空闲时 sleep 而不是忙等：一次构建要几十秒，0.5 秒的调度延迟完全可以接受，
    而忙等会让一个 4 核机器的一个核永远跑满。
    """

    def __init__(
        self,
        runner: JobRunner,
        *,
        idle_seconds: float = 0.5,
        stop_timeout_seconds: float = 5.0,
    ) -> None:
        self._runner = runner
        self._idle = idle_seconds
        self._stop_timeout = stop_timeout_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="docfidelity-worker", daemon=True)
        self._thread.start()

    def stop(self, *, timeout: float | None = None) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout if timeout is not None else self._stop_timeout)
        self._thread = None

    def tick(self) -> bool:
        """跑一轮；返回是否真的执行了一个作业（测试直接调它，避免碰线程）。"""
        try:
            return self._runner.run_once() is not None
        except Exception:  # 调度线程绝不能因单次异常退出
            logging.getLogger(__name__).exception("worker tick 失败（已隔离，不影响后续作业）")
            return False

    def _run(self) -> None:
        while not self._stop.is_set():
            if not self.tick():
                self._stop.wait(self._idle)


#: 产物种类（与 ``contracts.job.Artifact.kind`` 的闭集一致）。
ArtifactKind = Literal["docx", "pdf", "png", "report", "repro_package", "other"]

#: 产物后缀 → 契约 :class:`~docfidelity.contracts.job.Artifact` 的 ``kind``。
#: 写成闭集映射而不是 ``if suffix == ".docx"`` 分支：新增产物类型时只加一行，
#: 且未登记的后缀会落到 ``other`` 而不是被某个分支悄悄吞掉。
ARTIFACT_KIND_BY_SUFFIX: Final[dict[str, ArtifactKind]] = {
    ".docx": "docx",
    ".pdf": "pdf",
    ".png": "png",
    ".json": "report",
    ".zip": "repro_package",
}

#: 阶段输出里承载门禁报告的逻辑名（``qa`` 阶段写它，服务端据此落库）。
REPORT_OUTPUT_KEY: Final = "report"

#: 调度会认领的作业状态。``running`` 也在内——理由见 :meth:`JobRunner.run_once`。
#: 编排作业**不在**其中：它由子作业聚合出状态，没有自己的阶段可跑。
_PENDING_STATUSES: Final[frozenset[str]] = frozenset({"queued", "running"})
_DRAIN_STATUSES: Final[frozenset[str]] = frozenset(
    {"succeeded", "failed", "canceled", "awaiting_decision"}
)

_FALLBACK_ARTIFACT_KIND: Final[ArtifactKind] = "other"


def artifact_kind_of(suffix: str) -> ArtifactKind:
    """产物后缀 → ``kind``；未登记的后缀归 ``other``（**不报错**：产物是结果，
    不是输入，多出来一种不该让整个作业失败）。"""
    return ARTIFACT_KIND_BY_SUFFIX.get(suffix.lower(), _FALLBACK_ARTIFACT_KIND)


def artifact_id_of(job_id: str, relative_path: str) -> str:
    """产物标识。**URL 安全**、稳定、与 ``relative_path`` 一一对应。

    为什么不用 ``f"{job_id}:{relative_path}"``：``relative_path`` 含 ``/``，
    而取件端点是 ``GET /artifacts/{artifact_id}/download`` —— 路径参数**不跨** ``/``，
    于是 id 里带斜杠的产物永远匹配不上路由，表现为"产物列表里看得见、下载 404"。

    取相对路径的摘要而不是"把 ``/`` 换成别的字符"：后者会让 ``a/b`` 与 ``a~b``
    映射到同一个 id（``checkpoint.path_segment`` 的文档写过这类撞车的代价）。
    可读性由响应里的 ``relative_path`` 承担——id 只需要唯一且可放进 URL。
    """
    return f"a_{digest_payload({'job_id': job_id, 'relative_path': relative_path})[:16]}"


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


class WorkspaceEventSink:
    """按 ``job_id`` 路由到 ``jobs/<job_id>/events.jsonl``（DD-11.7 的落盘口径）。

    为什么需要这一层：``pipeline`` 的 :class:`~docfidelity.pipeline.JsonlEventSink`
    写**一个**文件，而 DD-11.7 把事件放在**每个作业自己的目录**下。适配器只做路由，
    文件格式、换行、ensure_dir 全部复用那份实现——两处各写一份必然漂移。
    """

    def __init__(self, root: Path | str) -> None:
        self._root = Path(root)

    def job_dir(self, job_id: str) -> Path:
        return safe_join(self._root, "jobs", path_segment(job_id, field="job_id"))

    def write(self, event: JobEvent) -> None:
        JsonlEventSink(self.job_dir(event.job_id) / EVENTS_FILENAME).write(event)


@dataclass(frozen=True)
class RunOutcome:
    """一次子作业执行的结果。"""

    job_id: str
    status: JobStatus
    ran: tuple[str, ...]
    reused: tuple[str, ...]
    artifact_ids: tuple[str, ...] = ()
    stopped_at: StageName | None = None
    #: 进裁决队列的决策数（低置信 / 兜底 / 需人工）。非零即说明 ``status`` 是
    #: ``awaiting_decision`` 而不是 ``succeeded``——产物照常可用，等的是裁决而非重跑。
    pending_decisions: int = 0

    @property
    def failed(self) -> bool:
        return self.status == "failed"


class JobRunner:
    """驱动子作业跑完阶段机，并把事件、产物、报告写回仓储。

    Parameters
    ----------
    repository:
        作业 / 阶段 / 产物 / 报告的落点。
    registry:
        阶段实现（接缝；本模块不认识任何具体阶段）。
    settings:
        保留策略与锁租约时长从这里取。
    root:
        工作区根目录（``pipeline.Workspace`` 的根）。
    events:
        事件日志；缺省新建一个只留在内存里的（测试友好）。
    locks:
        项目锁；缺省按 ``settings`` 的租约时长新建。
    now:
        取时函数；测试注入固定时钟。
    decide_session:
        决策会话工厂（见 :data:`SessionFactory`）。缺省建**确定性会话**：不接适配层、
        模型总开关关闭，全链路走规则默认值。缺省的这条路径**仍然读项目的路由配置**
        （`ProjectConfig.routing`），否则"配置只能收紧不能放宽"就只是文档里的一句话。
    """

    def __init__(
        self,
        *,
        repository: Repository,
        registry: StageRegistry,
        settings: ServerSettings,
        root: Path | str,
        events: EventLog | None = None,
        locks: ProjectLockManager | None = None,
        decide_session: SessionFactory | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._repository = repository
        self._registry = registry
        self._settings = settings
        self._root = Path(root)
        self._events = events if events is not None else WorkspaceEventLog(self._root)
        self._execution_locks = WorkspaceExecutionLocks(self._root)
        self._locks = locks or ProjectLockManager(ttl_seconds=settings.project_lock_ttl_seconds)
        self._decide_session = decide_session
        self._clock = now or _utcnow
        self._store = FileCheckpointStore(self._root)

    # ---------------------------------------------------------------- 只读面
    @property
    def events(self) -> EventLog:
        return self._events

    @property
    def locks(self) -> ProjectLockManager:
        return self._locks

    @property
    def registry(self) -> StageRegistry:
        return self._registry

    @property
    def root(self) -> Path:
        return self._root

    def workspace(self, job_id: str) -> Workspace:
        return Workspace(self._root, job_id)

    def execution_guard(self, job_id: str) -> AbstractContextManager[None]:
        """持锁完成恢复类操作，运行中的 worker 不允许被重置。"""
        return self._execution_locks.guard(job_id)

    def plan_resume(self, job_id: str) -> tuple[StageName, ...]:
        """该作业接下来**会真正执行**的阶段（界面"将从 X 阶段续跑"用它）。

        直接调 ``pipeline.plan_resume``：断点在什么情况下不可复用（版本变 / 输入变 /
        断点损坏）是 ``checkpoint.decide_reuse`` 的口径，重写一遍必然漂移。
        """
        job = self._repository.get_job(job_id)
        return plan_resume(job_id=job_id, store=self._store, inputs_hash=job.inputs_hash)

    def discard_checkpoints(self, job_id: str, stage: str | None = None) -> int:
        """丢弃断点（``restart`` / ``retry_stage`` 的实现基础，见 ``CheckpointStore``）。

        服务层不得直接持有 ``FileCheckpointStore``：断点的落点属于本模块的编排职责，
        漏出去之后"断点写在哪"就会有两个答案。
        """
        return self._store.discard(job_id, stage)

    def has_checkpoint(self, job_id: str, stage: str) -> bool:
        """该阶段是否已有断点（``resume`` 的"续跑点是否可达"判定用它）。"""
        return self._store.load(job_id, stage) is not None

    def restore_events(self) -> int:
        """用落盘事件重建内存游标，返回恢复的作业数（进程重启后调用）。

        事件必须**先落库再推送**（DD-13.4）——这一条的可观测后果就是：重启后
        ``Last-Event-ID`` 仍然有效。若不重建游标，客户端报"我收到 7 号"时服务端
        会从 1 号开始编，于是同一条事件有两个 id，续推语义彻底失效。

        **逐个作业隔离**：某一个作业的事件文件坏了，只让它自己的续推失效，
        其余作业照常恢复。整批一起失败会把"一个坏文件"放大成"全站重连都重头补推"，
        而调用方（应用启动）只能笼统地记一条警告，连是谁坏的都说不出来。
        """
        jobs_root = self._root / "jobs"
        if not jobs_root.is_dir():
            return 0
        restored = 0
        for directory in sorted(path for path in jobs_root.iterdir() if path.is_dir()):
            path = directory / EVENTS_FILENAME
            if not path.is_file():
                continue
            try:
                events = (
                    list(self._events.events(directory.name))
                    if isinstance(self._events, WorkspaceEventLog)
                    else read_events(path)
                )
            except EngineError:
                logging.getLogger(__name__).warning(
                    "事件文件损坏，该作业的重连补推不可用：%s", path, exc_info=True
                )
                continue
            if not events:
                continue
            if not isinstance(self._events, WorkspaceEventLog):
                self._events.restore(directory.name, events)
            restored += 1
        return restored

    # ---------------------------------------------------------------- 提交
    def submit(
        self,
        *,
        project_id: str,
        specs: Sequence[ChildSpec],
        inputs_hash: str,
        orchestration_id: str,
        created_at: datetime | None = None,
        initiated_by: str | None = None,
        config: ProjectConfig | None = None,
    ) -> tuple[Job, list[Job]]:
        """派生编排作业与子作业并落库（调度由 :meth:`run_once` 承担）。

        ``initiated_by`` 是**留痕**，不是判定输入：它不参与 ``inputs_hash``，
        故"谁点的按钮"变了不会让断点失效、也不会让同一份输入的构建变得不可复现。
        """
        moment = created_at or self._clock()
        orchestration, children = submit_orchestration(
            orchestration_id=orchestration_id,
            project_id=project_id,
            specs=specs,
            inputs_hash=inputs_hash,
            created_at=moment,
            initiated_by=initiated_by,
        )
        # 先落盘全部输入，再让排队作业对独立 worker 可见。
        for child in children:
            persist_payload(
                freeze_payload(
                    child,
                    repository=self._repository,
                    settings=self._settings,
                    events=self._events,
                    config=config,
                ),
                self.workspace(child.job_id),
            )
        self._repository.save_job(orchestration)
        for child in children:
            self._repository.save_job(child)
        self._events.emit(
            orchestration.job_id,
            "job",
            {
                "status": orchestration.status,
                "total": len(children),
                "child_job_ids": [child.job_id for child in children],
            },
        )
        return orchestration, children

    # ---------------------------------------------------------------- 调度
    def run_once(self) -> RunOutcome | None:
        """跑一个待推进的子作业；没有可跑的返回 ``None``。

        ``queued`` **与** ``running`` 都算"待推进"
        -----------------------------------------
        ``retry_stage`` / ``resume_from`` 把作业从 ``failed`` / ``canceled`` 迁到
        ``running``（见契约的迁移表），而不是 ``queued``——因为语义上它是"接着跑"，
        不是"重新排队"。若调度只看 ``queued``，这两个动作会把作业推进一个**没人认领的
        状态**：接口回 200、界面显示"运行中"，而 worker 永远不看它一眼。

        把 ``running`` 也算进来，靠**租约**区分"正在跑"与"待认领"：正常执行期间
        ``run_once`` 一直持着该项目租约，故另一个 worker 取不到；取得到，就说明
        没有人在跑它（刚被 retry / resume 重置，或上一个 worker 崩了）。

        FIFO 由 ``list_jobs`` 的排序保证。取不到项目锁的作业**保持原状态**并继续找
        下一个（见模块文档②）。
        """
        for job in self._repository.list_jobs():
            needs_drain = job.status in _DRAIN_STATUSES and execution_pending(
                self._events, job.job_id
            )
            if job.is_orchestration or (job.status not in _PENDING_STATUSES and not needs_drain):
                continue
            lease = self._locks.acquire(job.project_id, job.job_id, now=self._clock())
            if not lease.acquired:
                continue
            try:
                with self._execution_locks.acquire(job.job_id) as acquired:
                    if not acquired:
                        continue
                    current = self._repository.get_job(job.job_id)
                    if current.status in _DRAIN_STATUSES:
                        return self._drain_finished(current)
                    if current.status in _PENDING_STATUSES:
                        return self._run_child(current)
            finally:
                self._locks.release(job.project_id, job.job_id, now=self._clock())
        return None

    def run_until_idle(self, *, max_jobs: int = 256) -> list[RunOutcome]:
        """把当前排队的作业跑完（有上限，防测试里写出死循环）。"""
        outcomes: list[RunOutcome] = []
        for _ in range(max_jobs):
            outcome = self.run_once()
            if outcome is None:
                break
            outcomes.append(outcome)
        return outcomes

    # ------------------------------------------------------------ 执行一个
    def session_for(self, job: Job) -> DecisionSession:
        """该作业的决策会话（缺省确定性；适配层与基线由组合根注入）。

        缺省路径把 ``model_enabled`` 显式传 ``False``：总开关开着却**没有适配层**时，
        路由表会把归属显示成 ``small_model``，而每一次决策都在 ``_try_model`` 里回退
        ——"配置说该问模型、实际每次都退"这件事在归属视图里就看不出来了。断言"没接
        模型"是更接近真相的表达。

        项目路由配置照常传入：它是"配置只能收紧不能放宽"的落点，非法配置（试图把
        ``rule`` / ``human`` 两档放宽成模型路由）在这里就抛 ``E_CONFIG_INVALID``。
        """
        payload = self.build_payload(job)
        raw_routing = payload.get("routing")
        routing = RoutingConfig.model_validate(raw_routing) if raw_routing is not None else None
        factory = self._decide_session
        if factory is not None:
            return factory(job, routing, self._events)
        return build_session(
            routing,
            job_id=job.job_id,
            events=self._events,
            model_enabled=False,
        )

    def _run_child(self, job: Job) -> RunOutcome:
        running = self._start(job)
        if running.status == "canceled":
            return self._drain_finished(running)
        self._events.emit(job.job_id, "job", {"status": running.status, "execution_active": True})

        try:
            session = self.session_for(job)
        except EngineError as error:
            # 会话都建不起来（例如项目配置试图把某个决策点**放宽**成模型路由）时，
            # 正确的处置是让这一份作业失败并说明原因，而不是让调度器每轮抛一次同样的
            # 异常——后者在界面上表现为"作业一直卡在运行中"，日志被刷屏而作业不动。
            return self._fail_before_stages(running, error)

        stages: list[Stage] = []
        ran: list[str] = []
        reused: list[str] = []
        stopped_at: StageName | None = None

        for stage in STAGE_ORDER:
            if self._repository.get_job(job.job_id).status == "canceled":
                # 取消点的位置见模块文档③：阶段之间，而不是阶段内部。
                break
            self._events.emit_stage(job.job_id, stage, "running", 0.0)
            run = run_stage(
                job=running,
                stage=stage,
                registry=self._registry,
                store=self._store,
                workspace=self.workspace(job.job_id),
                inputs_hash=job.inputs_hash,
                payload=self.build_payload(running),
                decisions=session,
                now=self._clock,
            )
            stages.append(run.record())
            self._repository.save_stages(job.job_id, stages)
            for finding in run.findings:
                self._events.emit_finding(job.job_id, finding)
            self._events.emit_stage(
                job.job_id, stage, run.status, 1.0 if run.status != "failed" else 0.0
            )
            (reused if run.reused else ran).append(stage)
            if run.status == "failed":
                stopped_at = stage
                break

        pending = session.pending_decisions()
        artifacts = self._collect_artifacts(running)
        if stopped_at is None:
            self._store_report(running.job_id)
        settled = self._settle(running, stages, stopped_at, pending=pending)
        self._aggregate_parent(settled, has_pending_decision=bool(pending))
        self._events.emit(
            settled.job_id,
            "job",
            {
                "status": settled.status,
                "stopped_at": stopped_at,
                "pending_decisions": len(pending),
                "execution_active": False,
            },
        )

        return RunOutcome(
            job_id=settled.job_id,
            status=settled.status,
            ran=tuple(ran),
            reused=tuple(reused),
            artifact_ids=tuple(item.artifact_id for item in artifacts),
            stopped_at=stopped_at,
            pending_decisions=len(pending),
        )

    def _start(self, job: Job) -> Job:
        """把作业置为 ``running``。

        **已经在 ``running`` 的作业不重复迁移**：``retry_stage`` / ``resume_from``
        的结果就是 ``running``（契约迁移表），而 ``("running", "lease")`` 并不在表里
        ——硬套一次会抛 ``E_JOB_CONFLICT``，把一个正常的续跑变成一次冲突错误。
        状态已经是目标状态时，正确的动作是不动作。
        """
        job = self._repository.get_job(job.job_id)
        if job.status == "canceled":
            return job
        if job.status == "running":
            self._repository.save_job(job)
            return job
        moved = JobStateMachine.apply(job, "lease")
        self._repository.save_job(moved)
        return moved

    def _drain_finished(self, job: Job) -> RunOutcome:
        """恢复终态的中断执行，只登记已有文件，不调用任何阶段。"""
        artifacts = self._collect_artifacts(job)
        self._store_report(job.job_id)
        self._aggregate_parent(job)
        self._events.emit(job.job_id, "job", {"status": job.status, "execution_active": False})
        return RunOutcome(
            job_id=job.job_id,
            status=job.status,
            ran=(),
            reused=(),
            artifact_ids=tuple(item.artifact_id for item in artifacts),
        )

    def _settle(
        self,
        job: Job,
        stages: Sequence[Stage],
        stopped_at: StageName | None,
        *,
        pending: Sequence[Decision] = (),
    ) -> Job:
        """按阶段结果迁移作业状态（**经状态机**，不直接改 status）。

        待裁决项的处置是这里的要害：阶段都跑完了、产物也都出来了，但**存在低置信 /
        兜底 / 待人工的决策**时不能报 ``succeeded``——那等于宣布"这份产物可以定稿了"，
        而它其实在等人裁决（NFR-26 / 验收 39）。状态落成 ``awaiting_decision`` 之后，
        编排聚合、``/inbox``、界面上的待裁决徽标全都自然亮起来，不必再另加一条链路。
        """
        current = self._repository.get_job(job.job_id)
        if current.status == "canceled":
            return current
        if stopped_at is not None:
            action = "fail"
        elif pending:
            action = "await_decision"
        else:
            action = "succeed"
        moved = JobStateMachine.apply(current, action)
        if stopped_at is not None:
            failed = next((stage for stage in stages if stage.name == stopped_at), None)
            error = failed.error if failed is not None else None
            moved = moved.model_copy(update={"error": error})
        self._repository.save_job(moved)
        return moved

    def _fail_before_stages(self, job: Job, error: EngineError) -> RunOutcome:
        """阶段一步都没跑就失败的处置（目前只有"决策会话建不起来"这一种）。

        与 :meth:`_settle` 的 ``fail`` 分支同源：经状态机迁移、把结构化错误写进作业、
        发一条事件、并**重算编排聚合**——漏掉最后一步时，子作业已经失败而编排作业
        还停在"运行中"，界面上的进度会永远差一份。
        """
        job = self._repository.get_job(job.job_id)
        if job.status == "canceled":
            return self._drain_finished(job)
        moved = JobStateMachine.apply(job, "fail").model_copy(update={"error": error.to_info()})
        self._repository.save_job(moved)
        self._aggregate_parent(moved, has_pending_decision=False)
        self._events.emit(
            moved.job_id,
            "job",
            {
                "status": moved.status,
                "stopped_at": None,
                "error": error.code,
                "execution_active": False,
            },
        )
        return RunOutcome(job_id=moved.job_id, status=moved.status, ran=(), reused=())

    # ------------------------------------------------------------ 输入快照
    def build_payload(self, job: Job) -> Mapping[str, Any]:
        """冻结这次构建的输入快照（实现见 ``server.jobs.payload.freeze_payload``）。

        这里只做转发：本方法的调用方（``_run_child``、测试、CLI）要的是一个**稳定的
        入口**，而"哪些东西算这次构建的输入、怎么解析"是一块独立的职责——它的三条纪律
        （不只给标识 / 不抛异常 / 不自己拼资产路径）都写在那个模块的文档里。
        """
        workspace = self.workspace(job.job_id)
        frozen = load_payload(job, workspace)
        if frozen is not None:
            return frozen
        frozen = freeze_payload(
            job,
            repository=self._repository,
            settings=self._settings,
            events=self._events,
        )
        persist_payload(frozen, workspace)
        return frozen

    # ---------------------------------------------------------------- 产物
    def _collect_artifacts(self, job: Job) -> list[Artifact]:
        """把工作区 ``artifacts/`` 里的文件登记成产物。

        扫目录而不是让阶段体逐个上报：阶段体只要把文件放进 ``artifacts/`` 就够了，
        少一处"要记得登记"的手工步骤——那一步漏掉时，产物会静静地不可下载。
        """
        directory = self.workspace(job.job_id).artifacts_dir
        if not directory.is_dir():
            return []
        policy = self._settings.retention_policy()
        created: list[Artifact] = []
        existing = {item.relative_path for item in self._repository.list_artifacts(job.job_id)}
        for path in sorted(iter_files(directory)):
            relative = relative_posix(path, self.workspace(job.job_id).job_dir)
            if relative in existing:
                continue
            artifact = Artifact(
                artifact_id=artifact_id_of(job.job_id, relative),
                job_id=job.job_id,
                kind=artifact_kind_of(path.suffix),
                relative_path=relative,
                size_bytes=path.stat().st_size,
                sha256=sha256_file(path),
                created_at=self._clock(),
                expires_at=self._clock() + timedelta(days=policy.artifacts_days),
            )
            self._repository.add_artifact(artifact)
            self._events.emit(
                job.job_id,
                "artifact",
                {"artifact_id": artifact.artifact_id, "kind": artifact.kind},
            )
            created.append(artifact)
        current = self._repository.get_job(job.job_id)
        ids = [item.artifact_id for item in self._repository.list_artifacts(job.job_id)]
        self._repository.save_job(current.model_copy(update={"artifact_ids": ids}))
        return created

    # ---------------------------------------------------------------- 报告
    def _store_report(self, job_id: str) -> None:
        """若 ``qa`` 阶段产出了报告（输出逻辑名 ``report``），把它落库。

        报告是**元数据**：它读不出来不该让一份已经渲染好的文档变成"构建失败"。
        所以这里只发一条 ``log`` 事件，作业状态不动。
        """
        outputs = self._workspace_outputs(job_id)
        relative = outputs.get(REPORT_OUTPUT_KEY)
        if relative is None:
            return
        try:
            raw = self.workspace(job_id).read_text(relative)
            report = Report.model_validate_json(raw)
        except (OSError, ValueError) as exc:
            self._events.emit(
                job_id,
                "log",
                {
                    "warning": "report_unreadable",
                    "path": relative,
                    "detail": str(exc),
                },
            )
            return
        self._repository.put_report(job_id, report)

    def _workspace_outputs(self, job_id: str) -> dict[str, str]:
        """合并该作业各阶段已落盘的输出（逻辑名 → 相对路径）。

        从断点读而不是从内存读：续跑时上游阶段的输出只在断点里（本次没重跑），
        内存里那份是空的，只看内存会漏掉"上一轮产出的报告"。
        """
        merged: dict[str, str] = {}
        for stage in STAGE_NAMES:
            checkpoint = self._store.load(job_id, stage)
            if checkpoint is not None:
                merged.update(checkpoint.outputs)
        return merged

    # ------------------------------------------------------------ 编排聚合
    def _aggregate_parent(self, child: Job, *, has_pending_decision: bool = False) -> None:
        """子作业结束后重算编排作业的派生状态与进度（DD-11.2）。

        ``has_pending_decision`` 与子作业状态本身**同源但不同时机**：子作业刚被
        :meth:`_settle` 落成 ``awaiting_decision`` 时两者一致；而这一份跑完时另一份
        可能仍在待裁决，那种情形由 ``aggregate`` 从子作业状态里读出来。故这里显式传，
        让"这一次生成了待裁决项"不依赖上一步的写入是否已经生效。
        """
        if child.parent_job_id is None:
            return
        parent = self._repository.find_job(child.parent_job_id)
        if parent is None:
            return
        children = [
            found
            for job_id in parent.child_job_ids
            if (found := self._repository.find_job(job_id)) is not None
        ]
        if not children:
            return
        aggregated = with_aggregate(parent, children, has_pending_decision=has_pending_decision)
        self._repository.save_job(aggregated)
        progress = child_progress(children)
        self._events.emit(
            parent.job_id, "job", {**progress.model_dump(), "status": aggregated.status}
        )
