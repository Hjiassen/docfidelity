#!/usr/bin/env python3
"""本地开发服务器：把 ``server`` 跑起来，并接上一组**演示用**阶段体。

为什么需要这个脚本
------------------
服务端的流水线阶段体由 ``docfidelity.build_stages`` 入口点提供（DD-13.1）——那是**构建包**
的职责，属于后续轮次；内核目前只提供阶段机与断点机制。所以 ``create_app()`` 起得来，
但 ``POST /jobs`` 会因为"阶段未注册"被明确拒绝（``/health`` 也会报出缺哪些阶段）。

于是前端验收（P0-15 的「最小闭环可在浏览器跑通」）缺一个能跑的后端。本脚本补的是这一段：
它装配内存仓储 + 一组只往工作区写文件的阶段体，让界面能走通
「建项目 → 发起构建 → 看阶段与事件 → 看门禁报告 → 看产物与设置」。

它**不是**生产入口，也**不是**引擎的替代实现
--------------------------------------------
- 阶段体只写占位文件，**不做真实的模板注入与渲染**：产物里的 ``out.docx`` 并不是一个
  可用的 Word 文档。它存在的意义是让界面上的链路有**真实响应**可渲染，而不是让前端
  对着空数据调样式。
- 门禁报告是**固定的演示载荷**（一条阻断、一条严重、一条已豁免 + 一条跳过），不是规则
  执行结果。这恰好让 P10 的汇总卡、级别分组、裁决区、豁免清单与"跳过的规则"都有东西可验。
- 仓储是内存实现，进程退出即清空（重启后项目与作业都不在）。

生产部署等 ``server`` 落库轮（SQLAlchemy + Alembic）与构建包就绪后由它们承担。

用法：
    PYTHONPATH="src;." .venv/Scripts/python.exe scripts/serve_dev.py
    # 另开一个终端
    cd web && npm run dev
"""

from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Final

from fastapi import FastAPI

from docfidelity.agents import AdapterOutcome, ScriptedAdapter
from docfidelity.contracts.config import RoutingConfig
from docfidelity.contracts.job import Job
from docfidelity.contracts.qa import Finding, Location, Report, RuleSeverity, Waiver
from docfidelity.pipeline import (
    ARTIFACTS_DIRNAME,
    STAGE_ORDER,
    WORK_DIRNAME,
    DecisionSession,
    EventLog,
    StageContext,
    StageOutcome,
    StageRegistry,
    build_session,
)
from docfidelity.qa import assign_finding_ids
from server.api.deps import AppState
from server.jobs import SessionFactory, WorkspaceEventSink
from server.main import APP_STATE_ATTRIBUTE, create_app
from server.services import DEMO_PROFILE_ID, DEMO_TEMPLATE_REF, Services
from server.settings import ServerSettings
from server.storage import MemoryRepository

__all__ = ["DevStage", "build_dev_registry", "build_dev_session_factory", "main"]

_LOGGER = logging.getLogger("docfidelity.serve_dev")

#: 演示用的决策点：`qa` 阶段会就"体例冲突的裁决建议"问一次模型。
#: 选它是因为它属于 ``small_model`` 档、且回退标待确认——正好把"模型结论 → 裁决队列"
#: 这条链在界面上走通。要改成"高置信自动采用"，把下面的 ``confidence`` 调到 0.9 以上即可。
_DEV_DECISION_POINT = "convention.advisory"

#: 演示项目名（启动时若一个项目都没有就建它）。
DEMO_PROJECT_NAME = "演示项目（开发服务器）"

#: 每个阶段产出的占位产物名。`render` 给 `.docx` 是为了让产物的 `kind` 是 `docx`。
_STAGE_ARTIFACTS: dict[str, tuple[str, ...]] = {
    "ingest": ("facts.json",),
    "compose": ("content-ir.json",),
    "render": ("out.docx",),
    "refresh": (),
    "qa": (),
}

