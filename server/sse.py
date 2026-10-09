"""SSE 事件流适配（DD-13.4 / DD-11.6）。

本模块只有三件事，且都**与 Web 框架无关**（纯函数 + 一个异步生成器），
这样它既能在 FastAPI 下挂载，也能在测试里直接迭代验证：

① **帧编码**——把持久化的 :class:`~docfidelity.contracts.job.JobEvent` 编成 SSE 帧。
② **游标续推**——``Last-Event-ID`` → ``EventLog.since`` → 从断点补推。
③ **心跳**——空闲时按间隔发 ``heartbeat`` 帧。

两条容易做错的边界
------------------
**帧名与持久化类型是两件事**（``pipeline.events`` 的模块文档已声明该界线）。
DD-11.6 的 ``heartbeat`` 是**传输层**帧名；持久化层只有
``job|stage|decision|artifact|log`` 五种类型。心跳落盘为 ``log``，
由本模块把它命名成 ``heartbeat`` 帧。两个包各自只有一处口径。

**45 秒不是服务端的参数**。DD-13.4 的"心跳 15s / 超时 45s 判死并重连"里，
后一半是**客户端**纪律：它若 45s 收不到任何帧就重连。服务端的义务只有一个——
把心跳间隔压在 45s 以下（默认 15s），并且**绝不因为作业跑得久而掐掉流**
（渲染一次可能好几分钟，中间没有事件是正常的）。所以本模块没有"空闲超时"这一档，
它若存在，长作业会在正常推进中被自己掐断。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from typing import Any, Final

from docfidelity.contracts.errors import EngineError, ErrorCode
from docfidelity.contracts.job import Job, JobEvent
from docfidelity.pipeline import EventLog

from .jobs.events import WorkspaceEventLog, execution_pending

__all__ = [
    "HEARTBEAT_FRAME",
    "RETRY_MILLISECONDS",
    "STREAM_END_FRAME",
    "SETTLED_STATUSES",
    "encode_event",
    "encode_heartbeat",
    "frame_name",
    "job_stream_settled",
    "parse_last_event_id",
    "stream_events",
]

#: 心跳帧名（传输层，见模块文档）。
HEARTBEAT_FRAME: Final = "heartbeat"

#: 历史补推完成后的关闭信号，不占持久化事件序号。
STREAM_END_FRAME: Final = "stream_end"

SETTLED_STATUSES: Final[frozenset[str]] = frozenset(
    {"succeeded", "failed", "canceled", "partial_failure"}
)


def job_stream_settled(log: EventLog, job: Job) -> bool:
    """状态与持久化的最终事件均已发布后，才允许结束流。"""
    if job.status not in SETTLED_STATUSES:
        return False
    # 取消可让父作业先落成部分失败，所有终态均须等待子作业登记收尾。
    tracked = job.child_job_ids if job.is_orchestration else [job.job_id]
    for job_id in tracked:
        if isinstance(log, WorkspaceEventLog) and log.execution_active(job_id):
            return False
        if execution_pending(log, job_id):
            return False
    latest = next(
        (event for event in reversed(log.events(job.job_id)) if event.type == "job"), None
    )
    if latest is None:
        return True  # 没有事件历史的旧作业仍可正常查看。
    if "status" in latest.payload:
        return bool(latest.payload["status"] == job.status)
    if job.is_orchestration and job.progress is not None:
        # 旧版编排事件只有进度字段。
        return all(
            latest.payload.get(key) == value
            for key, value in job.progress.model_dump(exclude={"format_version"}).items()
        )
    return False


#: 建议客户端重连延迟（SSE ``retry`` 字段，毫秒）。
RETRY_MILLISECONDS: Final = 3000


def frame_name(event: JobEvent) -> str:
    """事件 → SSE 帧名。

    心跳在持久化层是 ``log`` + ``payload["heartbeat"] is True``；本函数是这两层之间
    **唯一**的翻译点。加一处同样的判断就会有两处口径，而它们迟早不一致。
    """
    if event.type == "log" and event.payload.get("heartbeat") is True:
        return HEARTBEAT_FRAME
    return event.type


def _encode(name: str, event_id: int, data: str) -> str:
    return f"event: {name}\nid: {event_id}\ndata: {data}\n\n"


def encode_event(event: JobEvent) -> str:
    """编一条事件帧。``data`` 是契约模型的 JSON，客户端可直接按类型解析。"""
    return _encode(frame_name(event), event.event_id, event.model_dump_json())


def encode_heartbeat(event: JobEvent) -> str:
    """编一条心跳帧。数据体仍带 ``job_id`` 与时间，便于排查"流是否还活着"。"""
    payload: dict[str, Any] = {"job_id": event.job_id, "at": event.created_at.isoformat()}
    return _encode(HEARTBEAT_FRAME, event.event_id, json.dumps(payload, ensure_ascii=False))


def parse_last_event_id(raw: str | None) -> int:
    """解析 ``Last-Event-ID``。

    缺失 → ``0``（从头补推，at-least-once，安全）。
    非法 → **报错**，不回退成 0：回退会让客户端以为"补推正常"，而它其实静默地
    重收了整段历史；把一次协议错误表现为一段重复流量，是最难排查的那种。
    """
    if raw is None or not raw.strip():
        return 0
    try:
        value = int(raw.strip())
    except ValueError as exc:
        raise EngineError(
            ErrorCode.STATE_INVALID,
            f"Last-Event-ID 不是合法序号：{raw!r}",
            where={"last_event_id": raw},
            remediation=["客户端应回传上一次收到的事件 id（整数）"],
        ) from exc
    if value < 0:
        raise EngineError(
            ErrorCode.STATE_INVALID,
            f"Last-Event-ID 不能为负：{value}",
            where={"last_event_id": value},
            remediation=["回传 0 表示从头补推"],
        )
    return value


async def stream_events(
    *,
    log: EventLog,
    job_id: str,
    last_event_id: int = 0,
    is_settled: Callable[[], bool] | None = None,
    heartbeat_seconds: float = 15.0,
    poll_interval: float = 0.05,
    preamble: bool = True,
) -> AsyncIterator[str]:
    """按游标补推、随后持续推送，直到作业进入终态且无待推事件。

    Parameters
    ----------
    log:
        事件日志。**先落库再推送**由它保证（``emit`` 返回时已然持久化），
        所以本函数天然不会推出"尚未落库"的事件。
    last_event_id:
        客户端已收到的最后一条序号；缺省 0 表示从头补推。
    is_settled:
        该作业是否已进入终态。``None`` 表示不限（例如服务端重启后补推历史）。
        终态判定由调用方给（作业状态机在 ``pipeline``），本模块不自行推断。
    preamble:
        是否先发一帧 ``retry`` 提示。它让客户端在**首次**断线时就知道重连间隔，
        而不是等到第一次超时才发现。
    """
    if heartbeat_seconds <= 0:
        raise EngineError(
            ErrorCode.CONFIG_INVALID,
            f"心跳间隔必须为正，收到 {heartbeat_seconds!r}",
            where={"heartbeat_seconds": heartbeat_seconds},
            remediation=["取一个小于 45 秒的正数（DD-13.4 的客户端判死阈值）"],
        )
    if preamble:
        yield f"retry: {RETRY_MILLISECONDS}\n\n"

    cursor = last_event_id
    idle = 0.0
    while True:
        pending = log.since(job_id, cursor)
        if pending:
            idle = 0.0
            for event in pending:
                cursor = event.event_id
                yield encode_event(event)
            continue

        if is_settled is not None and is_settled():
            # 交回主循环补推，yield 期间发生重试后须重新核对当前状态。
            if log.since(job_id, cursor):
                continue
            yield f"event: {STREAM_END_FRAME}\ndata: {{}}\n\n"
            return

        await asyncio.sleep(poll_interval)
        idle += poll_interval
        if idle >= heartbeat_seconds:
            idle = 0.0
            yield encode_heartbeat(log.heartbeat(job_id))
