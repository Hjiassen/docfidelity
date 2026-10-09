"""``server`` 轮的夹具（测试专用）。

阶段体为什么"真写文件"
----------------------
``server.jobs.JobRunner`` 的两处产物来自**扫描工作区**：产物登记读
``artifacts/`` 目录、门禁报告读阶段输出里的 ``report`` 逻辑名。若阶段体只返回
假路径，这两条路径就永远验不到——而它们恰恰是"产物能下载、报告能读"的前提。

时钟为什么可变
--------------
保留策略（过期 = ``created_at + N 天``）与项目锁租约都靠"时间往前走"才触发。
固定时钟只能证明"不过期"，证明不了"过期后会被回收"。故 :class:`MutableClock`
提供 :meth:`~MutableClock.advance`，让过期成为一次显式动作而不是 ``sleep``。

本目录在类型无关检索的白名单里，故夹具里出现中文说明文字是允许的。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from docfidelity.agents import DecisionOutcome
from docfidelity.contracts.config import DocumentEntry, ProjectConfig
from docfidelity.contracts.domain import DomainModel
from docfidelity.contracts.errors import EngineErrorInfo, ErrorCode
from docfidelity.contracts.qa import Finding, Location, Report, summarize, verdict_of
from docfidelity.pipeline import (
    ARTIFACTS_DIRNAME,
    STAGE_ORDER,
    WORK_DIRNAME,
    EventLog,
    StageContext,
    StageOutcome,
    StageRegistry,
)

if TYPE_CHECKING:
    # 只在类型注解里出现（``from __future__ import annotations`` 让注解不求值），
    # 故运行期不引入 HTTP 栈——本夹具模块仍可被纯用例层测试导入。
    from fastapi.testclient import TestClient
from docfidelity.qa import assign_finding_ids
from server.jobs import JobRunner, RunOutcome, SessionFactory, WorkspaceEventSink
from server.services import DEMO_PROFILE_ID, DEMO_TEMPLATE_REF, Services
from server.settings import ServerSettings
from server.storage import (
    MemoryRepository,
    RegisteredProfile,
    RegisteredTemplate,
    Repository,
)

__all__ = [
    "DEMO_DOC_KEYS",
    "FIXED_NOW",
    "ArtifactStage",
    "Harness",
    "MutableClock",
    "build_harness",
    "build_registry",
    "document_entry",
    "finding",
    "project_config",
    "seed_demo_assets",
    "seed_domain_models",
    "seed_domain_models_via_http",
]

#: 固定起点时刻（内核不用 ``random`` 参与逻辑，时间戳只进元数据）。
FIXED_NOW = datetime(2026, 9, 22, 9, 0, 0, tzinfo=timezone.utc)

#: 演示项目里会被构建的文档键（与 ``services.DEMO_DOCUMENT_KEYS`` 对齐）。
DEMO_DOC_KEYS = ("overview", "detail")


@dataclass
class MutableClock:
    """可推进的时钟，可当零参函数直接喂给 ``now=``。"""

    moment: datetime = FIXED_NOW

    def __call__(self) -> datetime:
        return self.moment

    def advance(self, **delta: float) -> datetime:
        self.moment = self.moment + timedelta(**delta)
        return self.moment


def finding(rule_id: str = "FR-M5-01", severity: str = "minor") -> Finding:
    """一条最小 finding（门禁报告的载荷）。"""
    return Finding(
        rule_id=rule_id,
        severity=severity,  # type: ignore[arg-type]
        message=f"{rule_id} 命中",
        location=Location(chapter_path=["1", "总览"]),
    )


def document_entry(
    doc_key: str,
    *,
    title: str | None = None,
    profile_id: str = "demo-profile",
    template_ref: str = "demo-package@1",
    enabled: bool = True,
) -> DocumentEntry:
    return DocumentEntry(
        doc_key=doc_key,
        title=title or doc_key,
        profile_id=profile_id,
        template_ref=template_ref,
        source_ids=[],
        enabled=enabled,
    )


def project_config(
    project_id: str,
    *,
    doc_keys: tuple[str, ...] = DEMO_DOC_KEYS,
    **entry_overrides: Any,
) -> ProjectConfig:
    """一份可直接构建的配置（每份文档都有模板包与档案引用）。"""
    return ProjectConfig(
        project_id=project_id,
        author="fixture",
        documents=[document_entry(key, **entry_overrides) for key in doc_keys],
    )


def seed_demo_assets(harness: Harness) -> None:
    """把演示配置**引用**的模板包与档案真的登记进库（幂等）。

    为什么需要这一步
    ----------------
    前置检查从本轮起**真查**"配置引用的东西在不在库"（``server.precheck`` 的 ``refs``
    入参），不再只校验"引用是否已声明"。演示配置写死的 ``demo-package@1`` /
    ``demo-profile`` 于是必须真的存在——否则每一个 ``harness.build(...)`` 都会被
    ``E_PRECHECK_FAILED`` 挡在门口，而它们要验的是**构建之后**的事（断点、产物、门禁、
    裁决）。挡下来的那条拒绝理由本身是**对的**，只是与这些用例的题目无关。

    为什么直接落仓储而不是走导入流水线
    ----------------------------------
    本夹具要的只是"库里有一条登记"这个**状态**；"模板怎么进库、登记产出什么形状"
    由 ``test_server_templates.py`` / ``test_server_template_library.py`` 覆盖，
    再走一遍导入只会让别处用例的失败原因变模糊（同 ``test_server_profiles.seed_template``
    的理由）。**真实导入链路产出的登记能被这份检查看见**这件事由
    ``test_server_precheck.py`` 里那条端到端用例单独守着——不然这个捷径就把
    "两边口径漂了"藏起来了。

    模板与档案的标识、版本分开登记；预检会解析配置里的裸标识或版本引用。
    """
    template_id, _, version = DEMO_TEMPLATE_REF.partition("@")
    if harness.repository.find_template(template_id, version) is None:
        harness.repository.add_template(
            RegisteredTemplate(
                template_id=template_id,
                version=version,
                package_hash="seed",
                source_hash="seed",
                package_dir=str(harness.settings.templates_root / template_id / version),
                registered_at=FIXED_NOW,
            )
        )
    if harness.repository.find_profile(DEMO_PROFILE_ID, "1.0") is None:
        harness.repository.add_profile(
            RegisteredProfile(
                profile_id=DEMO_PROFILE_ID,
                version="1.0",
                doc_type=DEMO_PROFILE_ID,
                title="演示档案",
                profile_dir=str(harness.settings.profiles_root / DEMO_PROFILE_ID / "1.0"),
                fingerprint="seed",
                registered_at=FIXED_NOW,
            )
        )


def seed_domain_models(harness: Harness, models: Sequence[DomainModel] | None = None) -> None:
    """把夹具那套领域模型登记进库（幂等）。

    为什么必须由用例**显式**调，而不是塞进 :func:`seed_demo_assets`
    ------------------------------------------------------------
    领域模型不是构建路径上的资产，而是**档案的词汇表**：档案登记时要按它校验
    ``for_each`` 引用的类型（FR-M2-23，未登记即拒绝登记）。凡是"要登记一份档案"的
    用例都得先有它——而 :func:`seed_demo_assets` 服务的是"构建能跑起来"，
    把两件事混在一起之后，"这条用例为什么需要领域模型"就看不出来了。

    内容相同重复登记是幂等的（``register_domain_model`` 按指纹判，硬规则 24），
    故各处可以放心重复调用。
    """
    from tests.fixtures.domain_fixture import api_models

    for model in api_models() if models is None else models:
        harness.services.register_domain_model(model=model, author="fixture")


def seed_domain_models_via_http(client: TestClient) -> None:
    """同 :func:`seed_domain_models`，但走**真 HTTP** ``POST /api/v1/domain-models``。

    什么时候用这一版
    ----------------
    凡是"档案登记要经过端点"的用例都得用它：端点那条链路（路由 → 请求体 →
    用例 → 库 → 登记期校验）本身是交付面的一部分，绕过端点直接写仓储就验不到
    "登记完立刻能拿来校验档案"。反过来，只关心用例层的用例用
    :func:`seed_domain_models` 更直接。

    逐条断言 201：与 HTTP 用例里其它播种一致——播种失败必须当场红，
    否则后面那条"登记成功"的断言会在一个**空库**上通过，掩盖真正的失败原因。
    """
    from tests.fixtures.domain_fixture import api_models

    for model in api_models():
        posted = client.post("/api/v1/domain-models", json={"model": model.model_dump(mode="json")})
        assert posted.status_code == 201, posted.text


@dataclass
class ArtifactStage:
    """可编程阶段体：真落文件、可注入失败 / 不适用 / 裸异常。

    ``artifacts`` 里的名字会被写进 ``artifacts/``（供 ``JobRunner._collect_artifacts``
    扫描登记）；``emit_report=True`` 时额外产出逻辑名为 ``report`` 的输出
    （供 ``_store_report`` 读取）。
    """

    name: str
    #: 前 N 次调用返回失败（用于验证"失败 → 续跑"与"失败隔离"）。
    failures: int = 0
    raises: Exception | None = None
    skip_reason: str | None = None
    #: 写进工作区 ``artifacts/`` 的文件名（后缀决定产物 ``kind``）。
    artifacts: tuple[str, ...] = ()
    #: 给上一步那些文件写的字节。``None`` = 写一行 ``"<阶段>:<名字>"`` 占位文本。
    #: 比对类用例要的是**真 docx**（``compare_documents`` 读的是文档包），
    #: 而占位文本会让比对在解析那一步就失败——那不是被验的东西。
    artifact_payload: bytes | None = None
    #: 是否额外产出 ``report`` 输出（QA 阶段的产物）。
    emit_report: bool = False
    findings: tuple[Finding, ...] = ()
    #: 非 ``None`` 时本阶段**先做一次**该决策点的决策（验证决策接缝）。
    decide_point: str | None = None
    decide_target: str = ""
    calls: list[StageContext] = field(default_factory=list)
    decisions: list[DecisionOutcome] = field(default_factory=list)

    @property
    def call_count(self) -> int:
        return len(self.calls)

    def __call__(self, context: StageContext) -> StageOutcome:
        self.calls.append(context)
        if self.decide_point is not None:
            # 决策排在失败判定**之前**：决策已经发生了，阶段随后可能失败。
            # 顺序反过来时"有待裁决项但作业仍应判 failed"就验不到——那正是失败优先的情形。
            self.decisions.append(
                context.decide(self.decide_point, {"stage": self.name}, target=self.decide_target)
            )
        if self.raises is not None:
            raise self.raises
        if self.call_count <= self.failures:
            return StageOutcome(
                error=EngineErrorInfo(
                    code=ErrorCode.INTERNAL,
                    message=f"注入的失败：{self.name} 第 {self.call_count} 次调用",
                )
            )
        if self.skip_reason is not None:
            return StageOutcome(skipped_reason=self.skip_reason)
        outputs = {
            self.name: context.workspace.put_text(
                f"{WORK_DIRNAME}/{self.name}.txt", f"{self.name}@{context.inputs_hash}"
            )
        }
        for relative in self.artifacts:
            payload = self.artifact_payload or f"{self.name}:{relative}".encode()
            context.workspace.put_bytes(f"{ARTIFACTS_DIRNAME}/{relative}", payload)
        if self.emit_report:
            # 报告里的判定必须带上**可寻址标识**（``<报告标识>@<序号>``），否则这些判定
            # 在裁决端点上"点不了"，而原因看上去像是随机的——真实门禁走 ``qa.engine.run``
            # 时标识是自动赋的，夹具不赋就等于造了一份接口处理不了的报告。
            findings = list(self.findings)
            report_id = f"r-{context.job.job_id}"
            assign_finding_ids(findings, report_id)
            verdict = verdict_of(findings)
            report = Report(
                report_id=report_id,
                job_id=context.job.job_id,
                generated_at=FIXED_NOW,
                findings=findings,
                waivers=[],
                verdict=verdict,
                summary=summarize(findings, verdict=verdict),
            )
            outputs["report"] = context.workspace.put_text(
                f"{WORK_DIRNAME}/report.json", report.model_dump_json()
            )
        return StageOutcome(outputs=outputs)


def build_registry(**overrides: Any) -> StageRegistry:
    """五个阶段的默认实现；``overrides`` 按阶段名给参数。

    例：``build_registry(render={"failures": 1, "artifacts": ("out.docx",)})``。
    """
    stages: list[ArtifactStage] = []
    for name in STAGE_ORDER:
        params = overrides.get(name)
        stages.append(ArtifactStage(name=name, **(params if isinstance(params, dict) else {})))
    return StageRegistry({stage.name: stage for stage in stages})


@dataclass
class Harness:
    """一套装配好的服务端运行件（内存仓储 + 真工作区 + 注入时钟）。"""

    root: Path
    settings: ServerSettings
    repository: Repository
    runner: JobRunner
    services: Services
    clock: MutableClock

    #: 按阶段名取回阶段体（断言"这个阶段跑了几次"用）。
    stages: dict[str, ArtifactStage] = field(default_factory=dict)

    def run_all(self, *, max_jobs: int = 32) -> list[RunOutcome]:
        return self.runner.run_until_idle(max_jobs=max_jobs)

    def new_project(self, name: str = "演示项目") -> str:
        """建项目 + 初始化演示配置，返回 ``project_id``。

        顺带把演示配置引用的模板包与档案登记进库（:func:`seed_demo_assets`）：
        没有它，前置检查会（正确地）拒绝这份配置，而本夹具的大多数用例要验的是
        构建**之后**的事。
        """
        seed_demo_assets(self)
        project = self.services.create_project(name=name)
        self.services.demo_init(project.project_id)
        return project.project_id

    def build(self, project_id: str, **kwargs: Any) -> str:
        """发起构建并跑完，返回**编排作业** id。"""
        orchestration = self.services.submit_build(project_id, **kwargs)
        self.run_all()
        return orchestration.job_id


def build_harness(
    root: Path | str,
    *,
    stages: StageRegistry | None = None,
    clock: MutableClock | None = None,
    decide_session: SessionFactory | None = None,
    repository: Repository | None = None,
    **settings: Any,
) -> Harness:
    """装配一套 Harness。``settings`` 直接透传给 :class:`ServerSettings`。

    一律禁用 ``.env``（``_env_file=None``）：测试断言的是"我传进去的这组设置"，
    若开发机上恰好有个 ``.env``，断言就会随环境漂移。

    ``decide_session`` 缺省为 ``None``（确定性会话），与服务端一致；要验证"模型给出
    结论"的路径时，传一个用 :class:`~docfidelity.agents.ScriptedAdapter` 装配的工厂。

    ``repository`` 缺省为进程内仓储。要验证"落库仓储下链路照常"时传
    :class:`~server.storage.SqlAlchemyRepository`——上层（``services`` / ``JobRunner``）
    一行不改，这正是把协议单独成模块的意义。
    """
    moment = clock or MutableClock()
    resolved_root = Path(root)
    resolved_root.mkdir(parents=True, exist_ok=True)
    resolved_settings = ServerSettings(_env_file=None, storage_root=resolved_root, **settings)
    registry = stages if stages is not None else build_registry()
    resolved_repository = repository if repository is not None else MemoryRepository()
    runner = JobRunner(
        repository=resolved_repository,
        registry=registry,
        settings=resolved_settings,
        root=resolved_root,
        events=EventLog(WorkspaceEventSink(resolved_root)),
        decide_session=decide_session,
        now=moment,
    )
    services = Services(
        repository=resolved_repository,
        runner=runner,
        settings=resolved_settings,
        now=moment,
    )
    harness = Harness(
        root=resolved_root,
        settings=resolved_settings,
        repository=resolved_repository,
        runner=runner,
        services=services,
        clock=moment,
    )
    for name in registry.registered():
        resolved = registry.resolve(name)
        if isinstance(resolved, ArtifactStage):
            harness.stages[name] = resolved
    return harness