#: 演示报告里的判定项。**固定载荷**：本脚本不执行真实规则（见模块文档）。
#: 第一条是**阻断**（P10 的待裁决候选）、第二条严重、第三条已豁免（见 :data:`_DEMO_WAIVED_INDEX`）。
_DEMO_FINDINGS: tuple[tuple[str, RuleSeverity, str, str], ...] = (
    (
        "FR-M5-07",
        "blocker",
        "第 2 章的表 2-1 不在正文的编号序列里（表注跳号）。",
        "在渲染层重建表注编号；若该表本不该出现，请从内容 IR 中移除它。",
    ),
    (
        "FR-M5-03",
        "major",
        "第 3 章存在重复编号的图注「图 3-1」。",
        "改正文的图注或重新编号；编号由渲染层生成时请检查章节注入顺序。",
    ),
    (
        "FR-M5-11",
        "minor",
        "术语「接口」在同一文档内出现了两种写法（接口 / API 接口）。",
        "统一到术语表（glossary）中的首选写法。",
    ),
)

#: 演示报告里**已豁免**的那一条（配一条对应的 waiver，故 P10 的豁免清单非空）。
_DEMO_WAIVED_INDEX: Final = 2

#: 演示报告里**因缺能力被跳过**的规则（P10 单独列出：跳过不等于通过）。
_DEMO_SKIPPED: Final[tuple[str, ...]] = ("FR-M5-06（缺 渲染目检：本机未安装 Word 转换器）",)

#: 演示报告摘要的前缀。后接 ``qa`` 重算出的结论句——那句话由唯一实现产出，
#: 这里只把"这不是真实门禁结果"这件事**接在它前面**，不覆盖它。
_DEMO_SUMMARY_PREFIX: Final = "演示载荷（本报告由开发服务器固定产出，不是真实规则执行结果）。"


@dataclass
class DevStage:
    """演示阶段体：写占位输出，``qa`` 另外产出一份演示报告。

    ``calls`` 记录调用次数，便于在日志里确认"复用断点后阶段确实没再跑"。
    """

    name: str
    calls: list[str] = field(default_factory=list)

    def __call__(self, context: StageContext) -> StageOutcome:
        self.calls.append(context.inputs_hash)
        outputs = {
            self.name: context.workspace.put_text(
                f"{WORK_DIRNAME}/{self.name}.txt",
                f"{self.name} 占位输出 @ {context.inputs_hash}\n",
            )
        }
        for relative in _STAGE_ARTIFACTS.get(self.name, ()):
            context.workspace.put_bytes(
                f"{ARTIFACTS_DIRNAME}/{relative}",
                f"docfidelity dev placeholder: {relative}\n".encode(),
            )
        if self.name == "qa":
            # 决策**先于**报告：报告要能把这次的决策算进去。
            context.decide(
                _DEV_DECISION_POINT,
                {"stage": self.name},
                target=context.job.doc_key or context.job.job_id,
                candidates=["统一到术语表首选写法", "保留两种写法并加注"],
            )
            outputs["report"] = context.workspace.put_text(
                f"{WORK_DIRNAME}/report.json", _demo_report(context).model_dump_json()
            )
        return StageOutcome(outputs=outputs)


def _demo_report(context: StageContext) -> Report:
    """固定载荷的演示报告。

    首次产出时四条清单各有一条东西——**这是刻意的**：它们正好对应 P10 上的四块区域
    （待裁决条目 / 裁决留痕 / 豁免清单 / 跳过的规则）。裁决留痕初始为空（它由人工在界面上
    产生），其余三块非空，故"界面上的区域有没有东西可看"在开发环境里是可验的。

    ``finding_id`` 走 :func:`~docfidelity.qa.assign_finding_ids`（**同一套规则**）：
    少了它，演示环境里点不了裁决而真实环境可以，且看上去是随机的。
    ``verdict`` 由 :meth:`Report.recompute` 算出，不手写——手写的结论会在判定变动后
    立刻自相矛盾。
    """
    job_id = context.job.job_id
    report_id = f"dev-report-{job_id}"
    moment = datetime.now(tz=timezone.utc)
    findings = [
        Finding(
            rule_id=rule_id,
            severity=severity,
            status="open",
            message=message,
            location=Location(chapter_path=["1", "演示章节"]),
            suggestion=suggestion,
        )
        for rule_id, severity, message, suggestion in _DEMO_FINDINGS
    ]
    # 豁免：判定状态与 waiver 必须**同时**存在，否则报告自相矛盾（汇总卡说"1 项已豁免"
    # 而清单里一条都没有）。
    waived = findings[_DEMO_WAIVED_INDEX]
    waiver = Waiver(
        waiver_id="wv_demo",
        rule_id=waived.rule_id,
        reason="演示载荷：该写法已由责任人确认保留。",
        operator="demo-init",
        created_at=moment,
        scope="instance",
    )
    waived.status = "waived"
    waived.waiver_id = waiver.waiver_id
    assign_finding_ids(findings, report_id)

    produced = Report(
        report_id=report_id,
        job_id=job_id,
        generated_at=moment,
        findings=findings,
        waivers=[waiver],
        skipped=list(_DEMO_SKIPPED),
    ).recompute()
    return produced.model_copy(
        update={"summary": f"{_DEMO_SUMMARY_PREFIX}{produced.summary or ''}"}
    )


