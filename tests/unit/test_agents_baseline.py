"""小模型基线评测（TASK-P0-17 / NFR-23）——"决策点分级"的证据链。

本模块盯的是这条链上最容易"看着对、其实没接上"的三处：

1. **样本覆盖**：模型档的每个决策点都必须有样本。漏一个不会被发现——它只是不出现在
   ``by_point`` 里，而 `routing` 对"没有数据"的处理是**按目录声明路由**，
   于是"没测过"与"测过且达标"在界面上长得一模一样（`uncovered_points` 就是为了
   把两者分开）。
2. **判分口径**：通过 = 被采纳**且**命中期望（``verdict`` 的唯一实现）。
   只要"别降级"就算通过的话，模型编一个不存在的章节名也会被判对。
3. **评测不自我实现**：``run_baseline`` 造路由表时**不传基线**。带上的话上一轮判"不达标"
   的点本轮会被强制走规则，于是恒通过——一个能力为零的模型也能拿满分。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from docfidelity.agents.baseline import (
    BASELINE_FILENAME,
    CASE_SOURCE_VERSION,
    BaselineCase,
    BaselineReport,
    cases_from_payload,
    cases_to_payload,
    default_cases,
    load_status,
    run_baseline,
    save_report,
    uncovered_points,
    verdict,
)
from docfidelity.agents.catalog import DECISION_POINTS, MODEL_ROUTED
from docfidelity.agents.routing import AdapterOutcome, DecisionOutcome, build_routing
from docfidelity.agents.sdk_adapter import NullAdapter, ScriptedAdapter
from docfidelity.contracts.config import DecisionPointRoute, RoutingConfig
from docfidelity.contracts.errors import EngineError, ErrorCode

pytestmark = pytest.mark.unit

#: 模型档决策点（`routing` 会去问模型的那几个）。
MODEL_POINT_IDS = tuple(point.id for point in DECISION_POINTS if point.kind in MODEL_ROUTED)


class _PerfectAdapter:
    """按样本 ``target`` 回带期望载荷的适配层：模拟"模型全答对"。

    刻意不写成 ``ScriptedAdapter``：那个回放器按**决策点**给答案，而基线对同一个点
    会问**两条不同的样本**（这正是区分"真在判"与"总选第一个"的手段）。按点回放的
    适配层在这件事上表达力不够，用它只会测出 0.5 —— 那不是判分逻辑的问题，
    而是回放器与样本集粒度的错配。
    """

    def __init__(self, cases: tuple[BaselineCase, ...]) -> None:
        self._by_target = {case.target: dict(case.expect) for case in cases}

    def available(self) -> bool:
        return True

    def describe(self) -> dict[str, Any]:
        return {"kind": "perfect", "available": True, "model": "perfect"}

    def call(self, request: Any, output_type: Any) -> AdapterOutcome:
        return AdapterOutcome(
            ok=True,
            schema_ok=True,
            confidence=1.0,
            model="perfect",
            chosen="ok",
            payload=self._by_target.get(request.target, {}),
        )


def _case(**kwargs: Any) -> BaselineCase:
    base: dict[str, Any] = {
        "case_id": "c1",
        "decision_point": "dedup.semantic_duplicate",
        "target": "t",
        "candidates": ["重复", "不重复"],
        "inputs": {"left": "a", "right": "b"},
        "expect": {"is_duplicate": True},
    }
    base.update(kwargs)
    return BaselineCase(**base)


def _outcome(**kwargs: Any) -> DecisionOutcome:
    base: dict[str, Any] = {
        "decision_point": "dedup.semantic_duplicate",
        "declared": "small_model",
        "effective": "small_model",
        "source": "model",
        "output": {"is_duplicate": True},
    }
    base.update(kwargs)
    return DecisionOutcome(**base)


# ------------------------------------------------------------------ 内置样本集
def test_default_cases_come_from_package() -> None:
    """内置样本集随包分发：缺了它评测根本无法开始（不是可选件）。"""
    cases = default_cases()
    assert cases, "内置样本集为空"
    assert all(case.case_id for case in cases)
    assert len({case.case_id for case in cases}) == len(cases), "case_id 必须唯一"


def test_default_cases_cover_every_model_point() -> None:
    """**分级有依据**：模型档的每个决策点都必须被样本覆盖。

    这是"未测 ≠ 已验证"的数据面：没被覆盖的点不会出现在 ``by_point`` 里，
    而路由对"没有数据"是按目录声明放行的——于是漏一个点不会被任何人发现，
    直到生产上发现那个点的模型结论一直是错的。
    """
    cases = default_cases()
    assert uncovered_points(cases) == ()
    assert {case.decision_point for case in cases} <= set(MODEL_POINT_IDS)


def test_default_cases_every_expect_is_checkable() -> None:
    """内置样本的 ``expect`` 一律非空：**能校验的一定要校验**。

    只要求"别降级"的样本会把"模型编了一个不存在的章节名"判成通过（需求 5.2.2 的
    候选注入正是要防这个）。此断言只针对**内置**样本集：外部样本集允许自由文本类
    只验引用，那是 ``BaselineCase.expect`` 文档写明的合法用法。
    """
    assert all(case.expect for case in default_cases())


def test_default_cases_use_neutral_words() -> None:
    """样本集不得携带文档类型名 / 领域概念名（NFR-28 / 验收 42）。

    查的是与 ``qa.type_agnostic`` 同一份词表，但**CI 脚本扫不到这里**——它只遍历
    ``*.py``，而样本是 ``.json``（包内数据文件，见 ``_CASES_PATH``）。样本会被反复
    增删，正是最容易顺手写进"接口 / 参数 / 字段"这类词的地方，故这条不变量由本测试守。

    只用 ``doc_type_name | domain_concept`` 两组（``ir_words()``），**不含**
    ``branch_literal``：对数据是子串匹配，而 ``table`` / ``field`` / ``endpoint``
    那些词必然出现在合法载荷的字段名里（理由见 ``qa.type_agnostic.ir_words``）。
    """
    from docfidelity.qa.type_agnostic import ir_words

    words = ir_words()
    assert words, "词表为空：这条断言就失去意义了"
    for case in default_cases():
        blob = cases_to_payload([case])
        hits = sorted(word for word in words if word in str(blob))
        assert not hits, f"样本 {case.case_id} 命中禁词：{hits}"


# ------------------------------------------------------------------ 样本自检
def test_validate_rejects_rule_tier_point() -> None:
    """规则档的点写样本必须当场可见：给它写样本必然恒不过。

    恒不过的通过率会反过来让路由表"强制规则接管"一个本来就是规则的档位——
    数字看着正常，过程全错。
    """
    cases = (_case(case_id="bad", decision_point="layout.decide"),)
    with pytest.raises(EngineError) as info:
        run_baseline(cases, adapter=NullAdapter())
    assert info.value.code == ErrorCode.CONFIG_INVALID
    assert "规则档" in str(info.value)


def test_validate_rejects_unregistered_point() -> None:
    cases = (_case(case_id="bad", decision_point="nope.unknown"),)
    with pytest.raises(EngineError) as info:
        run_baseline(cases, adapter=NullAdapter())
    assert info.value.code == ErrorCode.CONFIG_INVALID
    assert "未登记" in str(info.value)


def test_validate_rejects_duplicate_case_id() -> None:
    cases = (_case(case_id="dup"), _case(case_id="dup", target="t2"))
    with pytest.raises(EngineError) as info:
        run_baseline(cases, adapter=NullAdapter())
    assert info.value.code == ErrorCode.CONFIG_INVALID
    assert "重复" in str(info.value)


# ------------------------------------------------------------------ 判分口径
def test_verdict_requires_adoption_and_match() -> None:
    """通过 = **被采纳** 且 **命中期望**。两条不可合并。"""
    case = _case()
    passed, adopted, _ = verdict(case, _outcome())
    assert (passed, adopted) == (True, True)

    # 对立断言：答对了但没被采纳（降级到规则）→ 不通过。
    # 这正是"这个点交给模型能不能驱动"的答案，规则兜底接住的样本说明**不能**。
    passed, adopted, reason = verdict(case, _outcome(source="fallback", effective="rule"))
    assert (passed, adopted) == (False, False)
    assert "采纳" in reason or "降级" in reason

    # 对立断言：被采纳了但答错 → 不通过（``adopted`` 仍为真，便于诊断分开两种错）。
    passed, adopted, reason = verdict(case, _outcome(output={"is_duplicate": False}))
    assert (passed, adopted) == (False, True)
    assert "is_duplicate" in reason

    # 载荷缺字段同样不通过（"没写"不等于"写对了"）。
    passed, adopted, reason = verdict(case, _outcome(output={}))
    assert (passed, adopted) == (False, True)
    assert "缺少" in reason


def test_verdict_empty_payload_is_not_pass() -> None:
    passed, adopted, reason = verdict(_case(), _outcome(output=None))
    assert passed is False
    assert adopted is True
    assert "无法判分" in reason


# ------------------------------------------------------------------ 评测
def test_run_baseline_all_fail_when_model_unavailable() -> None:
    """模型不可用 ⇒ 全不过，且**逐点**为 0（不是"没算出"）。"""
    cases = default_cases()
    report = run_baseline(cases, adapter=NullAdapter(), model="null")
    assert report.total == len(cases)
    assert report.passed == 0
    assert report.pass_rate == 0.0
    assert set(report.by_point) == set(MODEL_POINT_IDS)
    assert all(rate == 0.0 for rate in report.by_point.values())
    assert report.failed and report.failures, "必须给出可读的失败原因"


def test_run_baseline_all_pass_with_perfect_adapter() -> None:
    """对立断言：答对即全过——判分不是"永远不通过"。"""
    cases = default_cases()
    report = run_baseline(cases, adapter=_PerfectAdapter(cases), model="perfect")
    assert report.passed == report.total
    assert report.pass_rate == 1.0
    assert all(rate == 1.0 for rate in report.by_point.values())
    assert report.failed == ()


def test_run_baseline_does_not_self_fulfil() -> None:
    """评测**不喂自己的结论**：上一轮 0 分不会让这一轮白拿分。

    做法上比对的是"同一批点，若真把 0 分基线喂给路由会怎样"：它们会被强制走规则。
    评测若也这么做，就无法回答"这个点交给模型行不行"——它只会复述上一轮的结论。
    """
    cases = default_cases()
    report = run_baseline(cases, adapter=_PerfectAdapter(cases), model="perfect")
    assert report.passed == report.total

    zero = dict.fromkeys(MODEL_POINT_IDS, 0.0)
    constrained = build_routing(model_enabled=True, baseline=zero)
    for point_id in MODEL_POINT_IDS:
        route = constrained.route(point_id)
        assert route.effective == "rule"
        assert "基线通过率" in route.reason


def test_report_carries_case_digest_and_uncovered() -> None:
    """报告必须带样本集指纹：换了一批样本，旧通过率不再可比。"""
    cases = default_cases()
    report = run_baseline(cases, adapter=_PerfectAdapter(cases), model="perfect")
    assert report.cases_digest
    assert report.uncovered == []

    # 只覆盖一个点的样本集 ⇒ 其余模型档点必须被标为"未测"。
    partial = tuple(case for case in cases if case.decision_point == MODEL_POINT_IDS[0])
    small = run_baseline(partial, adapter=_PerfectAdapter(cases), model="perfect")
    assert set(small.uncovered) == set(MODEL_POINT_IDS) - {MODEL_POINT_IDS[0]}
    assert small.cases_digest != report.cases_digest


def test_failures_are_verbatim_from_verdict() -> None:
    """失败原因是**逐字转发**的，不是在这里重写的（否则就是第二份口径）。"""
    cases = default_cases()
    report = run_baseline(cases, adapter=NullAdapter(), model="null")
    assert report.failures[0] == report.failed[0].reason


# ------------------------------------------------------------------ 编解码 / 落盘
def test_cases_payload_round_trip() -> None:
    cases = default_cases()
    payload = cases_to_payload(cases)
    assert payload["case_source_version"] == CASE_SOURCE_VERSION
    assert cases_from_payload(payload) == cases


@pytest.mark.parametrize("bad", [[], {"nope": 1}, {"cases": {}}])
def test_cases_from_payload_rejects_bad_shape(bad: Any) -> None:
    """形状不对即抛：静默跳过坏条目会让"样本少了一条"看起来像"跑过了"。"""
    with pytest.raises(EngineError) as info:
        cases_from_payload(bad)
    assert info.value.code == ErrorCode.CONFIG_INVALID


def test_report_round_trip_through_disk(tmp_path: Path) -> None:
    """落盘 → 读回：逐点通过率、未覆盖点、失败原因都不得丢失。"""
    cases = default_cases()
    report = run_baseline(cases, adapter=NullAdapter(), model="null")
    target = save_report(report, tmp_path / BASELINE_FILENAME)

    status = load_status(target)
    assert status.loaded is True
    assert status.problem == ""
    assert status.model == report.model
    assert status.cases_digest == report.cases_digest
    assert status.by_point == report.by_point
    assert status.uncovered == report.uncovered
    assert status.failures == report.failures


def test_load_status_three_states(tmp_path: Path) -> None:
    """**永不抛**，且三态各有各的话说——它们指向不同的处置。"""
    missing = tmp_path / "absent.json"
    status = load_status(missing)
    assert status.loaded is False
    assert "未找到" in status.problem

    broken = tmp_path / "broken.json"
    broken.write_text("{ not json", encoding="utf-8")
    status = load_status(broken)
    assert status.loaded is False
    assert "无法解析" in status.problem
    assert status.by_point == {}

    # 合法 JSON 但不是报告（缺必填字段）也算"读不动"，而不是当成空报告。
    wrong = tmp_path / "wrong.json"
    wrong.write_text('{"hello": 1}', encoding="utf-8")
    assert load_status(wrong).loaded is False


def test_load_status_does_not_judge_thresholds(tmp_path: Path) -> None:
    """加载状态**不做"是否达标"的判断**：达标是 routing 的事（阈值在目录里声明）。

    在这里再判一次就成了第二份口径，而两份口径的分歧表现为"设置页说生效了、
    实际被规则接管"。
    """
    cases = default_cases()
    report = run_baseline(cases, adapter=NullAdapter(), model="null")
    status = load_status(save_report(report, tmp_path / BASELINE_FILENAME))
    assert status.loaded is True
    assert status.problem == ""
    assert all(rate == 0.0 for rate in status.by_point.values())
    assert not hasattr(status, "satisfied")


def test_save_report_is_atomic_and_readable(tmp_path: Path) -> None:
    """落盘用原子写：半个文件会被读成"报告坏了"，而那看起来像资产问题。"""
    report = BaselineReport(model="m", cases_digest="d", total=0)
    target = save_report(report, tmp_path / "nested" / "deep" / BASELINE_FILENAME)
    assert target.is_file()
    assert load_status(target).loaded is True


# ------------------------------------------------------------------ 评测姿态
def _gate(threshold: float) -> RoutingConfig:
    """把同一个门槛套到**全部模型档**决策点上（"这个网关不报置信度"的姿态）。"""
    return RoutingConfig(
        decision_points={
            point_id: DecisionPointRoute(confidence_threshold=threshold)
            for point_id in MODEL_POINT_IDS
        }
    )


def test_missing_confidence_cannot_bypass_admission_with_zero_threshold() -> None:
    """同一样本与适配层，即使门槛归零也不能冒充真实置信度。"""
    case = _case(expect={})  # 只要求"被采纳"，把判分聚焦在门槛本身
    scripted = ScriptedAdapter(
        script={
            "dedup.semantic_duplicate": AdapterOutcome(
                ok=True, schema_ok=True, payload={"is_duplicate": True}, confidence=None
            )
        }
    )

    strict = run_baseline((case,), adapter=scripted)
    assert strict.outcomes[0].adopted is False
    assert strict.by_point["dedup.semantic_duplicate"] == 0.0

    lenient = run_baseline((case,), adapter=scripted, config=_gate(0.0))
    assert lenient.outcomes[0].adopted is False
    assert lenient.by_point["dedup.semantic_duplicate"] == 0.0


def test_the_posture_is_part_of_the_report() -> None:
    """姿态变了，报告就换了证据——与样本指纹同一个道理。

    不记指纹的话，两次结果看起来是"模型变强了"，而不是"评测换了个姿态"。
    """
    cases = default_cases()
    adapter = NullAdapter()

    first = run_baseline(cases, adapter=adapter, model="same")
    again = run_baseline(cases, adapter=adapter, model="same")
    other = run_baseline(cases, adapter=adapter, model="same", config=_gate(0.0))

    assert first.routing_digest != ""
    assert first.routing_digest == again.routing_digest  # 同姿态 ⇒ 同指纹
    assert other.routing_digest != first.routing_digest  # 换姿态 ⇒ 换指纹
    assert other.cases_digest == first.cases_digest  # 样本没换 ⇒ 样本指纹相同


def test_the_posture_digest_reaches_the_settings_view(tmp_path: Path) -> None:
    """姿态指纹要经 ``load_status`` 到达设置页——否则界面上只剩一个孤零零的通过率。"""
    report = run_baseline(default_cases(), adapter=NullAdapter(), model="m", config=_gate(0.0))
    status = load_status(save_report(report, tmp_path / BASELINE_FILENAME))
    assert status.routing_digest == report.routing_digest != ""


def test_placement_baseline_covers_topics_orders_and_target_integrity() -> None:
    cases = tuple(
        case for case in default_cases() if case.decision_point == "placement.section_slot"
    )
    assert len(cases) >= 10
    assert len({tuple(case.candidates) for case in cases}) >= 4
    assert len({tuple(case.expect["chapter_path"]) for case in cases}) >= 5
    for case in cases:
        assert case.inputs["unit_ref"] == case.expect["unit_ref"] == case.target
        constraints = case.context["placement_constraints"]
        assert all(label in constraints for label in case.candidates)
        assert case.expect["chapter_path"] in constraints.values()


def test_independent_baseline_reports_actual_none_confidence_and_usage(tmp_path: Path) -> None:
    case = _case(
        decision_point="placement.section_slot",
        target="u1",
        candidates=["甲", "乙"],
        inputs={"unit_ref": "u1", "chapters": ["甲", "乙"]},
        context={"placement_constraints": {"甲": ["甲"], "乙": ["乙"]}},
        expect={"unit_ref": "u1", "chapter_path": ["乙"]},
    )
    adapter = ScriptedAdapter(
        script={
            "placement.section_slot": AdapterOutcome(
                ok=True, payload=dict(case.expect), confidence=None, tokens_in=12, tokens_out=5
            )
        }
    )
    config = RoutingConfig(
        decision_points={
            "placement.section_slot": DecisionPointRoute.load(
                {"admission_policy": "independent_validation"}
            )
        }
    )
    report = run_baseline(
        (case,), adapter=adapter, model="m", config=config, endpoint_digest="digest"
    )
    assert report.by_point["placement.section_slot"] == 1.0
    assert report.accuracy == 1.0
    assert report.fallback_count == 0
    assert report.missing_confidence_count == 1
    assert (report.tokens_in, report.tokens_out) == (12, 5)
    assert report.outcomes[0].confidence is None
    assert report.outcomes[0].admission_evidence["accepted"] is True
    status = load_status(save_report(report, tmp_path / BASELINE_FILENAME))
    assert status.endpoint_digest == "digest"


def test_safety_validation_does_not_make_semantically_wrong_placement_pass() -> None:
    case = _case(
        decision_point="placement.section_slot",
        target="u1",
        candidates=["甲", "乙"],
        context={"placement_constraints": {"甲": ["甲"], "乙": ["乙"]}},
        expect={"unit_ref": "u1", "chapter_path": ["乙"]},
    )
    adapter = ScriptedAdapter(
        script={
            "placement.section_slot": AdapterOutcome(
                ok=True, payload={"unit_ref": "u1", "chapter_path": ["甲"]}, confidence=None
            )
        }
    )
    config = RoutingConfig(
        decision_points={
            "placement.section_slot": DecisionPointRoute.load(
                {"admission_policy": "independent_validation"}
            )
        }
    )
    report = run_baseline((case,), adapter=adapter, config=config)
    assert report.outcomes[0].adopted is True
    assert report.outcomes[0].passed is False
    assert report.by_point["placement.section_slot"] == 0.0


def test_policy_changes_the_baseline_posture_fingerprint() -> None:
    plain = run_baseline(default_cases(), adapter=NullAdapter())
    config = RoutingConfig(
        decision_points={
            "placement.section_slot": DecisionPointRoute.load(
                {"admission_policy": "independent_validation"}
            )
        }
    )
    validated = run_baseline(default_cases(), adapter=NullAdapter(), config=config)
    assert plain.routing_digest != validated.routing_digest


def test_endpoint_identity_fingerprint_does_not_expose_the_url() -> None:
    from docfidelity.agents.baseline import endpoint_digest

    first = endpoint_digest("http://127.0.0.1:9/v1/")
    assert first == endpoint_digest("http://127.0.0.1:9/v1")
    assert first != endpoint_digest("http://127.0.0.1:10/v1")
    assert "127.0.0.1" not in first
