"""``pipeline.decisions``：决策接缝——会话、留痕落点、阶段上下文与汇总。

覆盖规则点零模型调用、失败与低置信不静默采用、事件留痕、失败优先于待裁决。
模型行为断言来源与实际调用，不以结果值恰好相同代替路由验证。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from docfidelity.agents import AdapterOutcome, MemoryRecorder, ScriptedAdapter
from docfidelity.contracts.config import DecisionPointRoute, RoutingConfig
from docfidelity.contracts.errors import ConfigurationError, EngineError, ErrorCode
from docfidelity.contracts.job import Decision
from docfidelity.pipeline import (
    DECISION_EVENT_TYPE,
    RECORD_DECISION,
    RECORD_MODEL_CALL,
    DecisionSession,
    EventLog,
    JournalRecorder,
    MemoryCheckpointStore,
    Workspace,
    build_session,
    model_session_factory,
    rule_only_session,
    run_document,
    run_stage,
)
from tests.fixtures.pipeline_fixture import (
    all_stages,
    build_registry,
    clock,
    document_job,
)

pytestmark = pytest.mark.unit

#: 一个"会问模型"的点（小模型 + 回退标待确认）。
MODEL_POINT = "placement.section_slot"
#: 一个"从不问模型"的点（规则 + 回退不标待确认）。
RULE_POINT = "gate.judge"
#: 一个"必须人工"的点。
HUMAN_POINT = "source.conflict"
#: 一个"可选大模型 + 需确认"的点。
CONFIRM_POINT = "review.document"


def _answer(
    *,
    chosen: str = "选项甲",
    confidence: float | None = 0.95,
    ok: bool = True,
    schema_ok: bool = True,
) -> AdapterOutcome:
    return AdapterOutcome(
        ok=ok,
        schema_ok=schema_ok,
        payload={"unit_ref": "sec-1", "chapter_path": [chosen]} if ok else None,
        chosen=chosen if ok else None,
        confidence=confidence,
        model="m-test",
        tokens_in=11 if ok else None,
        tokens_out=7 if ok else None,
        latency_ms=5,
    )


def _scripted(**overrides: AdapterOutcome) -> ScriptedAdapter:
    return ScriptedAdapter(script=dict(overrides), available_flag=True)


def _schema_broken() -> AdapterOutcome:
    """``ok=True`` 但 ``schema_ok=False``：模型答了、答的东西不符合输出契约。

    与"调用失败"必须分开构造：两者的重试理由不同（格式能修，连不上只能等），
    而把它们混成一个 ``ok=False`` 会让"重试有用吗"这个问题在报告里无法回答。
    """
    return AdapterOutcome(
        ok=True,
        schema_ok=False,
        payload=None,
        chosen=None,
        confidence=0.9,
        model="m-test",
        detail="缺少必填字段 choice",
    )


def _workspace(tmp_path: Path, job_id: str = "j-doc-1") -> Workspace:
    return Workspace(tmp_path, job_id)


# --------------------------------------------------------------------- 留痕
class TestJournalRecorder:
    def test_decision_lands_as_an_event_carrying_the_contract_record(self) -> None:
        """决策必须落成 ``event: decision``，且载荷就是契约对象本身。

        载荷用 ``model_dump(mode="json")`` 而不是另挑几个字段：界面与报告要按契约
        渲染，少一个字段就意味着"这条信息只存在于内核里"。
        """
        log = EventLog()
        session = build_session(None, job_id="j1", events=log)
        session.decide(RULE_POINT, {"k": 1}, target="r-1")

        events = log.events("j1")
        assert len(events) == 1
        event = events[0]
        assert event.type == DECISION_EVENT_TYPE
        assert event.payload["record"] == RECORD_DECISION
        assert event.payload["decision_id"] == session.decisions()[0].decision_id
        assert event.payload["kind"] == RULE_POINT
        assert event.payload["target"] == "r-1"
        assert event.payload["status"] == "auto"

    def test_decision_events_share_the_sequence_with_other_events(self) -> None:
        """决策事件与阶段事件**共用**一条单调序列，否则 ``Last-Event-ID`` 续推会有洞。"""
        log = EventLog()
        log.emit_stage("j1", "render", "running", 0.0)
        session = build_session(None, job_id="j1", events=log)
        session.decide(RULE_POINT, {})
        log.emit_stage("j1", "render", "succeeded", 1.0)

        assert [event.event_id for event in log.events("j1")] == [1, 2, 3]
        assert [event.type for event in log.events("j1")] == ["stage", "decision", "stage"]

    def test_model_call_is_recorded_under_the_same_event_type(self) -> None:
        """模型调用与决策共用事件类型、靠 ``record`` 判别——契约的 ``EventType`` 是闭集。"""
        log = EventLog()
        adapter = _scripted(**{MODEL_POINT: _answer()})
        session = build_session(None, job_id="j1", events=log, adapter=adapter)
        session.decide(MODEL_POINT, {}, target="sec-1", candidates=["选项甲"])

        kinds = [event.payload["record"] for event in log.events("j1")]
        assert kinds == [RECORD_DECISION, RECORD_MODEL_CALL]

    def test_query_surface_is_delegated_not_reimplemented(self) -> None:
        """查询面与 ``MemoryRecorder`` 是同一份：``describe()`` 的数字必须自洽。"""
        session = build_session(None, job_id="j1")
        session.decide(MODEL_POINT, {})
        session.decide(RULE_POINT, {})

        describe = session.recorder.describe()
        assert describe["decisions"] == len(session.decisions()) == 2
        assert describe["pending"] == len(session.pending_decisions()) == 1
        assert describe["tokens_in"] == describe["tokens_out"] == 0

    def test_mirror_receives_every_record(self) -> None:
        """落库接缝：镜像收到与内存同样多的记录，而查询面仍走内存那份。"""
        mirror = MemoryRecorder(job_id="j1")
        session = build_session(None, job_id="j1", mirror=mirror)
        session.decide(RULE_POINT, {})

        assert len(mirror.decisions) == len(session.decisions()) == 1
        assert session.decisions()[0].decision_id == mirror.decisions[0].decision_id

    def test_illegal_job_id_is_rejected_before_any_decision(self) -> None:
        """作业 id 要当路径片段用，先在构造期拒掉——写盘那一刻才发现时决策已发生。"""
        with pytest.raises(ConfigurationError):
            JournalRecorder(EventLog(), job_id="a/b")

    def test_records_are_kept_even_when_the_store_never_sees_them(self) -> None:
        """未给事件日志时仍留内存（测试与无盘场合），而不是静默丢弃。"""
        recorder = JournalRecorder(EventLog(), job_id="j1")
        recorder.record_decision(_decision())
        assert len(recorder.decisions()) == 1


def _decision(status: str = "auto") -> Decision:
    from datetime import datetime, timezone

    return Decision(
        decision_id="d_test",
        job_id="j1",
        kind=RULE_POINT,
        target="r-1",
        chosen="选项甲",
        reason="用例构造",
        source="rule_default",
        status=status,  # type: ignore[arg-type]
        created_at=datetime(2026, 9, 23, tzinfo=timezone.utc),
    )


# --------------------------------------------------------------------- 会话
class TestDecisionSession:
    def test_rule_point_never_touches_the_adapter(self) -> None:
        """规则点**一次都不问模型**——哪怕适配层可用、脚本里还备好了答案。"""
        adapter = _scripted(**{RULE_POINT: _answer()})
        session = build_session(None, job_id="j1", adapter=adapter)
        outcome = session.decide(RULE_POINT, {}, target="r-1")

        assert adapter.calls == []
        assert outcome.source == "rule_default"
        assert outcome.declared == outcome.effective == "rule"
        assert session.model_calls() == ()

    def test_human_point_never_touches_the_adapter(self) -> None:
        """人工档同理：属业务判断，模型不参与（FR-M1-11）。"""
        adapter = _scripted(**{HUMAN_POINT: _answer()})
        session = build_session(None, job_id="j1", adapter=adapter)
        outcome = session.decide(HUMAN_POINT, {})

        assert adapter.calls == []
        assert outcome.effective == "human"
        assert outcome.source == "manual"
        assert outcome.pending is True

    def test_unavailable_adapter_falls_back_and_marks_pending(self) -> None:
        """适配层不可用 ⇒ 回退规则默认值 + 标待确认，**构建不因此失败**（验收 39）。

        刻意断言 ``model_calls()`` 为空：适配层报"不可用"时**根本没有发生调用**，
        补一条 ``ok=False`` 的调用记录会把"没试过"写成"试过但失败了"——
        后者会让运维去查模型服务，而真相是这个进程压根没打算问它。
        """
        session = build_session(None, job_id="j1", adapter=ScriptedAdapter(available_flag=False))
        outcome = session.decide(MODEL_POINT, {}, target="sec-1", candidates=["选项甲"])

        assert (outcome.declared, outcome.effective) == ("small_model", "rule")
        assert outcome.source == "fallback"
        assert outcome.fallback_used is True
        assert outcome.status == "pending"
        assert outcome.attempts == 0
        assert "模型不可用" in outcome.reason
        assert session.model_calls() == ()
        assert session.silent_adoptions() == ()

    def test_confident_model_answer_is_adopted(self) -> None:
        """置信度达标 ⇒ 采用模型结论，并留下可解释的理由与调用记录。"""
        adapter = _scripted(**{MODEL_POINT: _answer(chosen="章节三")})
        session = build_session(None, job_id="j1", adapter=adapter)
        outcome = session.decide(MODEL_POINT, {}, target="sec-1", candidates=["章节三", "章节四"])

        assert outcome.source == "model"
        assert outcome.chosen == "章节三"
        assert outcome.declared == outcome.effective == "small_model"
        assert outcome.status == "auto"
        assert outcome.confidence == 0.95
        assert session.silent_adoptions() == ()
        call = session.model_calls()[0]
        assert (call.tokens_in, call.tokens_out) == (11, 7)
        assert call.ok is True

    def test_low_confidence_is_retried_with_feedback_then_queued(self) -> None:
        """低置信 ⇒ 带上次失败原因重试到预算用尽，仍不达标就进裁决队列。"""
        adapter = _scripted(**{MODEL_POINT: _answer(confidence=0.2)})
        session = build_session(None, job_id="j1", adapter=adapter)
        outcome = session.decide(MODEL_POINT, {}, target="sec-1", candidates=["选项甲"])

        assert outcome.status == "pending"
        assert outcome.fallback_used is True
        assert outcome.attempts == 3  # 首次 + 2 次重试
        assert len(adapter.calls) == 3
        assert adapter.calls[0].feedback == []
        assert "置信度" in adapter.calls[1].feedback[0]
        assert session.silent_adoptions() == ()

    def test_missing_confidence_counts_as_not_met(self) -> None:
        """模型没报置信度一律按不达标处理——把"没说"当成"很有把握"正是静默采用的入口。"""
        adapter = _scripted(**{MODEL_POINT: _answer(confidence=None)})
        session = build_session(None, job_id="j1", adapter=adapter)
        outcome = session.decide(MODEL_POINT, {}, target="sec-1", candidates=["选项甲"])

        assert outcome.source == "fallback"
        assert outcome.status == "pending"
        assert session.silent_adoptions() == ()

    def test_schema_invalid_output_is_retried_then_queued(self) -> None:
        """格式不合法与置信度不足是两件事：前者重试有意义，故必须也走重试。"""
        adapter = _scripted(**{MODEL_POINT: _schema_broken()})
        session = build_session(None, job_id="j1", adapter=adapter)
        outcome = session.decide(MODEL_POINT, {}, target="sec-1", candidates=["选项甲"])

        assert len(adapter.calls) == 3
        assert "Schema" in adapter.calls[1].feedback[0]
        assert "choice" in adapter.calls[1].feedback[0]  # 重试要带上"缺哪个字段"
        assert outcome.status == "pending"
        assert session.model_calls()[0].ok is True  # 调用本身成功，败在输出契约上

    def test_confirm_level_still_queues_a_confident_answer(self) -> None:
        """可选大模型 + ``level=confirm``：置信度再高也要过确认，不能自动采用。"""
        adapter = _scripted(**{CONFIRM_POINT: _answer(confidence=0.99)})
        session = build_session(None, job_id="j1", adapter=adapter)
        outcome = session.decide(CONFIRM_POINT, {})

        assert outcome.source == "model"
        assert outcome.status == "pending"
        assert session.pending_decisions()[0].kind == CONFIRM_POINT

    def test_model_switch_off_keeps_the_chain_intact(self) -> None:
        """总开关关闭：全链路走规则、无 token 消耗，产物仍完整。"""
        adapter = _scripted(**{MODEL_POINT: _answer()})
        session = build_session(None, job_id="j1", adapter=adapter, model_enabled=False)
        outcome = session.decide(MODEL_POINT, {})

        assert adapter.calls == []
        assert outcome.effective == "rule"
        assert outcome.declared == "small_model"
        assert session.recorder.token_totals() == (0, 0)

    def test_repeated_decisions_get_distinct_ids(self) -> None:
        """同一作业内对同一对象的重复同结论决策，标识必须不同（靠会话序号）。"""
        session = build_session(None, job_id="j1")
        session.decide(RULE_POINT, {}, target="r-1")
        session.decide(RULE_POINT, {}, target="r-1")

        ids = [item.decision_id for item in session.decisions()]
        assert len(set(ids)) == 2

    def test_unknown_decision_point_reports_a_config_error(self) -> None:
        """写错决策点 id 是调用方缺陷：报"未登记"而不是"引擎少了条记录"。"""
        session = build_session(None, job_id="j1")
        with pytest.raises(EngineError) as caught:
            session.decide("not.a.decision_point", {})
        assert caught.value.code == ErrorCode.CONFIG_INVALID

    def test_routing_view_lists_every_point_with_declared_and_effective(self) -> None:
        """验收 41 的数据面：每个决策点的声明归属与实际路径都在。"""
        session = build_session(None, job_id="j1")
        records = {record["id"]: record for record in session.routing()}
        assert len(records) == len(session.table.points())
        for record in records.values():
            assert {"declared", "effective", "level", "strategy", "reason"} <= set(record)

    def test_summary_reports_the_adapter_without_any_secret(self) -> None:
        session = rule_only_session("j1")
        summary = session.summary()
        assert summary["adapter"]["kind"] == "null"
        assert summary["adapter"]["available"] is False
        assert summary["job_id"] == "j1"

    def test_config_can_tighten_a_point_and_that_shows_up_in_the_session(self) -> None:
        """项目配置在这里才真正生效：把模型点收紧成规则，会话里就不再问模型。"""
        config = RoutingConfig(
            decision_points={MODEL_POINT: DecisionPointRoute(route="rule")},
        )
        adapter = _scripted(**{MODEL_POINT: _answer()})
        session = build_session(config, job_id="j1", adapter=adapter, model_enabled=True)
        outcome = session.decide(MODEL_POINT, {})

        assert adapter.calls == []
        assert outcome.effective == "rule"

    def test_config_cannot_widen_a_rule_point(self) -> None:
        """反方向被拒：把规则点改成模型路由会让分级表退化成一份默认值（需求 5.2.3）。"""
        config = RoutingConfig(
            decision_points={"geometry.compute": DecisionPointRoute(route="small_model")},
        )
        with pytest.raises(EngineError) as caught:
            build_session(config, job_id="j1")
        assert caught.value.code == ErrorCode.CONFIG_INVALID


# --------------------------------------------------- 真模型的会话工厂（A/组合根）
class TestModelSessionFactory:
    """``model_session_factory``：把"接一个真端点"折成可注入的作业级会话工厂。

    它是 ``pipeline`` 与 ``agents`` 那条 seam 的第二半（第一半是 ``build_session``）：
    ``server`` 不得 import ``agents``，所以"怎么把端点折成适配层"只能在这里回答一次，
    服务端的组合根只负责把 ``settings.model_*`` 读出来。
    """

    def test_the_factory_wires_a_real_adapter(self) -> None:
        factory = model_session_factory(model="m-1", base_url="http://127.0.0.1:9/v1")

        session = factory(document_job(), None, EventLog())

        described = session.adapter_description()
        assert described["kind"] == "openai_agents"
        assert described["model"] == "m-1"
        assert described["base_url"] == "http://127.0.0.1:9/v1"

    def test_the_factory_reports_the_model_as_enabled(self) -> None:
        """工厂接了模型且该点有已验证的基线，才允许模型路由。"""
        factory = model_session_factory(
            model="m-1", base_url="http://127.0.0.1:9/v1", baseline={MODEL_POINT: 1.0}
        )

        records = {
            record["id"]: record for record in factory(document_job(), None, EventLog()).routing()
        }

        assert records[MODEL_POINT]["effective"] == "small_model"

    def test_one_adapter_is_shared_by_every_job(self) -> None:
        """适配层只建一次，故 SDK 客户端也不随作业数增长（接缝放编排层要避免的第一个后果）。"""
        factory = model_session_factory(model="m-1", base_url="http://127.0.0.1:9/v1")

        first = factory(document_job("j-a"), None, EventLog())
        second = factory(document_job("j-b"), None, EventLog())

        assert first.adapter is second.adapter

    def test_the_factory_never_leaks_the_key(self) -> None:
        """NFR-21：自述会进日志与界面，密钥只进不出。"""
        factory = model_session_factory(
            model="m-1", base_url="http://127.0.0.1:9/v1", api_key="sk-leak-me"
        )

        described = factory(document_job(), None, EventLog()).adapter_description()

        assert "sk-leak-me" not in repr(described)

    def test_the_job_id_comes_from_the_job_not_from_the_factory(self) -> None:
        """会话与作业一一对应：留痕的 ``job_id`` 必须是被服务的那一个，不能是工厂自己的。"""
        factory = model_session_factory(model="m-1", base_url="http://127.0.0.1:9/v1")

        session = factory(document_job("j-42"), None, EventLog())

        assert session.summary()["job_id"] == "j-42"


# ------------------------------------------------ 基线评测接进路由（P0-17 / NFR-23）
class TestBaselineWiring:
    """``baseline_path``：把"评测报告"接到"该不该问模型"上（NFR-23 的能力门槛）。

    在此之前 ``resolve_routes(baseline=…)`` 有能力门限、但**没有数据来源**：
    形参收着、服务端传空，于是每个点都按目录声明路由——"决策点分级"停留在一句
    设计声称。这一组用例盯的就是"那份数据真的接上了"。
    """

    @staticmethod
    def _write(
        tmp_path: Path,
        by_point: dict[str, float],
        *,
        model: str = "m-1",
        cases_digest: str | None = None,
        routing_digest: str | None = None,
        routing: RoutingConfig | None = None,
        endpoint_fingerprint: str | None = None,
    ) -> Path:
        from docfidelity.agents.baseline import (
            BaselineReport,
            default_cases,
            endpoint_digest,
            save_report,
        )
        from docfidelity.agents.routing import build_routing
        from docfidelity.platform.hashing import digest_payload

        report = BaselineReport(
            model=model,
            endpoint_digest=endpoint_digest("http://127.0.0.1:9/v1")
            if endpoint_fingerprint is None
            else endpoint_fingerprint,
            cases_digest=cases_digest
            if cases_digest is not None
            else digest_payload([case.model_dump(mode="json") for case in default_cases()]),
            routing_digest=routing_digest
            if routing_digest is not None
            else digest_payload(build_routing(routing, model_enabled=True).describe()),
            by_point=dict(by_point),
        )
        return save_report(report, tmp_path / "baseline.json")

    @staticmethod
    def _effective(factory: object, point: str) -> tuple[str, str]:
        session = factory(document_job("j-b"), None, EventLog())  # type: ignore[operator]
        record = next(item for item in session.routing() if item["id"] == point)
        return record["effective"], record["reason"]

    def test_a_below_threshold_point_is_forced_to_rules(self, tmp_path: Path) -> None:
        """未达通过率的点被强制由规则接管，且**原因里带着那个数字**。

        只断言 ``effective == "rule"`` 是不够的：总开关关闭、配置收紧、基线不足
        都会给出同一个取值。原因文本是唯一能区分它们的东西，也是界面照抄的那句话。
        """
        path = self._write(tmp_path, {MODEL_POINT: 0.4})
        factory = model_session_factory(
            model="m-1", base_url="http://127.0.0.1:9/v1", baseline_path=path
        )

        effective, reason = self._effective(factory, MODEL_POINT)

        assert effective == "rule"
        assert "0.40" in reason and "基线" in reason

    def test_a_point_above_threshold_keeps_its_declared_route(self, tmp_path: Path) -> None:
        """对立断言：达标即按声明交给模型——门槛不是"一律接管"。"""
        path = self._write(tmp_path, {MODEL_POINT: 1.0})
        factory = model_session_factory(
            model="m-1", base_url="http://127.0.0.1:9/v1", baseline_path=path
        )

        effective, reason = self._effective(factory, MODEL_POINT)

        assert effective == "small_model"
        assert "达标" in reason

    def test_the_report_is_reread_for_every_job(self, tmp_path: Path) -> None:
        """**每个作业现读**报告：跑完评测不必重启服务。

        构造期读一次会让"我刚跑完评测，界面却没变"变成一个只能靠重启解释的现象，
        而评测本就是运维动作。
        """
        path = self._write(tmp_path, {MODEL_POINT: 0.4})
        factory = model_session_factory(
            model="m-1", base_url="http://127.0.0.1:9/v1", baseline_path=path
        )
        assert self._effective(factory, MODEL_POINT)[0] == "rule"

        # 同一个工厂，只把报告换成"达标"——不重建工厂、不重启。
        self._write(tmp_path, {MODEL_POINT: 1.0})

        assert self._effective(factory, MODEL_POINT)[0] == "small_model"

    def test_unreadable_report_degrades_to_rule(self, tmp_path: Path) -> None:
        """坏报告不得授权模型，也不得让构建失败。"""
        broken = tmp_path / "baseline.json"
        broken.write_text("{ not json", encoding="utf-8")
        factory = model_session_factory(
            model="m-1", base_url="http://127.0.0.1:9/v1", baseline_path=broken
        )

        effective, reason = self._effective(factory, MODEL_POINT)

        assert effective == "rule"
        assert "无法解析" in reason

    def test_missing_report_degrades_to_rule(self, tmp_path: Path) -> None:
        """没跑过评测时生产构建走规则，待确认而不失败。"""
        factory = model_session_factory(
            model="m-1",
            base_url="http://127.0.0.1:9/v1",
            baseline_path=tmp_path / "never-written.json",
        )

        assert self._effective(factory, MODEL_POINT)[0] == "rule"

    @pytest.mark.parametrize(
        "identity",
        [
            {"model": "other-model"},
            {"cases_digest": "other-cases"},
            {"routing_digest": "other-routing"},
            {"routing_digest": ""},
        ],
    )
    def test_stale_report_cannot_authorize_model(
        self, tmp_path: Path, identity: dict[str, str]
    ) -> None:
        path = self._write(tmp_path, {MODEL_POINT: 1.0}, **identity)
        factory = model_session_factory(
            model="m-1", base_url="http://127.0.0.1:9/v1", baseline_path=path
        )
        assert self._effective(factory, MODEL_POINT)[0] == "rule"

    @pytest.mark.parametrize("report_matches", [False, True])
    def test_changed_runtime_routing_requires_new_report(
        self, tmp_path: Path, report_matches: bool
    ) -> None:
        routing = RoutingConfig(
            decision_points={MODEL_POINT: DecisionPointRoute(confidence_threshold=0.0)}
        )
        path = self._write(
            tmp_path, {MODEL_POINT: 1.0}, routing=routing if report_matches else None
        )
        factory = model_session_factory(
            model="m-1", base_url="http://127.0.0.1:9/v1", baseline_path=path
        )
        session = factory(document_job(), routing, EventLog())
        record = next(item for item in session.routing() if item["id"] == MODEL_POINT)
        assert record["effective"] == ("small_model" if report_matches else "rule")

    @pytest.mark.parametrize("fixed", [True, False])
    def test_uncovered_baseline_point_is_not_authorized(self, tmp_path: Path, fixed: bool) -> None:
        rates = {MODEL_POINT: 1.0}
        factory = model_session_factory(
            model="m-1",
            base_url="http://127.0.0.1:9/v1",
            baseline=rates if fixed else None,
            baseline_path=None if fixed else self._write(tmp_path, rates),
        )
        assert self._effective(factory, "dedup.semantic_duplicate")[0] == "rule"

    @pytest.mark.parametrize("rate", [-1.0, 2.0, float("nan"), float("inf")])
    def test_invalid_pass_rates_cannot_authorize_model(self, tmp_path: Path, rate: float) -> None:
        path = self._write(tmp_path, {MODEL_POINT: rate})
        factory = model_session_factory(
            model="m-1", base_url="http://127.0.0.1:9/v1", baseline_path=path
        )
        assert self._effective(factory, MODEL_POINT)[0] == "rule"

    @pytest.mark.parametrize("point", [MODEL_POINT, "copy.generate", "review.document"])
    def test_unverified_production_decisions_are_pending_without_model_calls(
        self, monkeypatch: pytest.MonkeyPatch, point: str
    ) -> None:
        import docfidelity.pipeline.decisions as decisions

        adapter = ScriptedAdapter(available_flag=True)
        monkeypatch.setattr(decisions, "OpenAIAgentsAdapter", lambda **_kwargs: adapter)
        factory = model_session_factory(model="m-1", base_url="http://127.0.0.1:9/v1")
        session = factory(document_job(), None, EventLog())
        outcome = session.decide(point)
        assert outcome.effective == "rule"
        assert outcome.pending is True
        assert session.has_pending_decision is True
        assert session.model_calls() == ()
        assert adapter.calls == []
        rule = session.decide(RULE_POINT)
        assert rule.effective == "rule"
        assert rule.pending is False

    def test_explicit_baseline_wins_over_the_path(self, tmp_path: Path) -> None:
        """显式给的映射优先（测试与嵌入方用），路径只在这时退居其次。"""
        path = self._write(tmp_path, {MODEL_POINT: 1.0})
        factory = model_session_factory(
            model="m-1",
            base_url="http://127.0.0.1:9/v1",
            baseline={MODEL_POINT: 0.0},
            baseline_path=path,
        )

        assert self._effective(factory, MODEL_POINT)[0] == "rule"


# ------------------------------------------------------- 阶段上下文与批处理
class TestStageWiring:
    def test_stage_context_is_always_bound_to_a_session(self, tmp_path: Path) -> None:
        """阶段体不必判空：``StageContext.decisions`` 是必填字段，永远有会话。"""
        seen: list[DecisionSession] = []

        def probe(context: object) -> object:
            from docfidelity.pipeline import StageOutcome

            session = context.decisions  # type: ignore[attr-defined]
            assert session.job_id == context.job.job_id  # type: ignore[attr-defined]
            seen.append(session)
            return StageOutcome()

        from docfidelity.pipeline import StageContext as _Context

        class _Probe:
            name = "render"

            def __call__(self, context: _Context):  # type: ignore[no-untyped-def]
                return probe(context)

        run_stage(
            job=document_job(),
            stage="render",
            registry=build_registry(_Probe()),  # type: ignore[arg-type]
            store=MemoryCheckpointStore(),
            workspace=_workspace(tmp_path),
            inputs_hash="h1",
            now=clock,
        )
        assert len(seen) == 1

    def test_run_stage_without_a_session_is_deterministic(self, tmp_path: Path) -> None:
        """不给会话时的默认是**确定性**会话：模型点也走规则、不消耗 token。"""
        stage = all_stages(**{"qa": {"decide_point": MODEL_POINT}})[-1]
        run = run_stage(
            job=document_job(),
            stage="qa",
            registry=build_registry(stage),
            store=MemoryCheckpointStore(),
            workspace=_workspace(tmp_path),
            inputs_hash="h1",
            now=clock,
        )

        assert run.status == "succeeded"
        assert stage.decisions[0].effective == "rule"
        assert stage.decisions[0].declared == "small_model"
        assert stage.calls[0].decisions is not None

    def test_a_passed_session_receives_the_stage_decisions(self, tmp_path: Path) -> None:
        log = EventLog()
        session = build_session(None, job_id="j-doc-1", events=log)
        stage = all_stages(**{"qa": {"decide_point": RULE_POINT}})[-1]
        run_stage(
            job=document_job(),
            stage="qa",
            registry=build_registry(stage),
            store=MemoryCheckpointStore(),
            workspace=_workspace(tmp_path),
            inputs_hash="h1",
            decisions=session,
            now=clock,
        )

        assert len(session.decisions()) == 1
        assert all(event.type == DECISION_EVENT_TYPE for event in log.events("j-doc-1"))
        assert [event.payload["record"] for event in log.events("j-doc-1")] == [
            "stage_start",
            RECORD_DECISION,
        ]


class TestDocumentRun:
    def test_pending_decision_keeps_the_job_out_of_succeeded(self, tmp_path: Path) -> None:
        """有阶段产出了待裁决项 ⇒ 作业不是 ``succeeded``，而是 ``awaiting_decision``。

        这一条不能省：并进 ``succeeded`` 等于宣布"这份产物可以定稿了"，
        而它其实在等人裁决（NFR-26 / 验收 39）。
        """
        registry = build_registry(
            *all_stages(**{"compose": {"decide_point": MODEL_POINT}}),
        )
        run = run_document(
            job=document_job(),
            registry=registry,
            store=MemoryCheckpointStore(),
            workspace=_workspace(tmp_path),
            inputs_hash="h1",
            now=clock,
        )

        assert run.status == "awaiting_decision"
        assert run.stopped_at is None  # 全部阶段都跑完了
        assert len(run.pending_decisions()) == 1
        assert run.has_pending_decision is True
        assert run.pending_decisions()[0].kind == MODEL_POINT

    def test_rule_only_decisions_leave_the_job_succeeded(self, tmp_path: Path) -> None:
        """规则点的回退不标待确认，故作业照常 ``succeeded``——不能把每个决策都当待裁决。"""
        registry = build_registry(*all_stages(**{"qa": {"decide_point": RULE_POINT}}))
        run = run_document(
            job=document_job(),
            registry=registry,
            store=MemoryCheckpointStore(),
            workspace=_workspace(tmp_path),
            inputs_hash="h1",
            now=clock,
        )

        assert run.status == "succeeded"
        assert len(run.decisions()) == 1
        assert run.pending_decisions() == ()

    def test_failure_wins_over_pending_decisions(self, tmp_path: Path) -> None:
        """阶段既做了决策又失败了 ⇒ 状态是 ``failed``：待裁决的是产物，而这份没有产物。"""
        registry = build_registry(
            *all_stages(**{"render": {"decide_point": MODEL_POINT}, "qa": {"failures": 1}}),
        )
        run = run_document(
            job=document_job(),
            registry=registry,
            store=MemoryCheckpointStore(),
            workspace=_workspace(tmp_path),
            inputs_hash="h1",
            now=clock,
        )

        assert run.status == "failed"
        assert run.stopped_at == "qa"
        assert len(run.pending_decisions()) == 1  # 决策确实发生过

    def test_all_stages_share_one_session_so_ids_stay_unique(self, tmp_path: Path) -> None:
        """一个作业一个会话：两个阶段各做一次同点决策，标识必须不同。"""
        registry = build_registry(
            *all_stages(
                **{
                    "compose": {"decide_point": RULE_POINT, "decide_target": "r-1"},
                    "qa": {"decide_point": RULE_POINT, "decide_target": "r-1"},
                }
            ),
        )
        run = run_document(
            job=document_job(),
            registry=registry,
            store=MemoryCheckpointStore(),
            workspace=_workspace(tmp_path),
            inputs_hash="h1",
            now=clock,
        )

        assert len(run.decisions()) == 2
        assert len({item.decision_id for item in run.decisions()}) == 2

    def test_decision_events_reach_the_job_event_log(self, tmp_path: Path) -> None:
        """批处理入口也能把决策落成事件（SSE / 重连 / 重启回放都靠它）。"""
        log = EventLog()
        registry = build_registry(*all_stages(**{"qa": {"decide_point": MODEL_POINT}}))
        run_document(
            job=document_job(),
            registry=registry,
            store=MemoryCheckpointStore(),
            workspace=_workspace(tmp_path),
            inputs_hash="h1",
            events=log,
            now=clock,
        )

        decisions = [
            event
            for event in log.events("j-doc-1")
            if event.type == DECISION_EVENT_TYPE and event.payload.get("record") == RECORD_DECISION
        ]
        assert len(decisions) == 1
        assert decisions[0].payload["kind"] == MODEL_POINT
