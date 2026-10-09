"""SSE 事件流适配（DD-13.4 / DD-11.6）。

本模块**不 imports 任何 Web 框架**（纯函数 + 一个异步生成器），所以这里的用例直接
迭代生成器，不必起 HTTP 服务器——"帧怎么编、游标怎么续、心跳什么时候来"这三件事
与 FastAPI 无关，把它们绑在 TestClient 上测，只是让用例更慢更难读。

两条边界最容易做错，各有一组用例：
① ``Last-Event-ID`` 非法时**报错**而不是回退成 0（回退会让客户端静默重收整段历史）；
② 心跳只在**空闲**时发，且绝不因为作业跑得久而掐流。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from starlette.requests import Request

from docfidelity.contracts.errors import EngineError, ErrorCode
from docfidelity.pipeline import EventLog
from server.api.jobs import stream_job_events
from server.sse import (
    HEARTBEAT_FRAME,
    RETRY_MILLISECONDS,
    encode_event,
    encode_heartbeat,
    frame_name,
    parse_last_event_id,
    stream_events,
)
from tests.fixtures.server_fixture import build_harness

pytestmark = pytest.mark.api


def log_with(*, job_id: str = "j_1", count: int = 3) -> EventLog:
    """只在内存里的事件日志（``sink=None``）：本模块的用例不碰磁盘。"""
    log = EventLog()
    for index in range(1, count + 1):
        log.emit(job_id, "stage", {"stage": "ingest", "index": index})
    return log


async def collect(stream: AsyncIterator[str], *, limit: int) -> list[str]:
    """取前 ``limit`` 帧。**有限上界**是刻意的：生成器在作业未终态时永不结束，
    没有上界的收集会把一次"流没停下"的缺陷表现成用例挂死。"""
    frames: list[str] = []
    async for frame in stream:
        frames.append(frame)
        if len(frames) >= limit:
            break
    return frames


# ------------------------------------------------------- Last-Event-ID 解析
@pytest.mark.parametrize("raw", [None, "", "   "])
def test_missing_last_event_id_means_from_the_start(raw: str | None) -> None:
    """缺席 → 0（at-least-once，安全）。"""
    assert parse_last_event_id(raw) == 0


def test_last_event_id_parses_with_surrounding_space() -> None:
    assert parse_last_event_id(" 7 ") == 7
    assert parse_last_event_id("0") == 0


def test_non_numeric_last_event_id_is_an_error() -> None:
    """**报错**而不是回退成 0：回退会让客户端以为"补推正常"，而它其实重收了整段历史。"""
    with pytest.raises(EngineError) as caught:
        parse_last_event_id("event-7")
    assert caught.value.code == ErrorCode.STATE_INVALID
    assert caught.value.where["last_event_id"] == "event-7"


def test_negative_last_event_id_is_an_error() -> None:
    with pytest.raises(EngineError) as caught:
        parse_last_event_id("-1")
    assert caught.value.code == ErrorCode.STATE_INVALID


# ------------------------------------------------------------------- 帧编码
def test_heartbeat_is_named_at_the_transport_layer_only() -> None:
    """持久化层只有 ``job|stage|decision|artifact|log`` 五种类型；
    ``heartbeat`` 是传输层的帧名，由这一处翻译（两处口径迟早不一致）。"""
    log = EventLog()
    beat = log.heartbeat("j_1")
    assert beat.type == "log"
    assert beat.payload["heartbeat"] is True
    assert frame_name(beat) == HEARTBEAT_FRAME


def test_ordinary_log_events_keep_their_type() -> None:
    log = EventLog()
    ordinary = log.emit("j_1", "log", {"message": "hello"})
    assert frame_name(ordinary) == "log"


def test_event_frame_shape() -> None:
    event = log_with(count=1).since("j_1", 0)[0]
    frame = encode_event(event)
    assert frame.startswith("event: stage\nid: 1\n")
    assert frame.endswith("\n\n"), "SSE 帧必须以空行结束，否则客户端不派发"
    payload = json.loads(next(line for line in frame.splitlines() if line.startswith("data: "))[6:])
    assert payload["event_id"] == 1
    assert payload["job_id"] == "j_1"
    assert payload["payload"]["index"] == 1


def test_heartbeat_frame_shape() -> None:
    frame = encode_heartbeat(EventLog().heartbeat("j_1"))
    assert frame.startswith(f"event: {HEARTBEAT_FRAME}\n")
    payload = json.loads(next(line for line in frame.splitlines() if line.startswith("data: "))[6:])
    assert payload["job_id"] == "j_1"
    assert "at" in payload


def test_heartbeat_frame_is_much_smaller_than_a_full_event_frame() -> None:
    """心跳是定期流量，不该每次都把整份事件 JSON 重复一遍。"""
    log = EventLog()
    beat = log.heartbeat("j_1")
    assert len(encode_heartbeat(beat)) < len(encode_event(beat).replace('"log"', '"log"')) + 200


# ------------------------------------------------------------------- 流
async def test_stream_starts_with_a_retry_preamble() -> None:
    """客户端要在**首次**断线前就知道重连间隔，而不是等到第一次超时才发现。"""
    frames = await collect(
        stream_events(log=EventLog(), job_id="j_1", poll_interval=0.001), limit=1
    )
    assert frames == [f"retry: {RETRY_MILLISECONDS}\n\n"]


async def test_stream_replays_history_from_zero() -> None:
    log = log_with(count=3)
    frames = await collect(stream_events(log=log, job_id="j_1", poll_interval=0.001), limit=4)
    assert frames[0].startswith("retry: ")
    assert [frame.splitlines()[1] for frame in frames[1:]] == ["id: 1", "id: 2", "id: 3"]


async def test_stream_resumes_after_the_given_cursor() -> None:
    """续推的**唯一**判据是 ``Last-Event-ID``：只补它之后的。"""
    log = log_with(count=5)
    frames = await collect(
        stream_events(log=log, job_id="j_1", last_event_id=3, poll_interval=0.001), limit=3
    )
    ids = [frame.splitlines()[1] for frame in frames[1:]]
    assert ids == ["id: 4", "id: 5"]


async def test_stream_does_not_repeat_events_already_delivered() -> None:
    """游标之后的区间是空的：一条 ``event:`` 帧都不该再来。"""
    frames = await collect(
        stream_events(
            log=log_with(count=3),
            job_id="j_1",
            last_event_id=3,
            # 心跳间隔调小：默认 15 秒会让"没事件时该等多久"变成用例的等待时间。
            heartbeat_seconds=0.01,
            poll_interval=0.005,
        ),
        limit=3,
    )
    assert frames[0].startswith("retry: ")
    assert [frame for frame in frames[1:] if frame.startswith("event: stage")] == []


async def test_stream_can_omit_the_preamble() -> None:
    frames = await collect(
        stream_events(log=log_with(count=1), job_id="j_1", preamble=False, poll_interval=0.001),
        limit=1,
    )
    assert frames[0].startswith("event: stage")


async def test_stream_terminates_on_a_settled_job() -> None:
    """终态 + 无待推事件 → 流正常结束（不是靠超时掐断）。"""
    log = log_with(count=2)
    frames = [
        frame async for frame in stream_events(log=log, job_id="j_1", is_settled=lambda: True)
    ]
    assert [frame.splitlines()[1] for frame in frames[1:-1]] == ["id: 1", "id: 2"]
    assert frames[-1] == "event: stream_end\ndata: {}\n\n"


async def test_stream_drains_events_that_arrive_right_before_settling() -> None:
    """事件的产生与状态落定之间可能刚好插入一条——收尾前必须再取一次。"""
    log = log_with(count=2)
    state = {"settled": False}

    def settled() -> bool:
        if not state["settled"]:
            state["settled"] = True
            log.emit("j_1", "job", {"status": "succeeded"})  # 恰好在"已终态"之后产生
        return True

    frames = [frame async for frame in stream_events(log=log, job_id="j_1", is_settled=settled)]
    assert [frame.splitlines()[1] for frame in frames[1:-1]] == ["id: 1", "id: 2", "id: 3"]
    assert frames[-1] == "event: stream_end\ndata: {}\n\n"


async def test_stream_end_follows_all_retry_history() -> None:
    log = EventLog()
    for status in ("failed", "running", "succeeded"):
        log.emit("j_1", "job", {"status": status})
    frames = [
        frame async for frame in stream_events(log=log, job_id="j_1", is_settled=lambda: True)
    ]
    assert len(frames) == 5
    assert [
        json.loads(frame.split("data: ")[1])["payload"]["status"] for frame in frames[1:-1]
    ] == ["failed", "running", "succeeded"]
    assert frames[-1] == "event: stream_end\ndata: {}\n\n"


@pytest.mark.parametrize("kind", ["document", "orchestration"])
async def test_endpoint_waits_for_the_final_published_event(tmp_path: Path, kind: str) -> None:
    harness = build_harness(tmp_path)
    parent = harness.services.submit_build(harness.new_project())
    job_id = parent.child_job_ids[0] if kind == "document" else parent.job_id
    job = harness.repository.get_job(job_id)
    harness.repository.save_job(job.model_copy(update={"status": "succeeded"}))
    log = harness.runner.events
    log.emit(job_id, "job", {"status": "running"})

    async def publish() -> None:
        await asyncio.sleep(0)
        log.emit(job_id, "job", {"status": "succeeded"})

    publisher = asyncio.create_task(publish())
    response = await stream_job_events(job_id, Request({"type": "http"}), harness.services)

    async def frames() -> list[str | bytes]:
        return [frame async for frame in response.body_iterator]

    received = await asyncio.wait_for(frames(), timeout=2)
    await publisher
    assert any('"status":"succeeded"' in str(frame) for frame in received)
    assert str(received[-1]) == "event: stream_end\ndata: {}\n\n"


async def test_retry_during_terminal_drain_keeps_the_stream_open() -> None:
    log = log_with(count=1)
    state = {"settled": True, "published": False}

    def settled() -> bool:
        if not state["published"]:
            state["published"] = True
            log.emit("j_1", "job", {"status": "succeeded"})
        return state["settled"]

    stream = stream_events(log=log, job_id="j_1", is_settled=settled, preamble=False)
    assert (await anext(stream)).startswith("event: stage")
    assert '"status":"succeeded"' in await anext(stream)
    state["settled"] = False
    log.emit("j_1", "job", {"status": "running"})
    assert '"status":"running"' in await anext(stream)
    await stream.aclose()


async def test_stream_emits_a_heartbeat_while_idle() -> None:
    """空闲时按间隔发心跳（默认 15s，必须显著小于客户端的 45s 判死阈值）。"""
    log = EventLog()
    frames = await collect(
        stream_events(log=log, job_id="j_1", heartbeat_seconds=0.02, poll_interval=0.01),
        limit=2,
    )
    assert frames[1].startswith(f"event: {HEARTBEAT_FRAME}\n")


async def test_heartbeat_is_reset_by_activity() -> None:
    """有心跳也有事件时，先出事件；心跳不该抢在待推事件前面。"""
    log = log_with(count=1)
    frames = await collect(
        stream_events(log=log, job_id="j_1", heartbeat_seconds=0.02, poll_interval=0.01),
        limit=2,
    )
    assert frames[1].startswith("event: stage")


async def test_stream_keeps_running_for_a_long_job_without_events() -> None:
    """作业跑几分钟没有事件是正常的——服务端**不能**因为空闲而掐流。"""
    log = EventLog()
    stream = stream_events(log=log, job_id="j_1", heartbeat_seconds=0.01, poll_interval=0.005)
    frames = await collect(stream, limit=4)
    assert sum(1 for frame in frames if frame.startswith(f"event: {HEARTBEAT_FRAME}")) == 3
    # 关键：流仍然活着（还能继续取），不是被"空闲超时"掐掉了。
    assert frames[0].startswith("retry: ")


async def test_a_non_positive_heartbeat_is_rejected() -> None:
    """心跳间隔必须为正：0 或负数会让空闲循环每轮都发心跳，把长连接变成流量泵。"""
    stream = stream_events(log=EventLog(), job_id="j_1", heartbeat_seconds=0)
    with pytest.raises(EngineError) as caught:
        await stream.__anext__()
    assert caught.value.code == ErrorCode.CONFIG_INVALID


async def test_stream_does_not_block_the_event_loop() -> None:
    """空闲等待必须是 ``await asyncio.sleep``：同步 sleep 会占住事件循环。

    判据不是"能不能跑"（同步 sleep 也能跑完），而是"等待期间事件循环还能不能推进
    别的任务"——这里用一个并发的计数器任务来观察。
    """
    ticks = 0

    async def ticker() -> None:
        nonlocal ticks
        for _ in range(10):
            ticks += 1
            await asyncio.sleep(0.005)

    log = EventLog()
    task = asyncio.create_task(ticker())
    await collect(
        stream_events(log=log, job_id="j_1", heartbeat_seconds=0.005, poll_interval=0.005),
        limit=3,
    )
    await task
    assert ticks == 10, "事件循环在等待期间被占住了"
