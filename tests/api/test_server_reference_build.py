"""参考构建包端到端：从配置到产物（DD-13.1 / TASK-P0-15 遗留）。

这条用例守的是**仓库开箱能构建出一份文档**这件事
------------------------------------------------
在 ``docfidelity.builder`` 出现之前，``pyproject.toml`` 里既没有
``[project.entry-points."docfidelity.build_stages"]``，``[project.scripts]`` 指向的
``docfidelity.platform.cli`` 也不存在——生产构建路径上**没有任何阶段体**，
``POST /jobs`` 必被前置检查以 ``missing_stages`` 拒掉。

这件事之所以长期没暴露，是因为 ``tests/fixtures/server_fixture.build_registry`` 提供了
一组 ``ArtifactStage`` 假阶段体：它把"流程会跑完、断点会保住、失败会隔离"测得很扎实，
代价是**真实现一行都没被执行过**。所以本文件的判据必须落在真实现上——
每一层都问"这一步的结果是从磁盘上的真东西里读出来的吗"。

三层判据
--------
1. **装配** —— 入口点声明存在、指得着真东西、且覆盖全部阶段（缺一个就报错）。
2. **数据** —— 声明的来源在 ``ingest`` 阶段被**真跑一遍**（连接器 + 合并），产出的事实
   进了 ``work/facts.json``。这是"来源声明在 ingest 阶段的真实消费"这条遗留的判据。
3. **交付** —— 来源里的内容真的出现在产物 ``.docx`` 里（不是只到 IR 为止）。
"""

from __future__ import annotations

import importlib
import json
import re
import zipfile
from functools import partial
from pathlib import Path
from typing import Any

import pytest
import tomllib

from docfidelity.builder import build_registry as reference_registry
from docfidelity.builder import stages as reference_stages
from docfidelity.contracts.config import DocumentEntry, ProjectConfig
from docfidelity.contracts.fact import KindBinding, SourceConfig
from docfidelity.contracts.profile import Bindings
from docfidelity.pipeline import STAGE_ORDER
from server.services import DEMO_TEMPLATE_REF
from tests.fixtures.compose_fixture import api_profile
from tests.fixtures.domain_fixture import api_models
from tests.fixtures.server_fixture import Harness, build_harness

pytestmark = pytest.mark.api

#: 仓库根（``tests/api/<this file>`` 上溯三级）——读 ``pyproject.toml`` 用。
REPO_ROOT = Path(__file__).resolve().parents[2]

#: 本用例自己的档案标识。取 ``compose_fixture.api_profile`` 的那一份——它**声明了
#: ``ProjectionSpec.slot``，故内容单元真的落得进章节骨架。``domain_fixture`` 的同名档案
#: 不声明 slot，单元一律落保留槽位 ``default``，于是构建能跑完但产物只有骨架。
PROFILE_ID = "api-compact"

#: 送检的来源：一段 FastAPI 风格的源码。用源码而不是 xlsx/openapi 是因为它零依赖、
#: 且在仓库里就能造出"真解析出来的事实"。
SOURCE_MODULE = '''\
from fastapi import APIRouter

router = APIRouter()


@router.post("/orders")
def create_order(body: dict) -> dict:
    """创建订单。"""
    return {}


@router.get("/orders/{order_id}")
def get_order(order_id: str) -> dict:
    """查询订单。"""
    return {}
'''


@pytest.fixture
def harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Harness:
    """真实阶段体、无需本机 Word 的参考构建 harness。"""
    from docfidelity.render import toc

    def unexpected_office_call(*_args: Any, **_kwargs: Any) -> None:
        pytest.fail("参考构建测试不得探测或启动本机 Word")

    monkeypatch.setattr(toc, "office_available", unexpected_office_call)
    monkeypatch.setattr(toc, "refresh_with_office", unexpected_office_call)
    monkeypatch.setattr(reference_stages, "render", partial(reference_stages.render, office=False))
    return build_harness(tmp_path, stages=reference_registry())


# ------------------------------------------------------------------ 装配层
def test_the_stage_provider_is_declared_and_points_at_something_real() -> None:
    """入口点声明必须存在，且 ``module:attr`` 真的解得开。

    只断言"字符串写对了"是不够的——``platform.cli:main`` 那条悬空声明就那么躺了很久：
    ``pyproject.toml`` 语法正确、``pip install`` 不报错、直到有人真的调它才炸。
    故这里把它 import 进来并确认可调用。
    """
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    entries = pyproject["project"]["entry-points"]["docfidelity.build_stages"]

    assert entries["reference"] == "docfidelity.builder.stages:provide"
    module_name, _, attr = entries["reference"].partition(":")
    assert callable(getattr(importlib.import_module(module_name), attr))


