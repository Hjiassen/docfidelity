"""决策留痕（NFR-15 决策可解释 / NFR-18 成本可控）。

留痕的**形状**早已冻结在契约里（``contracts.job.Decision`` / ``ModelCall``），本模块只做
两件事：把一次 :class:`~docfidelity.agents.routing.DecisionOutcome` 落成契约对象，
以及提供一个纯内存的记录器供内核单测与"无数据库环境"使用。

两处刻意的设计
--------------
1. **标识是内容摘要，不是计数器也不是随机数**。内核禁 `random` 参与逻辑（DD-2.4），
   而计数器在进程重启后会重复。故 ``decision_id`` / ``call_id`` 由内容确定性推导——
   同一次决策在同一次作业内得到同一个 id，重跑与回放因此可比对。
2. **低置信与兜底项必须可单独取出**（``pending_decisions``）。NFR-26 的要求不是
   "记录下来"，而是"进裁决队列、不静默采用"；一个只能全量列出记录的接口，
   在界面上仍然要靠人一页页找。
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from datetime import datetime, timezone
from typing import Any

from docfidelity.contracts.decisions import DecisionRecorder
from docfidelity.contracts.job import Decision, DecisionOption, ModelCall
from docfidelity.platform.hashing import digest_payload

__all__ = [
    "DecisionRecorder",
    "MemoryRecorder",
    "decision_id",
    "model_call_id",
    "now_utc",
]


def now_utc() -> datetime:
    """当前 UTC 时间。**只进元数据**，不参与任何判定（DD-2.4）。"""
    return datetime.now(tz=timezone.utc)


def decision_id(payload: dict[str, Any]) -> str:
    """决策标识：由作业 / 决策点 / 对象 / 取值 / 序号确定性推导。"""
    return "d_" + digest_payload(payload)[:16]


def model_call_id(payload: dict[str, Any]) -> str:
    """模型调用标识：由作业 / 决策点 / 尝试序号确定性推导。"""
    return "mc_" + digest_payload(payload)[:16]


class MemoryRecorder:
    """进程内留痕（内核单测与无数据库环境的默认实现）。"""

    def __init__(self, *, job_id: str | None = None) -> None:
        self.job_id = job_id
        self.decisions: list[Decision] = []
        self.model_calls: list[ModelCall] = []

    # -------------------------------------------------------------- 写入
    def record_decision(self, decision: Decision) -> None:
        self.decisions.append(decision)

    def record_model_call(self, call: ModelCall) -> None:
        self.model_calls.append(call)

    def extend(self, other: MemoryRecorder) -> None:
        """并入另一份记录（子作业 → 编排作业的归集用）。"""
        self.decisions.extend(other.decisions)
        self.model_calls.extend(other.model_calls)

    # -------------------------------------------------------------- 查询
    def pending_decisions(self) -> tuple[Decision, ...]:
        """进裁决队列的项（低置信 / 兜底 / 需人工）。"""
        return tuple(item for item in self.decisions if item.status == "pending")

    def silent_adoptions(self) -> tuple[Decision, ...]:
        """低置信却被自动采用的项——**应当恒为空**。

        它被写成查询而不是断言：留痕层不改变结论，只把"有没有静默采用"变成
        可被测试与报告看见的事实（验收 39 的要害正是"无一项被静默采用"）。
        """
        return tuple(
            item
            for item in self.decisions
            if item.status == "auto"
            and item.confidence is not None
            and item.confidence < _LOW_CONFIDENCE_FLOOR
        )

    def counts_by_point(self) -> dict[str, int]:
        return dict(Counter(item.kind for item in self.decisions))

    def fallback_count(self) -> int:
        return sum(1 for item in self.decisions if item.source == "fallback")

    def model_route_count(self) -> int:
        return sum(1 for item in self.decisions if item.source == "model")

    def token_totals(self) -> tuple[int, int]:
        """``(输入, 输出)`` token 合计（NFR-18：确定性模式下必须为 0）。"""
        return (
            sum(call.tokens_in or 0 for call in self.model_calls),
            sum(call.tokens_out or 0 for call in self.model_calls),
        )

    def describe(self) -> dict[str, Any]:
        """可序列化的汇总（报告与界面共用同一份口径）。"""
        tokens_in, tokens_out = self.token_totals()
        return {
            "job_id": self.job_id,
            "decisions": len(self.decisions),
            "by_decision_point": self.counts_by_point(),
            "pending": len(self.pending_decisions()),
            "model_route": self.model_route_count(),
            "fallback": self.fallback_count(),
            "model_calls": len(self.model_calls),
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
        }


#: "低置信"的下限。低于它的自动采用一律视为静默采用。
#: 取 0.6 与 DD-12.3 的 `threshold: float = 0.6` 默认值一致；逐决策点的真实阈值
#: 由路由配置声明（`DecisionPointRoute.confidence_threshold`），这里是**保守下界**。
_LOW_CONFIDENCE_FLOOR: float = 0.6


def options_of(
    candidates: Iterable[str], confidences: dict[str, float] | None = None
) -> list[DecisionOption]:
    """把候选值折成契约里的选项列表（低置信选项带 reason，供裁决队列判断）。"""
    scores = confidences or {}
    return [
        DecisionOption(
            value=value,
            label=value,
            confidence=scores.get(value),
            reason="候选集内取值" if value not in scores else f"模型置信度 {scores[value]:.2f}",
        )
        for value in candidates
    ]
