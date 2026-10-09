"""编排层：阶段机、断点、作业派生与聚合、事件、决策接缝（DD-11 / DD-12.3）。

``pipeline`` 是内核里最上面的一层：它知道"有哪些阶段、按什么顺序跑、失败了怎么办"，
但**不知道任何阶段内部在做什么**。阶段体由调用方注册（:class:`StageRegistry`），
服务端在启动时把 ``ingest`` / ``compose`` / ``render`` / ``refresh`` / ``qa`` 的实现接上。
这条界线的用处很实际——换渲染策略、在测试里塞一个必失败的阶段、在无 Word 的机器上
验证降级，都不必改编排代码（见 ``stages`` 模块文档）。

五件事，五个模块
----------------
===================  =========================================================
``checkpoint.py``    阶段断点与**复用判定**（``version`` + ``inputs_hash``）
``job.py``           作业状态机（唯一迁移入口）、编排派生/聚合、失败隔离、项目锁
``stages.py``        阶段定义与顺序、工作区、checkpoint 感知的阶段推进
``events.py``        事件日志（单调 id、JSONL 双写、按 ``Last-Event-ID`` 续推）
``decisions.py``     决策接缝：作业级会话、决策落成 ``event: decision``
``templates.py``     模板用例：``server`` 与 ``registry`` 之间的接缝
``profiles.py``      档案用例：登记校验（DD-7.5）、可满足性转发、反向引用；
                     领域模型注册表的装配（``domain_registry``，`model` 侧）
``sources.py``       事实源用例：配置期校验、试接入、条目浏览
``artifacts.py``     产物用例：两份产出的稳定化比对（P11），转 ``qa.compare``
===================  =========================================================

与 ``contracts.job`` 的分工
---------------------------
契约层定义**数据**（``Job`` / ``Stage`` / ``StageCheckpoint`` / ``JobEvent``）与
**状态迁移表**（``JOB_TRANSITIONS``）；本层定义**行为**（怎么迁移、怎么聚合、
怎么续跑）。两者不重叠：本层不新增字段，契约层不做判定。

三道"唯一接缝"（都由 ``.importlinter`` 的 forbidden 契约守着）
--------------------------------------------------------------
* ``decisions`` 是本层唯一认识 ``agents`` 的模块；
* ``templates`` 是本层唯一认识 ``registry`` 的模块；
* ``profiles`` / ``sources`` 是本层唯一处理档案与事实源的模块——前者转发
  ``compose.precheck`` 与 ``model.validation`` 并装配领域模型注册表，后者转发
  ``ingest``；
* ``artifacts`` 是本层唯一认识 ``qa`` 的模块（``qa.compare`` 的稳定化比对）。

``artifacts`` 与前三条有一个差别：前三条由 ``.importlinter`` 的 seam 契约逐一钉住
（"其余编排模块不得认识 registry / ingest / agents"），而它没有对应的禁止契约——
因为 ``qa`` 本来就在 ``pipeline`` 之下（``pipeline > agents > qa``），本层任何模块
都有权 import 它。把它单独放一个模块的理由不是"防止别人越过它"，而是**它的入参形状
与别的模块不同**（收文件路径、会抛 ``E_PACKAGE_INCOMPLETE``），混进编排模块会让
"这个函数会不会读磁盘"变成一件要翻实现才知道的事。

三者对称，理由也一样：DD-13.1 禁止 ``server`` 直接 import 内核实现，而 DD-2.1
又要求"界面需要的每一步能力都由 ``pipeline`` 暴露为用例函数"——于是这条缝隙必须
落在某一处，就只能落在**一个**模块里。散开之后，"服务端到底能碰内核的什么"
这个问题会变成一次全局搜索。

``profiles`` / ``sources`` 与另外两条的一个差别：它们不收"路径 + 落点"这类运行时
数据，收的是**声明**（``DocTypeProfile`` / ``SourceConfig``），并把外部数据
（已登记的领域类型、模板库现有引用、工作区根）当**纯参**接进来。所以这两条接缝
可以同时服务于"登记"与"构建前预检"两处调用，而不必让预检去够仓储。
"""

from __future__ import annotations