def test_the_console_script_points_at_something_real() -> None:
    """``[project.scripts]`` 曾指向不存在的 ``docfidelity.platform.cli``。"""
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    target = pyproject["project"]["scripts"]["docfidelity"]

    module_name, _, attr = target.partition(":")
    assert callable(getattr(importlib.import_module(module_name), attr))


def test_the_reference_registry_covers_the_whole_stage_order() -> None:
    """缺一个阶段就**报错**，而不是少跑一个阶段。

    ``provide`` 里那道 ``missing`` 检查防的是"契约新增了阶段、本包没跟上"——那种情况下
    作业会卡在某个阶段，而报错点在远离"谁没登记"的地方。
    """
    registry = reference_registry()

    assert registry.missing() == ()
    for stage in STAGE_ORDER:
        assert callable(registry.resolve(stage))


# ------------------------------------------------------------------ 端到端
def _stage_status(job_dir: Path) -> dict[str, str]:
    """事件流里每个阶段的**最终**状态（后写的覆盖先写的：``running`` → 终态）。"""
    status: dict[str, str] = {}
    for line in (job_dir / "events.jsonl").read_text(encoding="utf-8").splitlines():
        event = json.loads(line)
        if event.get("type") != "stage":
            continue
        payload = event.get("payload") or {}
        if payload.get("stage") and payload.get("status"):
            status[payload["stage"]] = payload["status"]
    return status


def _findings(job_dir: Path, stage: str) -> list[dict[str, Any]]:
    path = job_dir / "work" / "findings" / f"{stage}.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _register_real_assets(harness: Harness, template_docx: Path) -> None:
    """把**真**模板包与**真**档案登记进库。

    不能用 ``Harness.new_project``（它走 ``seed_demo_assets``）：那条捷径只在仓储里放
    一条登记记录、库目录是空的——对 ``ArtifactStage`` 够用，而本文件的阶段体要从磁盘
    读模板包与原件。
    """
    services = harness.services
    for model in api_models():
        services.register_domain_model(model=model, author="e2e")

    record = services.import_template(
        filename=template_docx.name,
        payload=template_docx.read_bytes(),
        doc_type="api-compact",
        template_id="demo-package",
        version="1",
        author="e2e",
    )
    services.register_template(
        record.import_id, template_id="demo-package", version="1", operator="e2e"
    )

    document = api_profile().model_copy(
        update={"bindings": Bindings(template_packages=[DEMO_TEMPLATE_REF])}
    )
    services.register_profile(
        document=document,
        author="e2e",
        known_template_refs=[DEMO_TEMPLATE_REF],
        domain_models=services.domain_declarations(),
    )


def _add_source(harness: Harness, project_id: str, root: Path) -> SourceConfig:
    """在存储根下造一个源码目录，并登记一条指向它的 ``ast`` 来源。"""
    source_dir = root / "facts_src"
    source_dir.mkdir(parents=True, exist_ok=True)
    (source_dir / "orders_api.py").write_text(SOURCE_MODULE, encoding="utf-8")

    config = SourceConfig(
        source_id="orders-src",
        source_type="ast",
        title="订单接口源码",
        # ``root`` 相对**存储根**——与「试接入」同一个解析基点（state 的那条纪律）。
        root="facts_src",
        bindings=[
            KindBinding(
                shape="route",
                kind="endpoint",
                # ``key`` 写的是 ``field_map`` 之后的名字，顺序即键序。
                key=["module", "path"],
                field_map={"route": "path"},
            )
        ],
        # 这个源码目录只当"接口清单"用，故关掉模型识别：未绑定的形态会记 Finding，
        # 而那是**对的**告警——这里不想让它淹没本题的判据。
        options={"model_bases": []},
    )
    harness.services.add_source(project_id, config=config, author="e2e")
    return config


