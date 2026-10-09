"""``pipeline`` 轮的可复用夹具（测试专用）。

为什么阶段体要"真写文件"而不是返回假路径
----------------------------------------
断点里存的是**工作区相对路径**（``StageCheckpoint.outputs``）。如果阶段体返回
``{"render": "out.docx"}`` 这种凭空捏的路径，测试就永远验不到"断点指向的文件真的
存在"——而那正是续跑能成立的前提（下游阶段按逻辑名取件）。故 :class:`ScriptedStage`
一律经 ``context.workspace`` 落一个真文件，再把它的相对路径放进 ``outputs``。

本目录在类型无关检索的白名单里，故夹具里出现中文说明文字是允许的。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from docfidelity.agents import DecisionOutcome
from docfidelity.contracts.errors import EngineErrorInfo, ErrorCode
from docfidelity.contracts.job import Job
from docfidelity.pipeline import (
    STAGE_ORDER,
    StageContext,
    StageOutcome,
    StageRegistry,
)

__all__ = [
    "FIXED_NOW",
    "ScriptedStage",
    "all_stages",
    "build_registry",
    "clock",
    "document_job",
]


#: 固定时钟：让 ``started_at`` / ``finished_at`` 可复现（内核不用 ``random`` 参与逻辑）。
FIXED_NOW = datetime(2026, 9, 21, 12, 0, 0, tzinfo=timezone.utc)


def clock() -> datetime:
    """零参数时钟，直接喂给 ``run_stage(now=...)``。"""
    return FIXED_NOW


def document_job(
    job_id: str = "j-doc-1",
    *,
    project_id: str = "p1",
    status: str = "queued",
    inputs_hash: str = "h-inputs-1",
    doc_key: str = "detail",
) -> Job:
    """一个最小文档子作业。"""
    return Job(
        job_id=job_id,
        kind="document",
        project_id=project_id,
        doc_key=doc_key,
        status=status,  # type: ignore[arg-type]
        created_at=FIXED_NOW,
        inputs_hash=inputs_hash,
    )


@dataclass
class ScriptedStage:
    """可编程阶段体：记录调用、可注入失败或"不适用"、可做一次决策。"""

    name: str
    outputs_key: str = ""
    #: 前 N 次调用返回失败（用于验证"失败 → 续跑"）。
    failures: int = 0
    #: 非 ``None`` 时返回"本阶段不适用"。
    skip_reason: str | None = None
    #: 非 ``None`` 时抛裸异常（用于验证编排的兜底）。
    raises: Exception | None = None
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
        key = self.outputs_key or self.name
        rel = context.workspace.put_text(
            f"{self.name}/{key}.txt", f"{self.name}@{context.inputs_hash}"
        )
        return StageOutcome(outputs={key: rel})


def all_stages(**overrides: object) -> list[ScriptedStage]:
    """五个阶段的默认实现；``overrides`` 按阶段名替换参数（如 ``render={"failures": 1}``）。"""
    stages: list[ScriptedStage] = []
    for name in STAGE_ORDER:
        params = overrides.get(name)
        if isinstance(params, dict):
            stages.append(ScriptedStage(name=name, **params))
        else:
            stages.append(ScriptedStage(name=name))
    return stages


def build_registry(*stages: ScriptedStage) -> StageRegistry:
    return StageRegistry({stage.name: stage for stage in stages})