from .artifacts import (
    COMPARE_MODES,
    DiffMode,
    DocumentDiff,
    compare_documents,
)
from .checkpoint import (
    CHECKPOINT_SUFFIX,
    WORK_DIRNAME,
    CheckpointStore,
    CorruptCheckpoint,
    FileCheckpointStore,
    MemoryCheckpointStore,
    ReuseDecision,
    ReuseReason,
    decide_reuse,
    impl_version_of,
    path_segment,
)
from .decisions import (
    BASELINE_FILENAME,
    DECISION_EVENT_TYPE,
    RECORD_DECISION,
    RECORD_KIND_FIELD,
    RECORD_MODEL_CALL,
    BaselineStatus,
    DecisionOutcome,
    DecisionRecorder,
    DecisionSession,
    GrammarCapability,
    JournalRecorder,
    ModelAdapter,
    build_session,
    decisions_from_events,
    load_baseline_status,
    model_session_factory,
    pending_decisions_from_events,
    probe_grammar,
    rule_only_session,
)
from .events import (
    EVENTS_FILENAME,
    EventLog,
    EventSink,
    JsonlEventSink,
    read_events,
)
from .job import (
    ACTION_TRANSITIONS,
    CANCEL_ACTION,
    FAILURE_ISOLATION_RULE_ID,
    ORCHESTRATION_RULE_ID,
    RESUMABLE_ACTIONS,
    ChildSpec,
    JobAction,
    JobStateMachine,
    LockLease,
    LockOutcome,
    ProjectLockManager,
    actions_for,
    aggregate,
    child_progress,
    derive_children,
    rerun_failed,
    resolve_transition,
    submit_orchestration,
    with_aggregate,
)
from .profiles import (
    PROFILE_RULE_ID,
    PROFILE_SECTIONS,
    ProfileCheck,
    ProfileReference,
    RegisteredProfile,
    domain_registry,
    fingerprint_domain_model,
    fingerprint_profile,
    referencing_profiles,
    satisfiable,
    validate_profile,
)
from .sources import (
    DEFAULT_ITEM_LIMIT,
    SourceItems,
    SourceProbe,
    check_config,
    connector_names,
    load_items,
    probe_source,
)
from .stages import (
    ARTIFACTS_DIRNAME,
    CHECKPOINT_RULE_ID,
    INPUTS_DIRNAME,
    STAGE_ORDER,
    DocumentRun,
    StageContext,
    StageFunc,
    StageOutcome,
    StageRegistry,
    StageRun,
    Workspace,
    plan_resume,
    run_document,
    run_stage,
)
from .templates import (
    BLOCK_KINDS,
    ContractSummary,
    FieldDelta,
    ImportStage,
    RegisteredPackage,
    SectionChange,
    TemplateImportResult,
    VersionDiff,
    diff_packages,
    export_package,
    fingerprint_problems,
    import_template,
    read_overrides,
    read_package,
    register_package,
    summarize,
    verify_package,
    write_overrides,
)

__all__ = [
    "ACTION_TRANSITIONS",
    "ARTIFACTS_DIRNAME",
    "BASELINE_FILENAME",
    "BLOCK_KINDS",
    "CANCEL_ACTION",
    "CHECKPOINT_RULE_ID",
    "CHECKPOINT_SUFFIX",
    "COMPARE_MODES",
    "DECISION_EVENT_TYPE",
    "DEFAULT_ITEM_LIMIT",
    "EVENTS_FILENAME",
    "FAILURE_ISOLATION_RULE_ID",
    "INPUTS_DIRNAME",
    "ORCHESTRATION_RULE_ID",
    "PROFILE_RULE_ID",
    "PROFILE_SECTIONS",
    "RECORD_DECISION",
    "RECORD_KIND_FIELD",
    "RECORD_MODEL_CALL",
    "RESUMABLE_ACTIONS",
    "STAGE_ORDER",
    "WORK_DIRNAME",
    "BaselineStatus",
    "CheckpointStore",
    "ChildSpec",
    "ContractSummary",
    "CorruptCheckpoint",
    "DecisionOutcome",
    "DecisionRecorder",
    "DecisionSession",
    "DiffMode",
    "DocumentDiff",
    "DocumentRun",
    "EventLog",
    "EventSink",
    "FieldDelta",
    "FileCheckpointStore",
    "GrammarCapability",
    "ImportStage",
    "JobAction",
    "JobStateMachine",
    "JournalRecorder",
    "JsonlEventSink",
    "LockLease",
    "LockOutcome",
    "MemoryCheckpointStore",
    "ModelAdapter",
    "ProfileCheck",
    "ProfileReference",
    "ProjectLockManager",
    "RegisteredPackage",
    "RegisteredProfile",
    "ReuseDecision",
    "ReuseReason",
    "SectionChange",
    "SourceItems",
    "SourceProbe",
    "StageContext",
    "StageFunc",
    "StageOutcome",
    "StageRegistry",
    "StageRun",
    "TemplateImportResult",
    "VersionDiff",
    "Workspace",
    "actions_for",
    "aggregate",
    "build_session",
    "check_config",
    "child_progress",
    "compare_documents",
    "connector_names",
    "decide_reuse",
    "decisions_from_events",
    "derive_children",
    "diff_packages",
    "domain_registry",
    "export_package",
    "fingerprint_domain_model",
    "fingerprint_problems",
    "fingerprint_profile",
    "impl_version_of",
    "import_template",
    "load_baseline_status",
    "load_items",
    "model_session_factory",
    "path_segment",
    "pending_decisions_from_events",
    "plan_resume",
    "probe_grammar",
    "probe_source",
    "read_events",
    "read_overrides",
    "read_package",
    "referencing_profiles",
    "register_package",
    "rerun_failed",
    "resolve_transition",
    "rule_only_session",
    "run_document",
    "run_stage",
    "satisfiable",
    "submit_orchestration",
    "summarize",
    "validate_profile",
    "verify_package",
    "with_aggregate",
    "write_overrides",
]