def _submit(harness: Harness, project_id: str, *, source_ids: list[str]) -> tuple[str, Path]:
    """发起构建并跑完，返回 ``(子作业 id, 子作业工作区)``。"""
    orchestration_id = harness.services.submit_build(project_id).job_id
    harness.run_all()
    child = harness.repository.get_job(orchestration_id).child_job_ids[0]
    return child, harness.root / "jobs" / child


def _project(harness: Harness, template_docx: Path, *, source_ids: list[str]) -> str:
    _register_real_assets(harness, template_docx)
    project = harness.services.create_project(name="端到端")
    harness.services.put_config(
        project.project_id,
        ProjectConfig(
            project_id=project.project_id,
            author="e2e",
            documents=[
                DocumentEntry(
                    doc_key="overview",
                    title="总览",
                    profile_id=PROFILE_ID,
                    template_ref=DEMO_TEMPLATE_REF,
                    source_ids=list(source_ids),
                )
            ],
        ),
        author="e2e",
    )
    return project.project_id


def test_a_declared_source_is_really_consumed_by_ingest(
    harness: Harness, template_docx: Path
) -> None:
    """**这条遗留的正题**：来源声明在 ``ingest`` 阶段被真跑一遍。

    在参考构建包之前，"来源声明"最多只被 ``sources/*`` 那组端点校一遍配置——
    构建路径上没有哪一步会把连接器真的执行起来。故本用例不接受"阶段报成功"这种证据，
    它去读 ``work/facts.json``：那里的事实必须**带得出源文件名与行号**，而这两个值
    只有真解析过那个 ``.py`` 才可能存在。
    """
    project_id = _project(harness, template_docx, source_ids=["orders-src"])
    _add_source(harness, project_id, harness.root)
    child, job_dir = _submit(harness, project_id, source_ids=["orders-src"])

    assert harness.repository.get_job(child).status == "succeeded"
    # 跳过会是 `skipped`——那正是"没有可用来源"时的诚实结论，也是本条遗留的旧状态。
    assert _stage_status(job_dir)["ingest"] == "succeeded"

    snapshot = json.loads((job_dir / "work" / "facts.json").read_text(encoding="utf-8"))
    endpoints = [fact for fact in snapshot["merged"] if fact["kind"] == "endpoint"]
    assert endpoints, "接入器一条事实都没抽出来——阶段报成功但快照是空的"
    create = next(fact for fact in endpoints if fact["key"] == ["orders_api", "/orders"])
    assert create["source"] == "orders-src"
    assert create["payload"]["method"] == "POST"
    assert create["payload"]["summary"] == "创建订单。"
    assert create["locator"].startswith("orders_api.py:")  # 源内定位，只有真解析才有


def test_the_source_contents_survive_all_the_way_into_the_deliverable(
    harness: Harness, template_docx: Path
) -> None:
    """**交付层**：来源里的内容出现在产物 ``.docx`` 里。

    只断言"事实进了快照"会放过一整类缺陷：事实到了 IR、IR 到了渲染，但槽位没收下、
    单元被静默丢掉——快照与 IR 都好看，产物里一个字都没有。
    """
    project_id = _project(harness, template_docx, source_ids=["orders-src"])
    _add_source(harness, project_id, harness.root)
    _, job_dir = _submit(harness, project_id, source_ids=["orders-src"])

    artifact = next((job_dir / "artifacts").glob("*.docx"))
    with zipfile.ZipFile(artifact) as package:
        text = re.sub(r"<[^>]+>", "", package.read("word/document.xml").decode("utf-8"))

    assert "/orders" in text
    assert "创建订单" in text
    assert "orders_api" in text


