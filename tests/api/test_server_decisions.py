"""决策接缝在服务端的样子：待裁决状态、事件落盘、适配层注入与配置校验。

判据是「决策可路由」在**服务端**落地后的三个可观测后果：

1. **待裁决不是成功**。子作业落 ``awaiting_decision``，编排作业随之聚合，
   而产物与断点照常保留——来裁决的人要有东西可看（NFR-26 / 验收 39）。
2. **决策进得了事件流**。落成 ``event: decision`` 且写进该作业自己的
   ``events.jsonl``，SSE、断线重连、重启回放三条路才会同时成立。
3. **接模型只改一处**。适配层由组合根经 ``decide_session`` 注入，
   worker 不认识它；缺省则是确定性会话（不接模型、产出完整、token 为 0）。

本文件从 ``test_server_jobs.py`` 拆出：那边已经贴着 800 行上限（NFR-09），
而决策接缝是可以独立阅读的一层，混在作业队列的用例里两头都读不痛快。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from docfidelity.agents import AdapterOutcome, ScriptedAdapter
from docfidelity.contracts.config import DecisionPointRoute, RoutingConfig
from docfidelity.contracts.errors import ErrorCode
from docfidelity.contracts.job import Job
from docfidelity.pipeline import DecisionSession, EventLog, build_session
from server.api.deps import build_state, session_factory_from
from server.settings import ServerSettings
from tests.fixtures.server_fixture import (
    build_harness,
    build_registry,
    project_config,
)

pytestmark = pytest.mark.api


# ---------------------------------------------------------------- 决策接缝
#: 会问模型的决策点（小模型 + 回退标待确认）。
_DECISION_POINT = "placement.section_slot"
#: 从不问模型的决策点（规则 + 回退不标待确认）。
_RULE_POINT = "gate.judge"


def test_pending_decision_keeps_the_child_out_of_succeeded(tmp_path: Path) -> None:
    """阶段产出待裁决项 ⇒ 子作业落 ``awaiting_decision``，**产物照常登记**。

    不能落成 ``succeeded``：那等于宣布"这份可以定稿了"，而它其实在等人裁决
    （NFR-26 / 验收 39）。产物与断点都留着——"已产出"与"已采用"是两件事，
    停掉产物收集会让来裁决的人没有东西可看。
    """
    harness = build_harness(
        tmp_path,
        stages=build_registry(qa={"decide_point": _DECISION_POINT, "artifacts": ("out.docx",)}),
    )
    project_id = harness.new_project()
    orchestration_id = harness.build(project_id)

    children = harness.services.job_children(orchestration_id)
    assert [child.status for child in children] == ["awaiting_decision", "awaiting_decision"]

    decision = harness.stages["qa"].decisions[0]
    assert decision.status == "pending"
    assert (decision.declared, decision.effective) == ("small_model", "rule")
    for child in children:
        assert harness.repository.list_artifacts(child.job_id)  # 产物没有因此消失
        assert harness.runner.has_checkpoint(child.job_id, "qa")  # 断点也没有


def test_pending_decision_is_reported_by_run_outcome(tmp_path: Path) -> None:
    """调度侧的返回值要能回答"这一份为什么没成功"，而不是只给一个状态字。"""
    harness = build_harness(tmp_path, stages=build_registry(qa={"decide_point": _DECISION_POINT}))
    project_id = harness.new_project()
    harness.services.submit_build(project_id)
    outcomes = harness.runner.run_until_idle()

    assert outcomes and all(outcome.pending_decisions == 1 for outcome in outcomes)
    assert all(outcome.stopped_at is None for outcome in outcomes)  # 没有失败的阶段


def test_deterministic_default_records_a_rule_decision_without_a_model_call(
    tmp_path: Path,
) -> None:
    """缺省不接模型：决策有留痕、``source`` 是 ``rule_default``、token 为 0。

    断言 ``source`` 而不是只断言"取值等于规则默认值"——静默采用时取值也可能恰好相等。
    """
    harness = build_harness(tmp_path, stages=build_registry(qa={"decide_point": _RULE_POINT}))
    project_id = harness.new_project()
    orchestration_id = harness.build(project_id)

    decision = harness.stages["qa"].decisions[0]
    assert decision.source == "rule_default"
    assert decision.status == "auto"  # 规则点的回退不标待确认
    for child in harness.services.job_children(orchestration_id):
        assert child.status == "succeeded"


def test_pending_decision_aggregates_into_the_orchestration(tmp_path: Path) -> None:
    """子作业待裁决 ⇒ 编排作业也必须待裁决，否则界面上的待裁决徽标永远不亮。"""
    harness = build_harness(tmp_path, stages=build_registry(qa={"decide_point": _DECISION_POINT}))
    project_id = harness.new_project()
    orchestration_id = harness.build(project_id)

    parent = harness.repository.get_job(orchestration_id)
    assert parent.status == "awaiting_decision"
    assert parent.progress.failed == 0  # 待裁决不是失败
    assert parent.progress.done == parent.progress.total


def test_decision_events_are_persisted_into_the_job_event_log(tmp_path: Path) -> None:
    """决策落成 ``event: decision`` 并**先落盘再推送**（SSE / 重连 / 重启回放都靠它）。"""
    harness = build_harness(tmp_path, stages=build_registry(qa={"decide_point": _RULE_POINT}))
    project_id = harness.new_project()
    orchestration_id = harness.build(project_id)
    child_id = harness.services.job_children(orchestration_id)[0].job_id

    events = harness.runner.events.since(child_id, 0)
    decisions = [event for event in events if event.type == "decision"]
    assert len(decisions) == 1
    assert decisions[0].payload["kind"] == _RULE_POINT
    assert decisions[0].payload["record"] == "decision"
    # 单调序列没有被决策事件打断（续推靠它）
    assert [event.event_id for event in events] == list(range(1, len(events) + 1))

    path = harness.root / "jobs" / child_id / "events.jsonl"
    raw = path.read_text(encoding="utf-8")
    assert decisions[0].payload["decision_id"] in raw
    assert any(json.loads(line)["type"] == "decision" for line in raw.splitlines() if line.strip())


def test_injected_adapter_is_adopted_end_to_end(tmp_path: Path) -> None:
    """组合根注入适配层 ⇒ 模型结论被采用、作业照常成功、调用留痕带上 token。

    这是"接模型"的完整形状：worker 不认识适配层，``decide_session`` 认识，
    整条链路上只有这一个地方需要改。
    """
    adapter = ScriptedAdapter(
        script={
            _DECISION_POINT: AdapterOutcome(
                ok=True,
                schema_ok=True,
                payload={"choice": "章节三"},
                chosen="章节三",
                confidence=0.96,
                model="m-test",
                tokens_in=9,
                tokens_out=4,
            )
        },
        available_flag=True,
    )

    def factory(job: Job, routing: RoutingConfig | None, events: EventLog) -> DecisionSession:
        return build_session(
            routing, job_id=job.job_id, events=events, adapter=adapter, model_enabled=True
        )

    harness = build_harness(
        tmp_path,
        stages=build_registry(qa={"decide_point": _DECISION_POINT}),
        decide_session=factory,
    )
    orchestration_id = harness.build(harness.new_project())

    decisions = harness.stages["qa"].decisions
    assert decisions and all(item.source == "model" for item in decisions)
    assert all(item.chosen == "章节三" for item in decisions)
    assert all(item.status == "auto" for item in decisions)  # 达标即采用，不进裁决队列
    assert len(adapter.calls) == len(decisions)  # 每个子作业各问一次，重试为零
    assert [child.status for child in harness.services.job_children(orchestration_id)] == [
        "succeeded",
        "succeeded",
    ]

    child_id = harness.services.job_children(orchestration_id)[0].job_id
    events = harness.runner.events.since(child_id, 0)
    calls = [event.payload for event in events if event.payload.get("record") == "model_call"]
    assert calls and calls[0]["tokens_in"] == 9 and calls[0]["tokens_out"] == 4
    assert calls[0]["ok"] is True


def test_illegal_routing_config_fails_the_job_instead_of_killing_the_worker(
    tmp_path: Path,
) -> None:
    """配置试图把规则点**放宽**成模型路由 ⇒ 这一份作业失败并说明原因，而不是异常风暴。

    让异常直接冒出去的话，作业会永远停在"运行中"、每轮调度再抛一次同样的异常——
    界面上只看到"作业卡住了"，而原因在日志里被刷屏淹没。
    """
    harness = build_harness(tmp_path)
    project_id = harness.new_project()
    harness.services.put_config(
        project_id,
        project_config(project_id).model_copy(
            update={
                "routing": RoutingConfig(
                    decision_points={"geometry.compute": DecisionPointRoute(route="small_model")}
                )
            }
        ),
        author="case",
    )

    orchestration_id = harness.build(project_id)
    for child in harness.services.job_children(orchestration_id):
        assert child.status == "failed"
        assert child.error is not None
        assert child.error.code == ErrorCode.CONFIG_INVALID
    assert harness.repository.get_job(orchestration_id).status == "failed"
    assert harness.runner.run_until_idle() == []  # 没有留下"每轮都抛"的作业


def test_project_routing_config_is_honoured_even_without_a_model(tmp_path: Path) -> None:
    """不接模型时项目路由配置照样生效：把模型点收紧成规则，实际路径随之改变。

    忽略这份配置的话，"配置只能收紧不能放宽"就只是文档里的一句话，
    而运维会以为改配置能影响构建。
    """
    harness = build_harness(tmp_path, stages=build_registry(qa={"decide_point": _DECISION_POINT}))
    project_id = harness.new_project()
    harness.services.put_config(
        project_id,
        project_config(project_id).model_copy(
            update={
                "routing": RoutingConfig(
                    decision_points={_DECISION_POINT: DecisionPointRoute(route="rule")}
                )
            }
        ),
        author="case",
    )
    orchestration_id = harness.build(project_id)

    decision = harness.stages["qa"].decisions[0]
    assert decision.source == "rule_default"
    assert decision.status == "pending"  # 该点声明里就是 mark_pending
    assert harness.repository.get_job(orchestration_id).status == "awaiting_decision"


def test_a_dead_model_service_queues_every_decision_and_adopts_none(tmp_path: Path) -> None:
    """**TASK-P0-17 验收第 2 条**（设计 19.3 / 验收 39）：断开模型服务后低置信项**全部**
    进裁决队列，且无一项被静默采用。

    与上一条互为对立面：上一条是"答得又对又确定 ⇒ 自动采用、不进队列"，这一条是
    "端点不可达 ⇒ 每一项都进队列"。两条合起来才说明"进不进队列"由**结论质量**决定，
    而不是由"模型开没开"决定。

    三处判据缺一不可：

    1. **作业不是失败**。模型不可用只是决策降级（NFR-26），产物照常产出，作业只是
       "等人裁决"——把它判成失败会让人去修模型，而该做的是去看那几条待裁决。
    2. **每一项都在队列里**。用 ``decisions`` 与 ``pending_decisions`` **长度相等**来断言：
       只查后者会放过"另有一项被 ``auto`` 采用了"，而静默采用恰恰以"少了一条"的形式出现。
    3. **留痕是"试过但失败"**。断开服务仍会发生调用，与"没配模型"的零调用不是一回事；
       把这两者合并会让人去查错的地方（见 ``test_the_deterministic_session_…`` 的对立面）。
    """
    adapter = ScriptedAdapter(
        script={
            _DECISION_POINT: AdapterOutcome(
                ok=False, schema_ok=False, error=ErrorCode.MODEL_UNAVAILABLE, detail="端点不可达"
            )
        },
        available_flag=True,
    )

    def factory(job: Job, routing: RoutingConfig | None, events: EventLog) -> DecisionSession:
        return build_session(routing, job_id=job.job_id, events=events, adapter=adapter)

    harness = build_harness(
        tmp_path,
        stages=build_registry(qa={"decide_point": _DECISION_POINT}),
        decide_session=factory,
    )
    orchestration_id = harness.build(harness.new_project())
    children = harness.services.job_children(orchestration_id)

    assert children, "没有子作业就谈不上'全部进队列'"
    assert all(child.status == "awaiting_decision" for child in children)
    assert harness.repository.get_job(orchestration_id).status == "awaiting_decision"

    for child in children:
        recorded = harness.services.decisions(child.job_id)
        queued = harness.services.pending_decisions(child.job_id)
        assert len(recorded) == len(queued) == 1, "有一项没进队列 = 被静默采用了"
        assert recorded[0].status == "pending"
        assert recorded[0].source == "fallback"
        assert recorded[0].confidence is None, "降级结论不得带上模型的置信度"

    subjects = harness.services.inbox()
    assert len(subjects) == len(children)
    assert {item.kind for item in subjects} == {"decision"}

    assert adapter.calls, "断开服务 ≠ 没有发生调用"


# ------------------------------------------------------------------ 组合根
def _probe_job(job_id: str = "j-probe") -> Job:
    return Job(
        job_id=job_id,
        kind="document",
        project_id="p1",
        doc_key="detail",
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        inputs_hash="h-probe",
    )


def test_settings_are_the_only_thing_that_decides_whether_a_model_is_used(tmp_path: Path) -> None:
    """组合根：``settings.model_*`` 齐备 ⇒ 作业会话真的挂了那个端点。

    判据刻意落在**会话自述**上，而不是"适配层字段有没有读到配置"：没有这条连接的话，
    ``.env`` 配得再对也没有任何代码会去调模型，而 ``/health`` 的数据边界仍报
    ``external_calls=true``——界面与报告上完全看不出"其实一次都没问过模型"。

    这条用例**不发任何请求**（只建会话）：端点是 ``127.0.0.1:9``，真调必然连接被拒。
    """
    settings = ServerSettings(
        _env_file=None,
        storage_root=tmp_path,
        model_enabled=True,
        model_base_url="http://127.0.0.1:9/v1",
        model_name="m-1",
    )
    state = build_state(settings=settings)

    described = state.runner.session_for(_probe_job()).adapter_description()

    assert described["kind"] == "openai_agents"
    assert described["model"] == "m-1"
    assert described["base_url"] == "http://127.0.0.1:9/v1"
    assert described["available"] is True


def test_partial_model_settings_fall_back_to_the_deterministic_session(tmp_path: Path) -> None:
    """三样缺一即退确定性会话，**而不是**挂着半好不坏的适配层。

    三样各自的理由不同（见 ``session_factory_from``）：开关是最外层；端点与
    ``data_boundary.external_calls`` 同一口径；模型标识缺了会让 SDK 拿**自己的**
    默认模型去配你给的端点——名字对不上、多数网关直接 4xx，而界面上一切"就绪"。
    """
    cases = {
        "开关没开": {},
        "没有端点": {"model_enabled": True, "model_name": "m-1"},
        "没有模型标识": {"model_enabled": True, "model_base_url": "http://127.0.0.1:9/v1"},
    }
    for index, (reason, patch) in enumerate(cases.items()):
        settings = ServerSettings(_env_file=None, storage_root=tmp_path / str(index), **patch)

        factory = session_factory_from(settings)

        assert factory is None, reason


def test_the_deterministic_session_is_still_reachable_through_build_state(tmp_path: Path) -> None:
    """没配模型时走 worker 的确定性路径：全链路规则、token 为 0（验收 39）。"""
    state = build_state(settings=ServerSettings(_env_file=None, storage_root=tmp_path))

    described = state.runner.session_for(_probe_job()).adapter_description()

    assert described["kind"] == "null"
    assert described["available"] is False


def test_an_explicit_factory_still_overrides_the_settings(tmp_path: Path) -> None:
    """显式注入是**覆盖**：开发服务器靠它把演示用的脚本化适配层挂上去。

    若配置优先于注入，``scripts/serve_dev.py`` 会开始按开发机上的 ``.env`` 真调模型
    ——而它存在的意义正是"不要求 API key 也能看完整的决策链形状"。
    """
    scripted = ScriptedAdapter(script={_DECISION_POINT: AdapterOutcome(ok=True, confidence=0.99)})

    def factory(job: Job, routing: RoutingConfig | None, events: EventLog) -> DecisionSession:
        return build_session(routing, job_id=job.job_id, events=events, adapter=scripted)

    settings = ServerSettings(
        _env_file=None,
        storage_root=tmp_path,
        model_enabled=True,
        model_base_url="http://127.0.0.1:9/v1",
        model_name="m-1",
    )
    state = build_state(settings=settings, decide_session=factory)

    assert state.runner.session_for(_probe_job()).adapter_description()["kind"] == "scripted"


# ------------------------------------------------- 基线评测接进组合根（P0-17）
@pytest.mark.parametrize("rate, expected", [(0.0, "rule"), (1.0, "small_model")])
def test_the_storage_root_holds_the_baseline_the_router_obeys(
    tmp_path: Path, rate: float, expected: str
) -> None:
    """组合根把 ``<storage_root>/baseline.json`` 交给工厂，路由**真的**受它约束。

    这一条串起整条链：``settings.storage_root`` → ``session_factory_from`` →
    ``model_session_factory(baseline_path=…)`` → ``build_routing(baseline=…)``。
    任何一环断了都不会报错——只会"评测跑完，路由没变"，而那在界面上与"评测通过、
    本就该交给模型"长得一模一样。

    文件名刻意**不在这里写死**：用例通过 ``BASELINE_FILENAME`` 取，故改名这条链
    依然成立（换个字面量就会变成"测试自己实现了第二份命名约定"）。
    """
    from docfidelity.agents.baseline import (
        BaselineReport,
        baseline_digests,
        default_cases,
        save_report,
    )
    from docfidelity.agents.routing import build_routing
    from docfidelity.pipeline import BASELINE_FILENAME

    cases_digest, routing_digest = baseline_digests(
        default_cases(), build_routing(model_enabled=True).describe()
    )
    save_report(
        BaselineReport(
            model="m-1",
            cases_digest=cases_digest,
            routing_digest=routing_digest,
            by_point={_DECISION_POINT: rate},
        ),
        tmp_path / BASELINE_FILENAME,
    )
    settings = ServerSettings(
        _env_file=None,
        storage_root=tmp_path,
        model_enabled=True,
        model_base_url="http://127.0.0.1:9/v1",
        model_name="m-1",
    )
    state = build_state(settings=settings)

    records = {item["id"]: item for item in state.runner.session_for(_probe_job()).routing()}

    assert records[_DECISION_POINT]["effective"] == expected
    assert "基线" in records[_DECISION_POINT]["reason"]
    assert records[_RULE_POINT]["effective"] == "rule", "规则档与基线无关"
