"""裁决面（P10 提交 / P12 待办）与"待裁决"如何收场。

判据是「一次裁决之后，服务端给出的四件事都自洽」：

1. **留痕落在报告上**（``Report.adjudications``）。裁决之后**重新读回**那份报告，
   那条裁决还在——不留痕的"已办结"与"没人看过"在界面上长得一模一样。
2. **门禁结论跟着判定状态走**。只有 ``waive`` 会翻结论，而它一翻，``verdict`` 与
   ``summary`` 必须**同时**翻，否则报告自相矛盾："结论：阻断" + 判定全是 waived。
3. **作业状态由服务端决定**。``advance`` 逐字说清这一次之后作业是回到运行中、
   还卡在这一份、还是根本不推进，界面照抄（NFR-22），不自己推断。
4. **待办列表只列"还没人办"的项**。办过一项就立刻消失，且两类待办（门禁判定 /
   待确认决策）落在**同一个**列表里——它们共用一条提交路径（DD-13.2）。

两类待办为什么都要有
--------------------
只列 blocker 判定是不够的：**让作业进入 ``awaiting_decision`` 的其实是决策项**
（低置信 / 兜底 / 路由给人工的那些，NFR-26 / 验收 39）。而只列决策项也不行——
门禁判定同样要人签字。故这里每个场景都同时造出两类，断言它们在同一次裁决流程里
各自被正确办结。

``advance`` 的四种收场各有一条用例，因为它们回答的是四个不同的问题：
``remaining``（还有别的）、``advanced``（可以继续了）、``blocked``（办完了也不能过）、
``configure``（改配置重来）。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from docfidelity.contracts.errors import EngineError, ErrorCode
from docfidelity.contracts.job import Job
from docfidelity.contracts.qa import Report
from tests.fixtures.server_fixture import (
    Harness,
    build_harness,
    build_registry,
    finding,
)

pytestmark = pytest.mark.api

#: 造一条 blocker（``verdict`` 因此是 ``blocked``，而它才是"待裁决"的那一类判定）。
_BLOCKER_RULE = "FR-M5-01"
#: 会问模型的决策点：回退标待确认 ⇒ 子作业落 ``awaiting_decision``。
_DECISION_POINT = "placement.section_slot"
#: 只构建一份文档。多一份就多一整套待办，断言里的数字全要乘二，读起来全是噪音。
_ONLY = ["overview"]


@dataclass(frozen=True)
class Awaiting:
    """一个"卡在待裁决"的现场：子作业 + 报告 + 两类待办项各一条。"""

    harness: Harness
    orchestration_id: str
    child_id: str
    report: Report
    finding_id: str
    decision_id: str

    @property
    def job(self) -> Job:
        return self.harness.repository.get_job(self.child_id)


def awaiting(root: Path) -> Awaiting:
    """装配一个卡在待裁决的作业（前置条件不成立就直接失败，不给后续用例留模糊地带）。"""
    harness = build_harness(
        root,
        stages=build_registry(
            qa={
                "decide_point": _DECISION_POINT,
                "decide_target": "overview#1",
                "emit_report": True,
                "findings": (finding(rule_id=_BLOCKER_RULE, severity="blocker"),),
            }
        ),
    )
    project_id = harness.new_project()
    orchestration_id = harness.build(project_id, doc_keys=_ONLY)
    child_id = harness.services.job_children(orchestration_id)[0].job_id
    assert harness.repository.get_job(child_id).status == "awaiting_decision"

    report = harness.repository.find_report(child_id)
    assert report is not None and report.verdict == "blocked"
    finding_id = report.findings[0].finding_id
    assert finding_id is not None, "qa 产出报告时必须给判定赋可寻址标识"

    decisions = harness.services.pending_decisions(child_id)
    assert len(decisions) == 1 and decisions[0].status == "pending"

    return Awaiting(
        harness=harness,
        orchestration_id=orchestration_id,
        child_id=child_id,
        report=report,
        finding_id=finding_id,
        decision_id=decisions[0].decision_id,
    )


def expect_error(code: str, fn: object) -> EngineError:
    with pytest.raises(EngineError) as caught:
        fn()  # type: ignore[operator]
    assert caught.value.code == code, caught.value.message
    return caught.value


# ------------------------------------------------------------------ 待办
def test_inbox_lists_both_kinds_of_pending_subjects(tmp_path: Path) -> None:
    """两类待办在同一个列表里，且**都带可寻址标识**（否则界面上点不了）。"""
    scene = awaiting(tmp_path)
    items = scene.harness.services.inbox()

    assert {item.kind for item in items} == {"finding", "decision"}
    assert {item.subject_id for item in items} == {scene.finding_id, scene.decision_id}
    assert all(item.job_id == scene.child_id for item in items)
    assert all(item.resolvable for item in items)
    assert all(item.unresolvable_reason is None for item in items)

    blocker = next(item for item in items if item.kind == "finding")
    assert blocker.severity == "blocker"
    assert _BLOCKER_RULE in blocker.title


def test_inbox_only_carries_blockers_not_advice(tmp_path: Path) -> None:
    """major / minor 是**信息**而非阻塞：把它们塞进待办会把徽标变成噪音计数器
    （一份报告里的判定本就是几十条）。"""
    harness = build_harness(
        tmp_path,
        stages=build_registry(qa={"emit_report": True, "findings": (finding(severity="major"),)}),
    )
    project_id = harness.new_project()
    harness.build(project_id, doc_keys=_ONLY)

    assert harness.services.inbox() == [], "只有 major 的报告不该有待裁决项"


def test_inbox_hides_a_subject_once_it_has_been_decided(tmp_path: Path) -> None:
    """办过一项就立刻消失：清单回答的是"还剩谁要看"而不是"历史上出现过多少问题"。"""
    scene = awaiting(tmp_path)
    before = {item.subject_id for item in scene.harness.services.inbox()}

    scene.harness.services.adjudicate(scene.decision_id, action="accept", operator="森")

    after = {item.subject_id for item in scene.harness.services.inbox()}
    assert after == before - {scene.decision_id}
    assert scene.finding_id in after


def test_inbox_is_scoped_to_one_project(tmp_path: Path) -> None:
    scene = awaiting(tmp_path)
    other = scene.harness.services.create_project(name="别人的项目")

    assert len(scene.harness.services.inbox()) == 2
    assert scene.harness.services.inbox(project_id=other.project_id) == []


def test_inbox_order_is_stable(tmp_path: Path) -> None:
    """次序按 ``(created_at, subject_id)`` 固定：每次刷新换个顺序，
    使用者会以为"刚才那条不见了"。"""
    scene = awaiting(tmp_path)
    first = scene.harness.services.inbox()
    keys = [(item.created_at, item.subject_id) for item in first]

    assert keys == sorted(keys)
    assert keys == [(item.created_at, item.subject_id) for item in scene.harness.services.inbox()]


# ------------------------------------------------------- 留痕与结论
def test_accept_records_a_ledger_entry_and_reads_back_from_the_repository(
    tmp_path: Path,
) -> None:
    """留痕要**落进仓储**并读得回来——只改内存里的那份报告，重启后裁决就没了。"""
    scene = awaiting(tmp_path)
    result = scene.harness.services.adjudicate(
        scene.finding_id, action="accept", operator="森", reason="已复核，可以接受"
    )

    assert result.adjudication.action == "accept"
    assert result.adjudication.operator == "森"
    assert result.adjudication.subject_id == scene.finding_id
    assert result.waiver is None, "accept 不产生 Waiver（只有 waive 才改门禁结论）"

    stored = scene.harness.repository.find_report(scene.child_id)
    assert stored is not None
    assert [item.subject_id for item in stored.adjudications] == [scene.finding_id]
    assert stored.adjudication_of(scene.finding_id) is not None
    assert stored.adjudication_of(scene.decision_id) is None


def test_accept_leaves_the_verdict_and_the_finding_alone(tmp_path: Path) -> None:
    """``accept`` 是"我认可这条判定"，**不是**"让它不阻断"。"""
    scene = awaiting(tmp_path)
    result = scene.harness.services.adjudicate(scene.finding_id, action="accept", operator="森")

    assert result.finding is not None and result.finding.status == "open"
    assert result.report.verdict == "blocked"
    assert result.report.open_findings(), "判定仍在未豁免集合里"


def test_accept_does_not_touch_the_other_pending_subject(tmp_path: Path) -> None:
    """裁决只办一条：顺手把别的也办掉，等于替使用者签了字。"""
    scene = awaiting(tmp_path)
    result = scene.harness.services.adjudicate(scene.decision_id, action="accept", operator="森")
    assert result.remaining == (scene.finding_id,)
    assert scene.harness.services.inbox()[0].subject_id == scene.finding_id


def test_waiving_a_finding_flips_the_verdict_and_the_summary_together(
    tmp_path: Path,
) -> None:
    """``waive`` 是唯一改变门禁结论的动作，故报告的两处结论**必须同时**翻。

    只改判定状态而不重算，报告就会自相矛盾："结论：阻断" + 判定全是 waived。
    """
    scene = awaiting(tmp_path)
    result = scene.harness.services.adjudicate(
        scene.finding_id,
        action="waive",
        operator="森",
        reason="该阻断项已由版式组确认可接受，责任在版式组",
    )

    assert result.waiver is not None
    assert result.waiver.reason.startswith("该阻断项")
    assert result.waiver.operator == "森"
    assert result.waiver.rule_id == _BLOCKER_RULE
    assert result.waiver.target == scene.finding_id, "实例级豁免要指明对象"
    assert result.waiver.scope == "instance"

    assert result.finding is not None
    assert result.finding.status == "waived"
    assert result.finding.waiver_id == result.waiver.waiver_id
    assert result.report.verdict == "pass"
    assert result.report.summary is not None and "已豁免 1" in result.report.summary
    assert result.report.waivers[-1].waiver_id == result.waiver.waiver_id


def test_waiving_a_decision_subject_leaves_no_rule_waiver(tmp_path: Path) -> None:
    """决策项**不是**判定：给它硬编一个 ``rule_id`` 会造出一份永远命中不了任何判定的
    豁免，而 ``Report.waivers`` 的计数正是定稿判据。故对决策项的 waive 只留痕。"""
    scene = awaiting(tmp_path)
    result = scene.harness.services.adjudicate(
        scene.decision_id, action="waive", operator="森", reason="不采纳该决策，按人工判断走"
    )

    assert result.waiver is None
    assert result.report.waivers == []
    assert [item.action for item in result.report.adjudications] == ["waive"]
    assert result.report.verdict == "blocked", "决策项的 waive 不改变门禁结论"


# ------------------------------------------------------- 作业怎么收场
def test_the_job_goes_back_to_running_once_every_subject_is_settled(
    tmp_path: Path,
) -> None:
    """全部办结且报告不再阻断 ⇒ 走状态机 ``decided`` 回 ``running``，worker 收尾。"""
    scene = awaiting(tmp_path)
    scene.harness.services.adjudicate(scene.decision_id, action="accept", operator="森")
    result = scene.harness.services.adjudicate(
        scene.finding_id, action="waive", operator="森", reason="已确认可接受"
    )

    assert result.advance == "advanced"
    assert result.remaining == ()
    assert result.job.status == "running"
    assert "worker" in result.note
    assert scene.harness.repository.get_job(scene.child_id).status == "running"
    # 编排作业跟着聚合：漏掉这一步时界面上的进度会永远差一份
    assert scene.harness.repository.get_job(scene.orchestration_id).status == "running"


def test_a_decided_job_really_finishes_when_the_worker_picks_it_up(
    tmp_path: Path,
) -> None:
    """``decided`` 不只是改了状态字：worker 认领之后**一个阶段都不重跑**（断点复用）
    而作业真的走到终态。"""
    scene = awaiting(tmp_path)
    scene.harness.services.adjudicate(scene.decision_id, action="accept", operator="森")
    scene.harness.services.adjudicate(
        scene.finding_id, action="waive", operator="森", reason="已确认可接受"
    )

    calls_before = scene.harness.stages["qa"].call_count
    scene.harness.run_all()

    assert scene.harness.repository.get_job(scene.child_id).status == "succeeded"
    assert scene.harness.stages["qa"].call_count == calls_before, "断点应被复用"


def test_a_settled_job_stays_put_while_a_blocker_is_unwaived(tmp_path: Path) -> None:
    """``accept`` 办完了但报告仍 ``blocked`` ⇒ **不推进**。

    ``blocked`` 的唯一出口是 ``can_finalize``（FR-M5-16），裁决面不得绕过它把作业
    推成"成功"。已办结与可通过是两件事：前者答"还有谁要看"，后者答"能不能定稿"。
    """
    scene = awaiting(tmp_path)
    scene.harness.services.adjudicate(scene.decision_id, action="accept", operator="森")
    result = scene.harness.services.adjudicate(scene.finding_id, action="accept", operator="森")

    assert result.advance == "blocked"
    assert result.remaining == ()
    assert result.report.verdict == "blocked"
    assert result.job.status == "awaiting_decision"
    assert "waive" in result.note and "豁免" in result.note, "理由与补救项一起镜射"


def test_configure_does_not_push_the_job_back_to_running(tmp_path: Path) -> None:
    """``configure`` 是"我去改配置，改完重来"，**不**把作业置回 ``running``。

    置回去的话 worker 会用**旧**的 ``inputs_hash`` 复用全部断点，一个阶段都不重跑
    却报 ``succeeded``——产物看起来是"按新配置做好了"，其实还是旧的。这是最坏的
    一种成功，故宁可不推进（见 :mod:`server.adjudication` 末节）。
    """
    scene = awaiting(tmp_path)
    result = scene.harness.services.adjudicate(scene.finding_id, action="configure", operator="森")

    assert result.advance == "configure"
    assert result.job.status == "awaiting_decision"
    assert "重新发起构建" in result.note
    assert scene.harness.repository.get_job(scene.child_id).status == "awaiting_decision"


def test_a_job_that_is_not_waiting_is_left_alone(tmp_path: Path) -> None:
    """作业已经不在待裁决（如已被别处推进）时只留痕，状态不动——
    硬套一次状态迁移会抛 ``E_JOB_CONFLICT``，把一次补记变成一次报错。"""
    scene = awaiting(tmp_path)
    scene.harness.services.adjudicate(scene.decision_id, action="accept", operator="森")
    scene.harness.services.adjudicate(
        scene.finding_id, action="waive", operator="森", reason="已确认可接受"
    )
    assert scene.job.status == "running"

    result = scene.harness.services.adjudicate(
        scene.finding_id, action="accept", operator="复核者", reason="补记一次复核"
    )
    assert result.advance == "not_awaiting"
    assert result.job.status == "running"
    # 后一次留痕照常记下：补记是合法动作，只是不改状态
    assert [item.operator for item in result.report.adjudications] == ["森", "森", "复核者"]


# ----------------------------------------------------------- 输入校验
def test_waiving_without_a_reason_is_refused(tmp_path: Path) -> None:
    """豁免必须留下"谁以什么理由让一条阻断不阻断"（FR-M7-06 / NFR-15），
    而它是**业务**错误不是字段必填错误——故服务端给的是专门的码。"""
    scene = awaiting(tmp_path)

    for blank in (None, "", "   \n"):
        error = expect_error(
            ErrorCode.WAIVER_REASON_REQUIRED,
            lambda blank=blank: scene.harness.services.adjudicate(
                scene.finding_id, action="waive", operator="森", reason=blank
            ),
        )
        assert error.where["subject_id"] == scene.finding_id
        assert any("accept" in item for item in error.remediation), "补救项要指出可以改用 accept"

    assert (
        scene.harness.repository.find_report(scene.child_id).adjudications  # type: ignore[union-attr]
        == []
    ), "被拒的提交不该留下半条留痕"


def test_an_unknown_action_is_refused(tmp_path: Path) -> None:
    """规范动作只有三种（``AdjudicationAction``）：每多一种就多一条绕过门禁的路径。"""
    scene = awaiting(tmp_path)
    error = expect_error(
        ErrorCode.CONFIG_INVALID,
        lambda: scene.harness.services.adjudicate(
            scene.finding_id, action="approve", operator="森"
        ),
    )
    assert error.where["allowed"] == ["accept", "configure", "waive"]


def test_an_unknown_subject_is_not_found(tmp_path: Path) -> None:
    scene = awaiting(tmp_path)
    error = expect_error(
        ErrorCode.NOT_FOUND,
        lambda: scene.harness.services.adjudicate("r_不存在@1", action="accept", operator="森"),
    )
    assert error.where["subject_id"] == "r_不存在@1"


def test_a_decision_without_a_report_is_visible_but_not_actionable(
    tmp_path: Path,
) -> None:
    """报告还没产出时，决策项照常**列出来**（它是真的在等人裁决），
    但 ``resolvable=False`` 并给出原因——界面照抄，不自己猜"为什么点不了"（硬约束 15）。
    """
    harness = build_harness(tmp_path, stages=build_registry(qa={"decide_point": _DECISION_POINT}))
    project_id = harness.new_project()
    orchestration_id = harness.build(project_id, doc_keys=_ONLY)
    child_id = harness.services.job_children(orchestration_id)[0].job_id
    assert harness.repository.find_report(child_id) is None, "前提：没有报告"

    items = harness.services.inbox()
    assert [item.kind for item in items] == ["decision"]
    assert items[0].resolvable is False
    assert items[0].unresolvable_reason, "不能点就要给出原因"

    error = expect_error(
        ErrorCode.REPORT_MISSING,
        lambda: harness.services.adjudicate(items[0].subject_id, action="accept", operator="森"),
    )
    assert error.where["job_id"] == child_id