def build_dev_registry() -> StageRegistry:
    """五个阶段各一个 :class:`DevStage`（顺序按契约的 ``STAGE_ORDER``）。"""
    stages = [DevStage(name=name) for name in STAGE_ORDER]
    return StageRegistry({stage.name: stage for stage in stages})


def build_dev_session_factory() -> SessionFactory:
    """演示用的决策会话工厂：脚本化适配层 + 一条**低置信**答案。

    不接真实模型（开发服务器不该要求 API key），但把决策链路的形状留全了：
    走的是 ``small_model`` 档 → 置信度不达阈值 → 带原因重试 → 仍不达标 → 回退规则
    默认值并**标待确认**。故意选低置信，是因为"高置信自动采用"那条路不需要人盯，
    而待裁决这条路要能被人看见：P08 的待裁决徽标、``/inbox`` 的计数、P09 事件流里的
    ``event: decision``，在开发环境里就都有真实数据可看，不必等接上真模型。

    真实部署的工厂是 ``server.api.deps.session_factory_from``（由 ``build_state`` 在
    ``decide_session=None`` 时自动调用）：它按 ``settings.model_*`` 决定挂不挂真模型，
    换适配层 / 加基线 / 挂落库镜像都只改那一处。本脚本**显式覆盖**它，为的是让开发
    环境不依赖 API key 也有完整的决策链形状可看（见上）。"""
    adapter = ScriptedAdapter(
        script={
            _DEV_DECISION_POINT: AdapterOutcome(
                ok=True,
                schema_ok=True,
                payload={"choice": "统一到术语表首选写法"},
                chosen="统一到术语表首选写法",
                confidence=0.41,  # 低于默认门槛 0.7 → 进裁决队列
                model="dev-scripted",
                tokens_in=120,
                tokens_out=18,
            )
        },
        available_flag=True,
    )

    def factory(job: Job, routing: RoutingConfig | None, events: EventLog) -> DecisionSession:
        return build_session(
            routing, job_id=job.job_id, events=events, adapter=adapter, model_enabled=True
        )

    return factory


def _seed_demo_project(app: FastAPI) -> None:
    """没有任何项目时建一个演示项目，免得首次打开界面是空列表。"""
    state: AppState = getattr(app.state, APP_STATE_ATTRIBUTE)
    if state.services.list_projects():
        return
    project = state.services.create_project(name=DEMO_PROJECT_NAME)
    state.services.demo_init(project.project_id)
    _LOGGER.info("已创建演示项目 %s（%s）", project.name, project.project_id)