def test_the_chain_still_delivers_with_the_model_switch_off(
    harness: Harness, template_docx: Path
) -> None:
    """**TASK-P0-17 验收第 1 条**（设计 19.3）：``DF_MODEL_ENABLED=false`` 时全链路跑通并产出。

    为什么把前提**断言**出来，而不是靠夹具默认值
    --------------------------------------------
    本夹具的 ``ServerSettings`` 确实是用默认值装的，而默认 ``model_enabled=False``
    （DD-19.2：``DF_MODEL_ENABLED`` 默认 ``false``）。但"夹具碰巧是关的"与"本条验的是
    关掉模型"是两件事：夹具哪天改了默认值，这条用例会**照常通过**，而它守的那句话已经不
    成立了。故先钉住前提。

    判据落在**产物**上，不停在"作业 succeeded"：后者会放过"五个阶段都跑了，但产物是
    零字节 / 根本不是个 zip"这一形态——那正是"关掉模型后拿不到可交付物"最可能的死法。
    """
    assert harness.settings.model_enabled is False, "本条验的是关掉模型，前提必须先成立"

    project_id = _project(harness, template_docx, source_ids=["orders-src"])
    _add_source(harness, project_id, harness.root)
    child, job_dir = _submit(harness, project_id, source_ids=["orders-src"])

    assert harness.repository.get_job(child).status == "succeeded"

    states = _stage_status(job_dir)
    assert set(states) == set(STAGE_ORDER), "有阶段没被跑到，就不是'全链路'"
    assert "failed" not in set(states.values())
    # ``refresh`` 恒为 ``skipped`` 是设计（DD-9.6 的第 10/11 步已收在 ``render()`` 里），
    # 故这里只排除 ``failed``，不要求每个阶段都 ``succeeded``。

    artifact = next((job_dir / "artifacts").glob("*.docx"))
    with zipfile.ZipFile(artifact) as package:
        assert package.testzip() is None
        assert package.read("word/document.xml")


