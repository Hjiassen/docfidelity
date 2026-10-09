"""作业事件与发布（DD-11.6）。

事件是「界面看到的东西」与「重启后还能看到的东西」之间唯一的桥。三条纪律：

1. **先落库再推送**。:meth:`EventLog.emit` 先把事件写进持久落地（JSONL），再返回给
   调用方去推送。反过来的话，进程在推送后被杀死，那条事件就只在某个人已经关掉的
   浏览器里存在过。
2. **``event_id`` 在作业内单调自增**。断线重连按 ``Last-Event-ID`` 从游标续推；
   客户端只要报"我最后收到的是几号"，服务端就能算出该补哪些——这也是"不允许静默
   停止推送"（P09）的可执行形态：漏推与否是一个可以算出来的事实，不是一种感觉。
3. **事件名与持久化类型是两件事**。DD-11.6 的 SSE 帧名 ``heartbeat`` 属于传输层
   （``server`` 的 ``sse.py``）；本模块的 :class:`~docfidelity.contracts.job.JobEvent`
   只认 ``job|stage|decision|artifact|log`` 五种持久化类型。心跳因此落为 ``log``，
   由传输层负责把它命名成 ``heartbeat`` 帧——两个包各自只有一处口径。

双写（DB + ``events.jsonl``）里的"DB 那一半"由 ``server`` 承担；本模块负责
**JSONL 那一半**与内存索引，并提供 :func:`read_events` 供进程重启后回放重建游标。
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final, Protocol

from docfidelity.contracts.errors import EngineError, ErrorCode
from docfidelity.contracts.job import EventType, JobEvent
from docfidelity.contracts.qa import Finding
from docfidelity.platform.fs import ensure_dir, read_text

__all__ = [
    "EVENTS_FILENAME",
    "EventLog",
    "EventSink",
    "JsonlEventSink",
    "read_events",
]

#: 作业事件文件名（DD-11.7：``jobs/<job_id>/events.jsonl``）。
EVENTS_FILENAME: Final = "events.jsonl"


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


class EventSink(Protocol):
    """持久化事件的下游（JSONL / DB / 二者）。"""

    def write(self, event: JobEvent) -> None:
        """写一条事件。实现必须保证"返回即已持久化"。"""
        ...


class JsonlEventSink:
    """追加写 ``events.jsonl``。一行一条事件，便于 ``tail`` 与人工排查。"""

    def __init__(self, path: Path | str) -> None:
        self._path = Path(path)

    @property
    def path(self) -> Path:
        return self._path

    def write(self, event: JobEvent) -> None:
        ensure_dir(self._path.parent)
        line = json.dumps(event.model_dump(mode="json"), ensure_ascii=False, sort_keys=True)
        with self._path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(line + "\n")


def read_events(path: Path | str) -> list[JobEvent]:
    """回放 ``events.jsonl``；空行忽略，坏行**报错**而不是跳过。

    跳过坏行会让"重启后续推"悄悄少一段：客户端拿到的序列有洞，而洞只在
    某条特定事件上才看得出来。宁可在重启时明确失败。
    """
    target = Path(path)
    if not target.is_file():
        return []
    events: list[JobEvent] = []
    for number, raw in enumerate(read_text(target).splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            raise _broken_line(target, number, exc.msg) from exc
        try:
            events.append(JobEvent.model_validate(payload))
        except ValueError as exc:  # pydantic ValidationError 亦是 ValueError
            raise _broken_line(target, number, f"契约校验失败：{exc}") from exc
    return events


def _broken_line(path: Path, number: int, detail: str) -> EngineError:
    return EngineError(
        ErrorCode.CHECKPOINT_MISSING,
        f"事件日志第 {number} 行无法解析：{path}",
        where={"path": str(path), "line": number, "detail": detail},
        remediation=["检查该行是否为完整事件；损坏的事件日志会导致重连补推出现缺口"],
    )


class EventLog:
    """作业事件日志：内存索引 + 可选的持久化下游。

    Parameters
    ----------
    sink:
        持久化下游；``None`` 表示只用内存（测试与无盘场景）。
    now:
        取时函数；测试注入固定时钟，使事件时间可复现。
    """

    def __init__(self, sink: EventSink | None = None, *, now: Any = None) -> None:
        self._sink = sink
        self._now = now or _utcnow
        self._events: dict[str, list[JobEvent]] = {}

    # ------------------------------------------------------------ 写入
    def emit(
        self, job_id: str, type: EventType, payload: Mapping[str, Any] | None = None
    ) -> JobEvent:
        """追加一条事件并**立即持久化**，返回该事件。"""
        bucket = self._events.setdefault(job_id, [])
        event = JobEvent(
            event_id=len(bucket) + 1,
            job_id=job_id,
            type=type,
            payload=dict(payload or {}),
            created_at=self._now(),
        )
        if self._sink is not None:
            self._sink.write(event)
        bucket.append(event)
        return event

    def emit_stage(self, job_id: str, stage: str, status: str, progress: float = 0.0) -> JobEvent:
        """阶段进度事件（DD-11.6 的 ``event: stage``）。"""
        return self.emit(
            job_id,
            "stage",
            {"stage": stage, "status": status, "progress": progress},
        )

    def emit_finding(self, job_id: str, finding: Finding) -> JobEvent:
        """门禁判定事件（DD-11.6 的 ``event: finding``）。"""
        return self.emit(
            job_id,
            "log",
            {
                "rule_id": finding.rule_id,
                "severity": finding.severity,
                "message": finding.message,
                "location": finding.location.model_dump(mode="json"),
            },
        )

    def emit_orchestration(
        self, job_id: str, *, done: int, total: int, failed: int, current_child: str | None
    ) -> JobEvent:
        """编排作业的聚合事件（DD-11.6 约定②：子作业 ``n/N`` + 当前阶段）。"""
        return self.emit(
            job_id,
            "job",
            {"done": done, "total": total, "failed": failed, "current_child": current_child},
        )

    def heartbeat(self, job_id: str) -> JobEvent:
        """心跳事件（传输层据此命名 ``heartbeat`` 帧；持久化类型为 ``log``）。"""
        return self.emit(job_id, "log", {"heartbeat": True})

    # ------------------------------------------------------------ 读取
    def events(self, job_id: str) -> tuple[JobEvent, ...]:
        return tuple(self._events.get(job_id, ()))

    def since(self, job_id: str, last_event_id: int = 0) -> list[JobEvent]:
        """``last_event_id`` 之后的事件，供断线重连续推。

        严格大于 ``last_event_id``：客户端报的那个 id 是**已收到**的最后一条，
        再推一次会重复。
        """
        return [event for event in self._events.get(job_id, ()) if event.event_id > last_event_id]

    def last_event_id(self, job_id: str) -> int:
        bucket = self._events.get(job_id)
        return bucket[-1].event_id if bucket else 0

    # ------------------------------------------------------------ 重启恢复
    def restore(self, job_id: str, events: Iterable[JobEvent]) -> None:
        """用回放出来的事件重建该作业的内存序列与 id 游标。

        只接受 ``event_id`` 严格递增的序列——乱序或重复说明日志本身被写过两次，
        此时"续推游标"已不可信，必须报错而不是将就。
        """
        bucket = self._events.setdefault(job_id, [])
        expected = bucket[-1].event_id if bucket else 0
        for event in events:
            if event.event_id != expected + 1:
                raise EngineError(
                    ErrorCode.CHECKPOINT_MISSING,
                    f"事件回放不连续：期望 {expected + 1}，实际 {event.event_id}",
                    where={"job_id": job_id, "expected": expected + 1, "actual": event.event_id},
                    remediation=["检查 events.jsonl 是否被重复写入或截断"],
                )
            bucket.append(event)
            expected = event.event_id