def _seed_demo_assets(services: Services, storage_root: Path) -> None:
    """给演示项目登记它引用的模板包与档案。

    为什么开发服务器需要这一段
    --------------------------
    前置检查现在**真查**"配置引用的东西在不在库"（``server.precheck`` 的 ``refs``
    入参），不再只校验"引用是否已声明"。演示配置写的 ``demo-package@1`` /
    ``demo-profile`` 于是必须真的存在——否则打开 P08 会看到一个禁用的"发起构建"
    加一条**正确**的拒绝理由，而开发服务器的用处恰恰是"能看见按钮点下去之后发生什么"。

    原件借用测试夹具（``tests/fixtures/docx_builder.build_template_docx``）：生产路径
    收的是使用者上传的 docx，本脚本只是需要一份"能解析出契约"的原件。夹具目录在
    类型无关门禁的白名单里，故它带领域概念名不会触发那条检查。

    失败只记警告、不拦启动：开发服务器的第一职责是能把界面打开，而真出问题的那一条
    会在 P08 页面上以"引用的模板包未登记"的形式显示出来——那比进程起不来更好排查。
    """
    from tests.fixtures.docx_builder import build_template_docx
    from tests.fixtures.domain_fixture import api_models, api_profile

    from docfidelity.contracts.profile import Bindings

    if services.list_templates(template_id=_template_id_of(DEMO_TEMPLATE_REF)):
        return
    try:
        # 领域模型**先于**档案登记：演示档案的 for_each 指着 `endpoint` /
        # `endpoint.parameters`，而不登记它们的话，档案登记会被
        # `E_DOMAIN_KIND_UNKNOWN` 拦住——那正是 FR-M2-23 该有的样子，不是 bug。
        for model in api_models():
            services.register_domain_model(model=model, author="serve-dev")
        seed_dir = storage_root / "seed"
        seed_dir.mkdir(parents=True, exist_ok=True)
        source = build_template_docx(seed_dir / "demo-package.docx")
        record = services.import_template(
            filename=source.name,
            payload=source.read_bytes(),
            doc_type=DEMO_PROFILE_ID,
            template_id=_template_id_of(DEMO_TEMPLATE_REF),
            version=_version_of(DEMO_TEMPLATE_REF),
            author="serve-dev",
        )
        services.register_template(
            record.import_id,
            template_id=_template_id_of(DEMO_TEMPLATE_REF),
            version=_version_of(DEMO_TEMPLATE_REF),
            operator="serve-dev",
        )
        # 演示档案直接借用夹具那份（"一模块一节 + 每接口一张参数表"），只把身份与
        # 模板绑定改成演示配置里写的值——否则登记时会被 `E_PROFILE_TEMPLATE_MISSING`
        # 拒掉，而那正是这条检查该有的样子。
        document = api_profile().model_copy(
            update={
                "profile_id": DEMO_PROFILE_ID,
                "doc_type": DEMO_PROFILE_ID,
                "bindings": Bindings(template_packages=[DEMO_TEMPLATE_REF]),
            }
        )
        services.register_profile(
            document=document,
            author="serve-dev",
            known_template_refs=[DEMO_TEMPLATE_REF],
            # 领域模型的装配与模板引用一样，得由调用方从库里读出来传进去：登记期
            # 校验刻意不认识仓储（硬规则 22 的"入参全是纯数据"）。
            domain_models=services.domain_declarations(),
        )
        _LOGGER.info("已登记演示资产：%s / %s", DEMO_TEMPLATE_REF, DEMO_PROFILE_ID)
    except Exception:  # 开发脚本：任何失败都只降级成警告，不拦启动
        _LOGGER.warning("演示资产登记失败，P08 会照实报告引用未登记", exc_info=True)


def _template_id_of(ref: str) -> str:
    return ref.rsplit("@", 1)[0]


def _version_of(ref: str) -> str:
    return ref.rsplit("@", 1)[-1]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="docfidelity 本地开发服务器（非生产）")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument(
        "--storage-root",
        default=".devdata",
        help="工作区根目录（默认 .devdata，已在 .gitignore 中）",
    )
    parser.add_argument("--reload", action="store_true", help="改动即重启（开发用）")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    settings = ServerSettings(storage_root=Path(args.storage_root))
    root = Path(args.storage_root)
    app = create_app(
        settings=settings,
        repository=MemoryRepository(),
        stages=build_dev_registry(),
        root=root,
        events=EventLog(WorkspaceEventSink(root)),
        decide_session=build_dev_session_factory(),
        start_worker=True,
    )
    _seed_demo_project(app)
    state: AppState = getattr(app.state, APP_STATE_ATTRIBUTE)
    _seed_demo_assets(state.services, root)

    # 横幅是刻意的：这个服务器会返回**假的**门禁报告，别把它当成验收环境。
    _LOGGER.warning(
        "开发服务器启动：阶段体为演示实现，门禁报告为固定载荷，"
        "决策结论来自脚本化适配层（非真实模型），仓储为内存实现（重启即清空）"
    )
    _LOGGER.info("后端 http://%s:%d  ·  工作区 %s", args.host, args.port, root.resolve())
    _LOGGER.info("前端请在 web/ 下执行 npm run dev（已代理 /api 与 /health 到此端口）")

    import uvicorn  # 延迟导入：仅本脚本需要，内核与服务端包都不依赖它

    uvicorn.run(app, host=args.host, port=args.port, reload=args.reload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
