"""作业子系统：调度、项目锁、worker（DD-11.4 / DD-11.5）。

本层是 ``pipeline`` 与 HTTP 之间唯一的执行体：它把"哪些作业该跑、谁先跑、跑完写什么"
定下来，而把"阶段内部做什么"完全交给注入的 :class:`~docfidelity.pipeline.StageRegistry`。
"""

from __future__ import annotations

from .events import WorkspaceEventLog
from .worker import (
    ARTIFACT_KIND_BY_SUFFIX,
    REPORT_OUTPUT_KEY,
    JobRunner,
    RunOutcome,
    SessionFactory,
    WorkerLoop,
    WorkspaceEventSink,
    artifact_id_of,
    artifact_kind_of,
)

__all__ = [
    "ARTIFACT_KIND_BY_SUFFIX",
    "REPORT_OUTPUT_KEY",
    "JobRunner",
    "RunOutcome",
    "SessionFactory",
    "WorkerLoop",
    "WorkspaceEventSink",
    "WorkspaceEventLog",
    "artifact_id_of",
    "artifact_kind_of",
]
