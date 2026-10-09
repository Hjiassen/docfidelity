"""``pipeline.events``：单调事件号、JSONL 双写、按 ``Last-Event-ID`` 续推。

核心不变量：**event_id 在作业内单调自增且不跳号**。一旦跳号，"我只收到 7 号"这句话
就无法换算出"该补哪些"——重连补推会出现静默缺口（P09 明确禁止）。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from docfidelity.contracts.errors import EngineError
from docfidelity.contracts.qa import Finding
from docfidelity.pipeline import EVENTS_FILENAME, EventLog, JsonlEventSink, read_events

pytestmark = pytest.mark.unit


def _clock() -> datetime:
    return datetime(2026, 9, 21, 12, 0, 0, tzinfo=timezone.utc)


def _log(tmp_path: Path | None = None) -> EventLog:
    sink = JsonlEventSink(tmp_path / EVENTS_FILENAME) if tmp_path is not None else None
    return EventLog(sink, now=_clock)


class TestEmit:
    def test_event_ids_are_monotonic_within_a_job(self) -> None:
        log = _log()
        assert [log.emit("j1", "job", {"n": n}).event_id for n in range(3)] == [1, 2, 3]

    def test_sequences_are_per_job(self) -> None:
        log = _log()
        log.emit("j1", "job", {})
        assert log.emit("j2", "job", {}).event_id == 1

    def test_event_id_never_repeats_after_many_emits(self) -> None:
        log = _log()
        ids = [log.emit("j1", "log", {"i": i}).event_id for i in range(50)]
        assert len(set(ids)) == len(ids)

    def test_last_event_id_tracks_the_tail(self) -> None:
        log = _log()
        assert log.last_event_id("j1") == 0
        log.emit("j1", "job", {})
        assert log.last_event_id("j1") == 1
        assert log.last_event_id("unknown") == 0


class TestPayloads:
    def test_stage_event_carries_stage_status_progress(self) -> None:
        event = _log().emit_stage("j1", "render", "running", 0.4)
        assert event.type == "stage"
        assert event.payload == {"stage": "render", "status": "running", "progress": 0.4}

    def test_finding_event_flattens_the_finding(self) -> None:
        finding = Finding(rule_id="FR-M5-07", severity="blocker", message="越界")
        event = _log().emit_finding("j1", finding)
        assert event.payload["rule_id"] == "FR-M5-07"
        assert event.payload["severity"] == "blocker"
        assert "location" in event.payload

    def test_orchestration_event_reports_child_progress(self) -> None:
        event = _log().emit_orchestration("orch", done=2, total=3, failed=1, current_child="c3")
        assert event.payload == {"done": 2, "total": 3, "failed": 1, "current_child": "c3"}

    def test_heartbeat_is_persisted_as_log_type(self) -> None:
        # DD-11.6 的 SSE 帧名 `heartbeat` 属于传输层；持久化类型只有 5 种，
        # 心跳落为 log，由 server 的 sse.py 负责命名成 heartbeat 帧。
        event = _log().heartbeat("j1")
        assert event.type == "log"
        assert event.payload == {"heartbeat": True}


class TestReplay:
    def test_since_returns_strictly_greater(self) -> None:
        log = _log()
        for _ in range(5):
            log.emit("j1", "log", {})
        assert [event.event_id for event in log.since("j1", 2)] == [3, 4, 5]

    def test_since_zero_returns_everything(self) -> None:
        log = _log()
        log.emit("j1", "log", {})
        assert len(log.since("j1", 0)) == 1

    def test_since_at_tail_is_empty(self) -> None:
        log = _log()
        log.emit("j1", "log", {})
        assert log.since("j1", log.last_event_id("j1")) == []

    def test_cursor_is_per_job(self) -> None:
        log = _log()
        log.emit("j1", "log", {})
        log.emit("j2", "log", {})
        assert len(log.since("j1", 0)) == 1


class TestJsonlSink:
    def test_every_event_is_written_in_order(self, tmp_path: Path) -> None:
        log = _log(tmp_path)
        for index in range(3):
            log.emit("j1", "log", {"i": index})
        lines = (tmp_path / EVENTS_FILENAME).read_text(encoding="utf-8").splitlines()
        assert [json.loads(line)["payload"]["i"] for line in lines] == [0, 1, 2]

    def test_sink_is_written_before_emit_returns(self, tmp_path: Path) -> None:
        # 先落盘再推送：emit 返回时那条事件已经在盘上
        log = _log(tmp_path)
        event = log.emit("j1", "log", {"i": 9})
        on_disk = read_events(tmp_path / EVENTS_FILENAME)
        assert [item.event_id for item in on_disk] == [event.event_id]

    def test_read_events_of_missing_file_is_empty(self, tmp_path: Path) -> None:
        assert read_events(tmp_path / "nope.jsonl") == []

    def test_read_events_ignores_blank_lines(self, tmp_path: Path) -> None:
        log = _log(tmp_path)
        log.emit("j1", "log", {})
        path = tmp_path / EVENTS_FILENAME
        path.write_text(path.read_text(encoding="utf-8") + "\n\n", encoding="utf-8")
        assert len(read_events(path)) == 1

    def test_read_events_fails_loudly_on_a_broken_line(self, tmp_path: Path) -> None:
        # 跳过坏行 = 允许补推出现静默缺口
        log = _log(tmp_path)
        log.emit("j1", "log", {"i": 0})
        path = tmp_path / EVENTS_FILENAME
        path.write_text(path.read_text(encoding="utf-8") + "{ broken\n", encoding="utf-8")
        with pytest.raises(EngineError) as caught:
            read_events(path)
        assert caught.value.where["line"] == 2

    def test_read_events_rejects_a_structurally_invalid_event(self, tmp_path: Path) -> None:
        path = tmp_path / EVENTS_FILENAME
        path.write_text('{"event_id": 1}\n', encoding="utf-8")
        with pytest.raises(EngineError) as caught:
            read_events(path)
        assert caught.value.where["line"] == 1


class TestRestore:
    def test_restore_seeds_the_cursor(self, tmp_path: Path) -> None:
        first = _log(tmp_path)
        first.emit("j1", "log", {"i": 0})
        first.emit("j1", "log", {"i": 1})

        reopened = _log(tmp_path)
        reopened.restore("j1", read_events(tmp_path / EVENTS_FILENAME))
        assert reopened.last_event_id("j1") == 2
        assert reopened.emit("j1", "log", {"i": 2}).event_id == 3
        assert reopened.since("j1", 1)[0].payload == {"i": 1}

    def test_restore_rejects_non_contiguous_log(self, tmp_path: Path) -> None:
        log = _log(tmp_path)
        log.emit("j1", "log", {})
        log.emit("j1", "log", {})
        events = read_events(tmp_path / EVENTS_FILENAME)
        with pytest.raises(EngineError):
            _log(tmp_path).restore("j1", [events[0], events[0]])
