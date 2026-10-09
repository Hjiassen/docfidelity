"""用例层：HTTP 层只做请求校验，业务动作都在这里（DD-13.1）。

三条纪律
--------
1. **所有状态迁移都经 :class:`~docfidelity.pipeline.JobStateMachine`**。本模块从不
   ``job.model_copy(update={"status": ...})``——那是绕过迁移表。
2. **动作不合法要给出对得上 DD-13.2 的错误码**。内核统一抛 ``E_JOB_CONFLICT``，
   而 DD-13.2 要求 ``cancel`` 报 ``E_STATE_INVALID``、``retry`` 报 ``E_NOT_RESUMABLE``
   ——由 :func:`_apply_action` 做这一层翻译，并附上"当前可用动作"作为可操作动作。
   界面因此能直接照 ``remediation`` 渲染按钮，而不必自己推断"这个状态能干什么"。
3. **"从头重跑"必须真的重跑**。``restart`` 回到 ``queued`` 只是状态变了，续跑判定
   仍然看断点——不丢断点，``run_stage`` 会把每个阶段都复用掉，界面显示"重跑成功"
   而一次都没执行。所以本模块的 restart / retry_stage / resume 都会**先丢断点**。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Final

from docfidelity.contracts.config import (
    DocumentEntry,
    ProjectConfig,
)
from docfidelity.contracts.errors import EngineError, ErrorCode
from docfidelity.contracts.job import (
    STAGE_NAMES,
    ChildProgress,
    Job,
    JobStatus,
    Stage,
    StageName,
)
from docfidelity.contracts.qa import Report
from docfidelity.pipeline import (
    RESUMABLE_ACTIONS,
    ChildSpec,
    JobStateMachine,
    actions_for,
    child_progress,
    path_segment,
    rerun_failed,
    with_aggregate,
)
from docfidelity.platform.fs import ensure_dir
from docfidelity.platform.hashing import digest_payload, sha256_hex

from .adjudication import AdjudicationMixin
from .artifacts import ArtifactMixin
from .domain_models import DomainModelMixin
from .ids import new_id
from .jobs import JobRunner
from .maintenance import MUTABLE_SETTINGS_FIELDS, SECRET_SETTINGS_FIELDS, MaintenanceMixin
from .overview import DriftReport, ProjectOverview, build_drift_report, build_overview
from .precheck import PrecheckReport, ReferenceIndex, collect
from .profiles import ProfileMixin, resolve_profile
from .settings import ServerSettings, default_model_prober
from .sources import SourceMixin
from .storage import ConfigVersion, MemoryRepository, Project, Repository, UploadRecord
from .template_library import TemplateLibraryMixin
from .templates import TemplateImportMixin
from .uploads import (
    UPLOAD_PURPOSES,
    UploadProbe,
    UploadReceipt,
    validate_upload,
    zip_members,
)

__all__ = [
    "DEMO_TEMPLATE_REF",
    "MUTABLE_SETTINGS_FIELDS",
    "RETRY_MODES",
    "SECRET_SETTINGS_FIELDS",
    "Services",
    "new_id",
]

#: 演示项目使用的模板包引用与档案标识（``demo-init`` 用）。
#: **只是取值**，不带任何文档类型语义——引擎不认识它们，是样例数据的一部分。
DEMO_TEMPLATE_REF: Final = "demo-package@1"
#: 演示配置沿用裸标识；实际配置也可用 ``profile_id@version`` 锁定版本。
DEMO_PROFILE_ID: Final = "demo-profile"
DEMO_DOCUMENT_KEYS: Final[tuple[tuple[str, str], ...]] = (
    ("overview", "总览"),
    ("detail", "明细"),
)

#: ``POST /jobs/{jid}/retry?mode=`` 的两种模式。
#: ``stage`` = 只重跑失败阶段（断点复用上游）；``full`` = 从头重跑（丢弃全部断点）。
RETRY_MODES: Final[frozenset[str]] = frozenset({"stage", "full"})


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


@dataclass
class Services(
    MaintenanceMixin,
    AdjudicationMixin,
    TemplateImportMixin,
    TemplateLibraryMixin,
    ProfileMixin,
    DomainModelMixin,
    SourceMixin,
    ArtifactMixin,
):
    """用例面。HTTP 层只注入这一个对象。

    设置与保留清理在 :class:`server.maintenance.MaintenanceMixin`，
    裁决与待办聚合在 :class:`server.adjudication.AdjudicationMixin`，
    模板导入流水线在 :class:`server.templates.TemplateImportMixin`，
    模板库的读取与治理在 :class:`server.template_library.TemplateLibraryMixin`，
    档案库在 :class:`server.profiles.ProfileMixin`，
    领域模型库在 :class:`server.domain_models.DomainModelMixin`，
    事实源声明与接入在 :class:`server.sources.SourceMixin`，
    产物聚合 / 比对 / 整包导出在 :class:`server.artifacts.ArtifactMixin`
    （同一对象，另八个文件——单文件行数上限，见 NFR-09）。

    八个 mixin **只用** ``repository`` / ``runner`` / ``settings`` / ``now``
    （``DomainModelMixin`` 只用到其中两个：领域模型没有磁盘本体，故不认识 ``settings``），
    故"同一个 Services 对象"这件事在九个文件之间都成立；拆分的分界线是"哪一类端点"，
    不是"哪一层状态"。方法名在各 mixin 之间**不得重名**——重名会被 dataclass 的
    MRO 悄悄选一个，而那种错在调用处看不出来。

    唯一会**跨域**的两个用例（项目总览 P02 / 漂移 P14）留在本类上，而不是做成第八个
    mixin：它们要同时读配置、事实源、作业、待裁决与前置检查，做成 mixin 就得伸手调
    兄弟 mixin，而上面那条"只用宿主字段"的边界正是拆分的依据。它们的判定逻辑在
    :mod:`server.overview` 里，以纯函数形式存在（收已取好的结果），本类只负责组装。
    """

    repository: Repository
    runner: JobRunner
    settings: ServerSettings
    now: Callable[[], datetime] = _utcnow
    model_prober: Callable[[str], tuple[bool, str]] = default_model_prober

    # ================================================================ 项目
    def create_project(self, *, name: str, demo: bool = False) -> Project:
        moment = self.now()
        project = Project(
            project_id=new_id("p_"),
            name=name,
            created_at=moment,
            updated_at=moment,
            demo=demo,
        )
        return self.repository.add_project(project)

    def list_projects(self, *, include_archived: bool = False) -> list[Project]:
        return self.repository.list_projects(include_archived=include_archived)

    def get_project(self, project_id: str) -> Project:
        return self.repository.get_project(project_id)

    def rename_project(self, project_id: str, name: str) -> Project:
        found = self.repository.get_project(project_id)
        return self.repository.save_project(
            Project(
                project_id=found.project_id,
                name=name,
                created_at=found.created_at,
                updated_at=self.now(),
                archived=found.archived,
                demo=found.demo,
            )
        )

    def set_archived(self, project_id: str, *, archived: bool) -> Project:
        found = self.repository.get_project(project_id)
        return self.repository.save_project(
            Project(
                project_id=found.project_id,
                name=found.name,
                created_at=found.created_at,
                updated_at=self.now(),
                archived=archived,
                demo=found.demo,
            )
        )

    # ================================================================ 配置
    def get_config(self, project_id: str) -> ProjectConfig:
        """取最新配置；项目不存在报 ``E_NOT_FOUND``，项目存在但无配置报 ``E_CONFIG_INVALID``。

        两者必须是不同的码："项目写错了"和"项目还没配"的可操作动作完全不同
        （前者改标识，后者去初始化）。
        """
        self.repository.get_project(project_id)
        config = self.repository.latest_config(project_id)
        if config is None:
            raise EngineError(
                ErrorCode.CONFIG_INVALID,
                f"项目尚未登记配置：{project_id}",
                where={"project_id": project_id},
                remediation=[
                    "POST /api/v1/projects/{pid}/demo-init 初始化演示配置",
                    "或在该项目下登记文档条目后再发起构建",
                ],
            )
        return config

    def list_config_versions(self, project_id: str) -> list[ConfigVersion]:
        self.repository.get_project(project_id)
        return self.repository.list_configs(project_id)

    def config_version(self, project_id: str, version: int) -> ProjectConfig:
        """读**指定版本**的配置（P07 的「变更留痕」：查看某一版当时是什么）。

        与 :meth:`get_config`（读最新）分开：历史版本的用处是"当时是什么样"，
        界面拿它做两件事——以它为基础重新编辑（回滚 = 以历史版本为内容再写一版，
        见 ``ProjectConfig.version`` 的说明），以及跟当前版本对照。

        版本不存在报 ``E_NOT_FOUND`` 而不是 ``E_CONFIG_INVALID``：请求的是一个**具体
        对象**，"它不在库里"与"这个项目还没配"是两件要给出不同动作的事。
        """
        self.repository.get_project(project_id)
        found = self.repository.get_config(project_id, version)
        if found is None:
            raise EngineError(
                ErrorCode.NOT_FOUND,
                f"找不到配置版本：{project_id} v{version}",
                where={"project_id": project_id, "version": version},
                remediation=["列出该项目的配置版本后重试"],
            )
        return found.config

    def demo_init(self, project_id: str) -> ProjectConfig:
        """初始化演示项目（DD-13.2 的 ``demo-init``，**幂等**：已有配置则原样返回）。"""
        project = self.repository.get_project(project_id)
        existing = self.repository.latest_config(project_id)
        if existing is not None:
            return existing
        config = ProjectConfig(
            project_id=project_id,
            author="demo-init",
            documents=[
                DocumentEntry(
                    doc_key=key,
                    title=title,
                    profile_id=DEMO_PROFILE_ID,
                    template_ref=DEMO_TEMPLATE_REF,
                    source_ids=[],
                )
                for key, title in DEMO_DOCUMENT_KEYS
            ],
        )
        entry = self.repository.append_config(
            ConfigVersion(
                project_id=project_id,
                version=1,
                config=config,
                author="demo-init",
                created_at=self.now(),
            )
        )
        if not project.demo:
            self.repository.save_project(
                Project(
                    project_id=project.project_id,
                    name=project.name,
                    created_at=project.created_at,
                    updated_at=self.now(),
                    archived=project.archived,
                    demo=True,
                )
            )
        return entry.config

    def put_config(
        self, project_id: str, config: ProjectConfig, *, author: str | None
    ) -> ConfigVersion:
        """写入配置的**新版本**（DD-13.6：不覆盖历史），返回带版本号的留痕条目。"""
        self.repository.get_project(project_id)
        if config.project_id != project_id:
            raise EngineError(
                ErrorCode.CONFIG_INVALID,
                "配置体里的 project_id 与路径不一致",
                where={"path_project_id": project_id, "body_project_id": config.project_id},
                remediation=["让请求体与路径指向同一个项目"],
            )
        return self.repository.append_config(
            ConfigVersion(
                project_id=project_id,
                version=config.version,
                config=config,
                author=author,
                created_at=self.now(),
            )
        )

    # ================================================================ 上传
    def accept_upload(
        self, *, filename: str, payload: bytes, purpose: str = "template"
    ) -> UploadRecord:
        """校验并落盘（DD-13.5）。校验口径在 :mod:`server.uploads`，此处只负责落下。

        ``purpose`` 取闭集：它只用于留痕，但自由字符串会让"模板包"与"模板包 "在留痕里
        变成两类，而审计要按它筛。
        """
        if purpose not in UPLOAD_PURPOSES:
            raise EngineError(
                ErrorCode.UPLOAD_TYPE,
                f"未知的上传用途：{purpose!r}",
                where={"purpose": purpose, "allowed": sorted(UPLOAD_PURPOSES)},
                remediation=["purpose 取 template / profile / reference"],
            )
        probe: UploadProbe = UploadProbe(
            filename=filename,
            size=len(payload),
            head=payload[:8],
            digest=sha256_hex(payload),
            # 成员名必须真的读出来：第④道宏闸门全靠它，留空等于那道闸门不存在。
            members=zip_members(payload),
        )
        receipt: UploadReceipt = validate_upload(probe, self.settings)
        upload_id = new_id("u_")
        target = ensure_dir(self.settings.uploads_root / upload_id) / filename
        target.write_bytes(payload)
        return self.repository.add_upload(
            UploadRecord(
                upload_id=upload_id,
                receipt=receipt,
                # 存**绝对**路径：``ensure_within`` 接受成形的路径，喂相对片段会被按
                # 工作目录解析而误判越界（``platform.fs`` 的两者分工见 DISCIPLINE）。
                stored_path=str(target),
                created_at=self.now(),
                purpose=purpose,
            )
        )

    def list_uploads(self) -> list[UploadRecord]:
        return self.repository.list_uploads()

    # ================================================================ 构建
    def precheck(self, project_id: str, *, doc_keys: Sequence[str] | None = None) -> PrecheckReport:
        """构建前置检查（DD-13.2 的 ``config/validate``；P08 的"为什么不能发起"）。

        **不抛错**：调用方有两种需求——``submit_build`` 要据此拒绝，界面要据此渲染
        "只差哪一项"。判据只有一份（:mod:`server.precheck`），本方法只负责把
        "项目 / 配置 / 阶段注册表 / 已登记引用"取齐再交出去。

        已登记引用由**本方法**从仓储读好后以纯参传入（``ReferenceIndex``），
        而不是让 ``precheck`` 自己去够仓储：那份判据要被 ``submit_build`` 与
        ``POST /config/validate`` 两处共用，而"怎么取数"在两边可能不同
        （测试里的仓储是内存实现，嵌入方可能给别的）。取数与判据分开之后，
        两处共用的是判据本身。
        """
        self.repository.get_project(project_id)
        config = self.get_config(project_id)
        registry = self.runner.registry
        entries = config.enabled_documents()
        return collect(
            project_id=project_id,
            entries=entries,
            selected=doc_keys,
            registered_stages=registry.registered(),
            missing_stages=registry.missing(),
            refs=self._reference_index(project_id, entries),
        )

    def _reference_index(self, project_id: str, entries: Sequence[DocumentEntry]) -> ReferenceIndex:
        """已登记引用的快照（见 :class:`server.precheck.ReferenceIndex`）。

        档案引用与冻结输入共用 ``resolve_profile``：版本引用精确匹配，裸标识
        取最新未废弃版本。索引保留配置原引用，避免两处分别推断版本策略。
        """
        return ReferenceIndex(
            templates=frozenset(entry.ref for entry in self.repository.list_templates()),
            profiles=frozenset(
                entry.profile_id
                for entry in entries
                if entry.profile_id
                and resolve_profile(self.repository, entry.profile_id) is not None
            ),
            sources=frozenset(
                entry.source_id for entry in self.repository.list_sources(project_id)
            ),
        )

    def submit_build(
        self,
        project_id: str,
        *,
        doc_keys: Sequence[str] | None = None,
        author: str | None = None,
    ) -> Job:
        """发起构建（DD-13.2 的 ``POST /projects/{pid}/jobs``）。

        前置检查是**轻量**的（DD-13.2：配置必填 / 模板包存在 / 事实源非空 / 档案可满足）：
        只查"能不能开工"，不解析模板内容——那要花几秒到几十秒，属于流水线的事。
        判据与 ``POST /config/validate`` **共用** :mod:`server.precheck` 那一份；
        两处各写一份的症状是"界面说可以发起、提交却被拒"。

        ``author`` 只落成留痕（``Job.initiated_by``），**不进** ``inputs_hash``：
        "谁点的按钮"变了不该让断点失效，也不该让同一份输入的构建变得不可复现。
        """
        report = self.precheck(project_id, doc_keys=doc_keys)
        if not report.ok:
            raise report.as_error()
        config = self.get_config(project_id)
        chosen = set(report.doc_keys)
        entries = [entry for entry in config.enabled_documents() if entry.doc_key in chosen]

        orchestration_id = new_id("j_")
        specs = [
            ChildSpec(
                # 子作业 id 会被直接当成目录名（``jobs/<job_id>/``），所以分隔符不能取
                # ``:``（Windows 上非法，见 ``pipeline.checkpoint.path_segment``）。
                # ``doc_key`` 来自配置、内容不可控，故逐个校验而不是"拼完再说"。
                job_id=f"{orchestration_id}.{path_segment(entry.doc_key, field='doc_key')}",
                doc_key=entry.doc_key,
                doc_type=None,
            )
            for entry in entries
        ]
        inputs_hash = digest_payload(
            {
                "project_id": project_id,
                "config_version": config.version,
                "documents": [
                    {
                        "doc_key": entry.doc_key,
                        "template_ref": entry.template_ref,
                        "profile_id": entry.profile_id,
                        "source_ids": list(entry.source_ids),
                        "overrides_ref": entry.overrides_ref,
                    }
                    for entry in entries
                ],
            }
        )
        orchestration, _ = self.runner.submit(
            project_id=project_id,
            specs=specs,
            inputs_hash=inputs_hash,
            orchestration_id=orchestration_id,
            initiated_by=author,
            config=config,
        )
        return orchestration

    def list_jobs(
        self,
        *,
        project_id: str | None = None,
        status: JobStatus | None = None,
        kind: str | None = None,
    ) -> list[Job]:
        return self.repository.list_jobs(project_id=project_id, status=status, kind=kind)

    def get_job(self, job_id: str) -> Job:
        return self.repository.get_job(job_id)

    def job_stages(self, job_id: str) -> list[Stage]:
        self.repository.get_job(job_id)
        return self.repository.get_stages(job_id)

    def job_children(self, job_id: str) -> list[Job]:
        job = self.repository.get_job(job_id)
        found = [
            child
            for child_id in job.child_job_ids
            if (child := self.repository.find_job(child_id)) is not None
        ]
        return found

    def job_progress(self, job_id: str) -> ChildProgress | None:
        children = self.job_children(job_id)
        return child_progress(children) if children else None

    def resume_plan(self, job_id: str) -> tuple[StageName, ...]:
        job = self.repository.get_job(job_id)
        if job.status == "succeeded":
            return ()
        if job.is_orchestration:
            # 编排作业**自身不推进阶段**（阶段在各子作业里，见 ``jobs.worker`` 的调度：
            # 它只认领 ``kind == "document"``）。不特判就会算出"五个阶段全都要跑"，
            # 于是界面对一个**已经完成**的编排作业显示"将从 ingest → … 继续"——
            # 那句话是错的，而且错得让人以为还得再跑一遍。
            return ()
        return self.runner.plan_resume(job_id)

    # ------------------------------------------------------------ 生命周期
    def cancel(self, job_id: str) -> Job:
        """取消。编排作业连子作业一起取消（能取消的那些）。

        **取消后不重新聚合编排状态**：``cancel`` 是显式动作，DD-11.2 允许它从
        ``running`` / ``partial_failure`` 直达 ``canceled``。若取消后再按子作业聚合，
        一份早已 ``succeeded`` 的子作业会把父作业拉回 ``partial_failure``——
        用户点了"取消"，状态却弹回"部分失败"。故这里只同步 ``progress``。
        """
        job = self.repository.get_job(job_id)
        if job.is_orchestration:
            for child in self.job_children(job_id):
                self._try_apply(child, "cancel")
        moved = self._apply(job, "cancel", code=ErrorCode.STATE_INVALID)
        if moved.is_orchestration:
            children = self.job_children(moved.job_id)
            if children:
                moved = self.repository.save_job(
                    moved.model_copy(update={"progress": child_progress(children)})
                )
            return moved
        return self._recompute_parent(moved)

    def retry(self, job_id: str, *, mode: str = "stage") -> Job:
        """重试（DD-13.2 的 ``?mode=``）：``stage`` 只重跑失败阶段，``full`` 从头重跑。"""
        if mode not in RETRY_MODES:
            raise EngineError(
                ErrorCode.CONFIG_INVALID,
                f"未知的重试模式：{mode!r}",
                where={"mode": mode, "allowed": sorted(RETRY_MODES)},
                remediation=["用 mode=stage（只重跑失败阶段）或 mode=full（从头重跑）"],
            )
        with self.runner.execution_guard(job_id):
            job = self.repository.get_job(job_id)
            action = "restart" if mode == "full" else "retry_stage"
            moved = self._transition(job, action, code=ErrorCode.NOT_RESUMABLE)
            if mode == "full":
                self.runner.discard_checkpoints(job_id)
            else:
                failed = self._failed_stage(job_id)
                if failed is not None:
                    self.runner.discard_checkpoints(job_id, failed)
            self._publish_transition(moved, action)
            return self._recompute_parent(moved)

    def resume(self, job_id: str, *, from_stage: StageName | None = None) -> Job:
        """从指定阶段续跑（DD-13.2 的 ``POST /jobs/{jid}/resume``）。

        ``from_stage`` 为空 = 从第一个不可复用的阶段开始（"接着上次的地方跑"）。
        指定了阶段时，**上游断点必须齐备**——否则"从 X 续跑"会静默退化成"从头跑"，
        而那正是用户最不想发生的事（他选的正是"别重跑前面"）。
        """
        with self.runner.execution_guard(job_id):
            job = self.repository.get_job(job_id)
            if from_stage is not None and from_stage not in STAGE_NAMES:
                raise EngineError(
                    ErrorCode.NOT_FOUND,
                    f"未知阶段：{from_stage!r}",
                    where={"stage": from_stage, "known": list(STAGE_NAMES)},
                    remediation=["阶段名取自 STAGE_NAMES，不得自创"],
                )
            if from_stage is not None:
                self._require_upstream_checkpoints(job_id, from_stage)
            moved = self._transition(job, "resume_from", code=ErrorCode.NOT_RESUMABLE)
            if from_stage is None:
                pending = self.runner.plan_resume(job_id)
                if pending:
                    self.runner.discard_checkpoints(job_id, pending[0])
            else:
                # 续跑点及其下游一律丢断点：续跑点的语义就是"从这里重来"。
                for stage in STAGE_NAMES[STAGE_NAMES.index(from_stage) :]:
                    self.runner.discard_checkpoints(job_id, stage)
            self._publish_transition(moved, "resume_from")
            return self._recompute_parent(moved)

    def rerun_failed(self, orchestration_id: str) -> list[Job]:
        """仅重跑失败子作业（DD-11.4）。已成功的子作业**产物与断点都不动**。"""
        job = self.repository.get_job(orchestration_id)
        if not job.is_orchestration:
            raise EngineError(
                ErrorCode.STATE_INVALID,
                f"作业 {orchestration_id} 不是编排作业，没有子作业可局部重跑",
                where={"job_id": orchestration_id, "kind": "document"},
                remediation=["对单个文档子作业请用 retry 或 resume"],
            )
        children = self.job_children(orchestration_id)
        with ExitStack() as stack:
            # 全部取得锁后再重读状态，避免候选快照把已完成子作业重新置回排队。
            for child in sorted(children, key=lambda item: item.job_id):
                stack.enter_context(self.runner.execution_guard(child.job_id))
            reset = rerun_failed(self.job_children(orchestration_id))
            for child in reset:
                self.runner.discard_checkpoints(child.job_id)
                self.repository.save_job(child)
            if reset:
                self._recompute(orchestration_id)
            return reset

    def _failed_stage(self, job_id: str) -> StageName | None:
        failed = [stage for stage in self.repository.get_stages(job_id) if stage.status == "failed"]
        return failed[-1].name if failed else None

    def _require_upstream_checkpoints(self, job_id: str, from_stage: StageName) -> None:
        index = STAGE_NAMES.index(from_stage)
        missing = [
            stage for stage in STAGE_NAMES[:index] if not self.runner.has_checkpoint(job_id, stage)
        ]
        if missing:
            raise EngineError(
                ErrorCode.CHECKPOINT_MISSING,
                f"无法从 {from_stage} 续跑：上游阶段缺少断点 {missing}",
                where={"job_id": job_id, "from_stage": from_stage, "missing": missing},
                remediation=[
                    "改用从头重跑（POST /jobs/{jid}/retry?mode=full）",
                    "或从更早的阶段续跑",
                ],
            )

    def _transition(self, job: Job, action: str, *, code: str) -> Job:
        try:
            return JobStateMachine.apply(job, action)
        except EngineError as exc:
            raise self._translate(exc, job, action, code) from exc

    def _publish_transition(self, moved: Job, action: str) -> Job:
        self.repository.save_job(moved)
        self.runner.events.emit(moved.job_id, "job", {"status": moved.status, "action": action})
        return moved

    def _apply(self, job: Job, action: str, *, code: str) -> Job:
        return self._publish_transition(self._transition(job, action, code=code), action)

    def _try_apply(self, job: Job, action: str) -> Job | None:
        """尽力而为地施加迁移；不合法就跳过（用于"取消编排作业"时逐个取消子作业）。"""
        try:
            moved = JobStateMachine.apply(job, action)
        except EngineError:
            return None
        self.repository.save_job(moved)
        self.runner.events.emit(moved.job_id, "job", {"status": moved.status, "action": action})
        return moved

    @staticmethod
    def _translate(exc: EngineError, job: Job, action: str, code: str) -> EngineError:
        """把内核的 ``E_JOB_CONFLICT`` 翻成 DD-13.2 给该端点规定的错误码。"""
        if exc.code != ErrorCode.JOB_CONFLICT:
            return exc
        return EngineError(
            code,
            f"作业 {job.job_id} 当前状态（{job.status}）不能执行动作 {action!r}",
            where={
                "job_id": job.job_id,
                "status": job.status,
                "action": action,
                "allowed_actions": list(actions_for(job.status)),
                "resumable_actions": sorted(RESUMABLE_ACTIONS),
            },
            remediation=[
                f"当前可用的动作：{list(actions_for(job.status))}",
                "若确需重跑已完成的作业，请新建一次构建",
            ],
        )

    def _recompute(self, job_id: str) -> Job:
        """重算派生状态（DD-11.2：派生值只有 ``pipeline.with_aggregate`` 一处实现）。

        编排作业 → 按子作业聚合；文档子作业 → 上溯到它的父作业。
        """
        job = self.repository.get_job(job_id)
        if not job.is_orchestration:
            return self._recompute_parent(job)
        children = self.job_children(job.job_id)
        if not children:
            return job
        aggregated = self.repository.save_job(with_aggregate(job, children))
        progress = aggregated.progress.model_dump() if aggregated.progress is not None else {}
        self.runner.events.emit(aggregated.job_id, "job", {**progress, "status": aggregated.status})
        return aggregated

    def _recompute_parent(self, child: Job) -> Job:
        if child.parent_job_id is None:
            return child
        if self.repository.find_job(child.parent_job_id) is None:
            return child
        return self._recompute(child.parent_job_id)

    # ============================================================ 产物与报告
    def project_overview(self, project_id: str) -> ProjectOverview:
        """项目总览（P02 的 ``GET /projects/{pid}/overview``）。

        这是**唯一**会跨域取数的用例：它把配置、事实源、作业、待裁决与前置检查收在
        一屏。判定逻辑在 :func:`server.overview.build_overview`（纯计算），
        本方法只负责把两处已经算好的结论取过来——``pending`` 与 ``precheck`` 都走
        各自已有的唯一实现，本方法不重算一遍。

        **没有配置时不调** ``precheck``：这条端点的用处正是回答"这个项目现在该做什么"，
        而一个刚建好、还没配过文档的项目恰恰是最需要它的时候。``precheck`` 在没配置时
        报 ``E_CONFIG_INVALID``（那份判据要给"发起构建"用，拒绝是对的），把它原样带上来
        会让总览页——也就是那条引导的入口——直接变成一张错误页。故先问一次"配了吗"，
        没有就交 ``None`` 出去（形状上 ``precheck`` 本来就可空，界面按"这一步没有可检查
        的对象"渲染）。
        """
        self.repository.get_project(project_id)
        has_config = self.repository.latest_config(project_id) is not None
        return build_overview(
            repository=self.repository,
            project_id=project_id,
            pending=self.inbox(project_id=project_id),
            precheck=self.precheck(project_id) if has_config else None,
        )

    def drift_report(self, project_id: str) -> DriftReport:
        """漂移清单（P14 的 ``GET /projects/{pid}/drift``）。

        判据与"事实级漂移还判不了"的说明见 :mod:`server.overview`。
        """
        return build_drift_report(repository=self.repository, project_id=project_id)

    def get_report(self, job_id: str) -> Report:
        self.repository.get_job(job_id)
        report = self.repository.find_report(job_id)
        if report is None:
            raise EngineError(
                ErrorCode.REPORT_MISSING,
                f"作业尚未产出报告：{job_id}",
                where={"job_id": job_id},
                remediation=[
                    "等待 qa 阶段执行完成",
                    "或重跑门禁阶段（POST /jobs/{jid}/retry?mode=stage）",
                ],
            )
        return report


def default_repository() -> MemoryRepository:
    """默认仓储（便于应用工厂与测试共用同一构造口径）。"""
    return MemoryRepository()