@pytest.mark.parametrize("mode", ["admitted", "disabled", "failed"])
def test_default_build_carries_semantic_decisions_to_ir_docx_and_report(
    harness: Harness, template_docx: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    from docfidelity.agents import ScriptedAdapter
    from docfidelity.pipeline import build_session
    from tests.unit.test_builder_decisions import PlacementAdapter

    project_id = _project(harness, template_docx, source_ids=["orders-src"])
    _add_source(harness, project_id, harness.root)
    profile = api_profile()
    profile.profile_id = "semantic-profile"
    profile.bindings = Bindings(template_packages=[DEMO_TEMPLATE_REF])
    profile.projections[0].slot = None
    harness.services.register_profile(
        document=profile,
        author="e2e",
        known_template_refs=[DEMO_TEMPLATE_REF],
        domain_models=harness.services.domain_declarations(),
    )
    config = harness.services.get_config(project_id)
    config.documents[0].profile_id = profile.profile_id
    harness.services.put_config(project_id, config, author="e2e")
    adapter = PlacementAdapter() if mode != "failed" else ScriptedAdapter()

    def factory(job, routing, events):
        return build_session(
            routing,
            job_id=job.job_id,
            events=events,
            adapter=adapter,
            model_enabled=mode != "disabled",
            max_retries=0,
            baseline={"placement.section_slot": 1.0},
        )

    monkeypatch.setattr(harness.runner, "_decide_session", factory)
    child, job_dir = _submit(harness, project_id, source_ids=["orders-src"])
    assert harness.repository.get_job(child).status == (
        "succeeded" if mode == "admitted" else "awaiting_decision"
    )
    ir = json.loads((job_dir / "work/ir.json").read_text(encoding="utf-8"))
    assert ir["units"][0]["children"]
    report = json.loads((job_dir / "work/report.json").read_text(encoding="utf-8"))
    assert len(report["decisions"]) == 2
    assert len(report["model_calls"]) == (0 if mode == "disabled" else 2)
    assert all(
        item["status"] == ("auto" if mode == "admitted" else "pending")
        for item in report["decisions"]
    )
    with zipfile.ZipFile(next((job_dir / "artifacts").glob("*.docx"))) as package:
        assert "创建订单" in package.read("word/document.xml").decode("utf-8")


def test_probe_and_build_resolve_the_source_root_the_same_way(
    harness: Harness, template_docx: Path
) -> None:
    """「试接入说通了、真接入却说找不到」是这条通路最典型的事故。

    两边的 ``root`` 都相对**存储根**（而不是作业工作区）解析，故这里把两侧的结论摆在一起。
    """
    project_id = _project(harness, template_docx, source_ids=["orders-src"])
    _add_source(harness, project_id, harness.root)

    assert harness.services.probe(project_id, "orders-src").ok is True

    _, job_dir = _submit(harness, project_id, source_ids=["orders-src"])
    assert _stage_status(job_dir)["ingest"] == "succeeded"


def test_without_a_source_ingest_says_skipped_instead_of_pretending(
    harness: Harness, template_docx: Path
) -> None:
    """对立断言：没有声明来源时如实 ``skipped``，**不是**"成功跑了一遍"。

    ``ingest`` 仍是 ``skipped`` 但作业照常成功：一份不引用任何事实源的文档是**不适用**，
    不是错误。它不会因此静默通过——``compose`` 会按骨架去要事实，槽位填不上就报 blocker。
    """
    project_id = _project(harness, template_docx, source_ids=[])
    child, job_dir = _submit(harness, project_id, source_ids=[])

    assert _stage_status(job_dir)["ingest"] == "skipped"
    assert harness.repository.get_job(child).status == "succeeded"
    # 快照照写（下游要读文件），只是里面没有事实。
    snapshot = json.loads((job_dir / "work" / "facts.json").read_text(encoding="utf-8"))
    assert snapshot["merged"] == []


def test_a_broken_source_fails_the_stage_instead_of_losing_a_chapter(
    harness: Harness, template_docx: Path
) -> None:
    """来源接不进来 ⇒ **整阶段失败**，且失败位置说得清是哪一条来源（FR-M1-12）。

    "跳过后继续"产出的是一份少了一整章的文档，而报告里只有一条 major——那正是本项目
    反复防的那个形态。故这里断言作业**失败**，并且 ``where`` 里带着 ``source``：
    一份文档可以引用多条来源，只说"扩展名不在白名单内"而不说**哪一条**，
    使用者就得自己一条条试。
    """
    project_id = _project(harness, template_docx, source_ids=["ghost-src"])
    harness.services.add_source(
        project_id,
        config=SourceConfig(
            source_id="ghost-src",
            source_type="ast",
            # 配置期校验只查"路径解不解得开"（``safe_join`` 管越界），**不查存在性**——
            # 于是"登记时好好的、构建时被移走了"这条路径走得通，正是要验的那一条。
            root="nowhere/orders_api.py",
        ),
        author="e2e",
    )

    child, job_dir = _submit(harness, project_id, source_ids=["ghost-src"])
    job = harness.repository.get_job(child)

    assert job.status == "failed"
    assert job.error is not None
    assert job.error.where.get("source") == "ghost-src"
    assert _stage_status(job_dir)["ingest"] in ("failed", "running")


def test_a_hit_write_guardrail_fails_the_stage_and_writes_nothing(
    harness: Harness, template_docx: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """渲染**写盘护栏**命中 ⇒ 整阶段失败、产物一个字节都不写。

    护栏拦的是"产物已坏但看不出来"（样式引用悬挂，DD-10.7）。此时**没有文件可交付**，
    阶段只能失败——这与"门禁判定为 ``blocked``"是两件事：后者是"文件写出来了但不合规"，
    报告负责说，作业照常成功。

    护栏的判定已在内核单测里覆盖，这里补的是**阶段体对它的处置**：不写盘、如实失败、
    错误码不是笼统的 ``E_INTERNAL``。
    """
    from docfidelity.render import pipeline

    monkeypatch.setattr(pipeline, "validate_style_refs", lambda _pkg, _styles: ["Missing"])
    project_id = _project(harness, template_docx, source_ids=["orders-src"])
    _add_source(harness, project_id, harness.root)

    child, job_dir = _submit(harness, project_id, source_ids=["orders-src"])
    job = harness.repository.get_job(child)

    assert job.status == "failed"
    assert job.error is not None
    assert job.error.code == "E_RENDER_BLOCKED"
    assert not list((job_dir / "artifacts").glob("*.docx")), "命中护栏却仍然写盘了"


def test_a_legitimate_ir_is_not_flagged_as_type_coupled(
    harness: Harness, template_docx: Path
) -> None:
    """回归：``compose`` 阶段体曾把**整份**词表喂给 IR 纯净检查。

    词表分三组，``branch_literal``（``endpoint`` / ``table`` / ``field`` / ``parameter``）
    只管"形如 ``x.kind == "..."`` 的类型分支"，只在扫源码 AST 时有意义。IR 检查只能做
    子串匹配，于是每一份**合法** IR 都被判 blocker——``unit_id`` 由档案的 ``for_each``
    派生、``type`` 就是 ``UnitType`` 的取值。表现是所有构建都挂着 4 条 blocker，
    而产物看起来是好的。
    """
    project_id = _project(harness, template_docx, source_ids=["orders-src"])
    _add_source(harness, project_id, harness.root)
    _, job_dir = _submit(harness, project_id, source_ids=["orders-src"])

    violations = [item for item in _findings(job_dir, "compose") if item["rule_id"] == "FR-M5-17"]
    assert violations == []
