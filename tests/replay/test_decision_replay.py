"""模型决策路径的离线回放（DD-17.3 / NFR-17 / NFR-18 / TASK-P0-19）。

判据
----
1. **录制件不过期**：录制时的 ``instructions_digest`` 必须等于当前值（提示词或工具
   描述改一个字就红）。这是 NFR-17 的"必须重跑回放并附结果"在本仓库里唯一能被 CI
   执行的形态——在此之前那句话只是一句声明。
2. **决策真的走了模型路径**：回放后 ``effective`` 不是规则档、``fallback_used`` 为假，
   且适配层确实被调过。只断言"结论一致"会放过一种假通过：请求根本没到适配层，
   路由直接从规则兜底里给出了同一个答案。
3. **越权指令被输入护栏拦下**：三类的意图判定与录制一致。

零网络、零模型：``ScriptedAdapter`` 按决策点回放录制好的输出。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from docfidelity.agents.guardrails import check_instruction
from docfidelity.agents.recorder import MemoryRecorder
from docfidelity.agents.replay import ReplayCase, load_cases, script_of, stale_cases
from docfidelity.agents.routing import (
    DecisionContext,
    DecisionRequest,
    build_routing,
)
from docfidelity.agents.sdk_adapter import ScriptedAdapter

pytestmark = pytest.mark.replay

REPLAY_ROOT = Path(__file__).resolve().parent

#: 样例录制的 ``run_id``。真录制（接模型端点）另取 run_id，故"样例"与"真录制"
#: 不会互相覆盖——文件名由 ``run_id + decision_point`` 摘要而来。
SAMPLE_RUN_ID = "demo"


def _cases() -> tuple[ReplayCase, ...]:
    return load_cases(REPLAY_ROOT)


def _decisions() -> tuple[ReplayCase, ...]:
    return tuple(case for case in _cases() if case.kind == "decision")


def _guards() -> tuple[ReplayCase, ...]:
    return tuple(case for case in _cases() if case.kind == "guard")


def test_every_case_is_recorded_for_this_run_id() -> None:
    """样例录制的 run_id 是 ``demo``。

    它同时守着"目录里混进了别的 run_id 的录制"这种情况：那些是别人接真端点录的，
    不该出现在 PR 门禁里（它们的输入可能依赖那个端点的输出）。
    """
    assert [case.run_id for case in _cases()] == [SAMPLE_RUN_ID] * len(_cases())


def test_no_case_has_gone_stale() -> None:
    """录制件的提示词/工具描述摘要必须与当前一致（NFR-17）。

    失败时**不要**急着重新录制：先看提示词改动是不是有意的，再跑一遍模型路径，
    把结果附进当轮记录，然后才更新录制件里的摘要。
    """
    outdated = stale_cases(_cases())
    assert outdated == [], (
        "提示词或工具描述变了，录制件已过期；必须重跑回放并附结果（NFR-17）：\n"
        + "\n".join(outdated)
    )


@pytest.mark.parametrize("case", _decisions(), ids=lambda case: case.decision_point)
def test_a_recorded_decision_replays_through_the_model_route(case: ReplayCase) -> None:
    """回放一条决策：走模型档、结论与录制一致、适配层确实被调到。"""
    adapter = ScriptedAdapter(script=script_of((case,)))
    ctx = DecisionContext(
        table=build_routing(model_enabled=True),
        recorder=MemoryRecorder(job_id=SAMPLE_RUN_ID),
        adapter=adapter,
        job_id=SAMPLE_RUN_ID,
    )
    assert case.request is not None
    outcome = ctx.table.resolve(DecisionRequest.model_validate(dict(case.request)), ctx)

    assert adapter.calls, "适配层没被调到——这次回放根本没走模型路径"
    assert outcome.effective != "rule", f"落回规则档了：{outcome.reason}"
    assert outcome.fallback_used is False, f"用了兜底：{outcome.reason}"
    assert case.outcome is not None
    recorded = case.outcome
    assert outcome.chosen == recorded.get("chosen")
    assert outcome.confidence == recorded.get("confidence")


@pytest.mark.parametrize("case", _guards(), ids=lambda case: case.decision_point)
def test_a_recorded_guard_case_still_hits_the_same_intent(case: ReplayCase) -> None:
    """输入护栏对同一段指令给出同一类意图判定。

    钉住意图而不只钉"被拒绝"：三类越权的处置不同（补事实源 / 重新入库模板 / 走裁决），
    只断言"拒绝了"会让护栏把``edit_template`` 判成 ``bypass_gate`` 也照常通过。
    """
    assert case.instruction is not None
    verdict = check_instruction(case.instruction)
    assert verdict.allowed is False, f"这段指令本该被拦下：{case.instruction!r}"
    assert verdict.intent == case.intent


def test_the_script_covers_every_recorded_decision_point() -> None:
    """每条 decision 录制都能装进脚本，且不含押后的重复决策点。

    ``script_of`` 对重复决策点是**报错**而不是后者覆盖前者（那会让人以为在回放 A
    样本、实际回放的是 B），这条用例把那个约定钉在门禁里。
    """
    script = script_of(_cases())
    assert sorted(script) == sorted(case.decision_point for case in _decisions())
