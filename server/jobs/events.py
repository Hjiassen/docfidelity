"""共享工作区事件日志：API 与 worker 通过文件锁串行分配作业游标。"""

from __future__ import annotations

import errno
import os
import sys
import time
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO

from docfidelity.contracts.errors import EngineError, ErrorCode
from docfidelity.contracts.job import EventType, JobEvent
from docfidelity.pipeline import EVENTS_FILENAME, EventLog, path_segment, read_events
from docfidelity.platform.fs import ensure_dir, safe_join

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl

__all__ = ["WorkspaceEventLog", "WorkspaceExecutionLocks", "execution_pending"]

_LOCK_FILENAME = ".events.lock"
_EXECUTION_LOCK_FILENAME = ".execution.lock"
_LOCK_TIMEOUT_SECONDS = 10.0


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


def _history_error(job_id: str, detail: str) -> EngineError:
    return EngineError(
        ErrorCode.CHECKPOINT_MISSING,
        "事件历史损坏，不能继续分配或回放作业游标",
        where={"job_id": job_id, "detail": detail},
        remediation=["备份并检查 events.jsonl 的完整性；重复或截断的历史不得自动重编号"],
    )


def _validate_history(job_id: str, events: Sequence[JobEvent]) -> None:
    for expected, event in enumerate(events, start=1):
        if event.job_id != job_id or event.event_id != expected:
            raise _history_error(
                job_id, f"期望 {job_id}@{expected}，实际 {event.job_id}@{event.event_id}"
            )


def _try_acquire(stream: BinaryIO) -> bool:
    try:
        if sys.platform == "win32":
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError as error:
        if error.errno not in (errno.EACCES, errno.EAGAIN):
            raise
        return False


def _acquire(stream: BinaryIO) -> None:
    deadline = time.monotonic() + _LOCK_TIMEOUT_SECONDS
    while not _try_acquire(stream):
        if time.monotonic() >= deadline:
            raise OSError(errno.EAGAIN, "事件锁等待超时")
        time.sleep(0.02)


def _release(stream: BinaryIO) -> None:
    if sys.platform == "win32":
        stream.seek(0)
        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


@contextmanager
def _lock_file(root: Path, job_id: str, filename: str) -> Iterator[BinaryIO]:
    directory = safe_join(root, "jobs", path_segment(job_id, field="job_id"))
    try:
        ensure_dir(directory)
        with safe_join(directory, filename).open("a+b") as stream:
            if stream.seek(0, os.SEEK_END) == 0:
                stream.write(b"\0")
                stream.flush()
            yield stream
    except OSError as error:
        raise _history_error(job_id, f"事件文件读写或加锁失败：{type(error).__name__}") from error


def execution_pending(log: EventLog, job_id: str) -> bool:
    """上次执行是否尚未发布完整的产物登记收尾。"""
    return next(
        (
            bool(event.payload["execution_active"])
            for event in reversed(log.events(job_id))
            if event.type == "job" and "execution_active" in event.payload
        ),
        False,
    )


class WorkspaceExecutionLocks:
    """执行期间持有系统锁；崩溃自动释放，API 重启不会影响独立 worker。"""

    def __init__(self, root: Path | str) -> None:
        self._root = Path(root)

    @contextmanager
    def acquire(self, job_id: str) -> Iterator[bool]:
        with _lock_file(self._root, job_id, _EXECUTION_LOCK_FILENAME) as stream:
            acquired = _try_acquire(stream)
            try:
                yield acquired
            finally:
                if acquired:
                    _release(stream)

    def active(self, job_id: str) -> bool:
        """非阻塞探测，绝不等待运行中的阶段完成。"""
        with self.acquire(job_id) as acquired:
            return not acquired

    @contextmanager
    def guard(self, job_id: str) -> Iterator[None]:
        """恢复类动作须在执行与收尾均完成后取得同一把锁。"""
        with self.acquire(job_id) as acquired:
            if not acquired:
                raise EngineError(
                    ErrorCode.STATE_INVALID,
                    "作业仍在执行或登记产物，暂时不能重试或续跑",
                    where={"job_id": job_id},
                    remediation=["等待当前阶段和产物登记完成后重试"],
                )
            yield


class WorkspaceEventLog(EventLog):
    """每次读取持久历史，读写共享每个作业的排他锁。

    锁文件保持同一路径，不能写完后删除；进程退出时操作系统释放锁。
    共享目录必须支持宿主系统文件锁，所有事件写入者均须使用本适配器。
    """

    def __init__(self, root: Path | str, *, now: Callable[[], datetime] | None = None) -> None:
        super().__init__(now=now)
        self._root = Path(root)
        self._clock = now or _utcnow
        self._execution_locks = WorkspaceExecutionLocks(self._root)

    def _directory(self, job_id: str) -> Path:
        return safe_join(self._root, "jobs", path_segment(job_id, field="job_id"))

    @contextmanager
    def _locked(self, job_id: str) -> Iterator[Path]:
        directory = self._directory(job_id)
        with _lock_file(self._root, job_id, _LOCK_FILENAME) as stream:
            _acquire(stream)
            try:
                yield safe_join(directory, EVENTS_FILENAME)
            finally:
                _release(stream)

    def execution_active(self, job_id: str) -> bool:
        return self._execution_locks.active(job_id)

    @staticmethod
    def _read(job_id: str, path: Path) -> list[JobEvent]:
        try:
            if path.is_file() and path.stat().st_size:
                with path.open("rb") as stream:
                    stream.seek(-1, os.SEEK_END)
                    if stream.read(1) != b"\n":
                        raise _history_error(job_id, "末行未完整结束")
            events = read_events(path)
        except UnicodeError as error:
            raise _history_error(job_id, "事件文件不是有效 UTF-8") from error
        _validate_history(job_id, events)
        return events

    @staticmethod
    def _append(path: Path, events: Sequence[JobEvent]) -> None:
        with path.open("a", encoding="utf-8", newline="\n") as stream:
            for event in events:
                stream.write(event.model_dump_json() + "\n")
            stream.flush()
            os.fsync(stream.fileno())

    def emit(
        self, job_id: str, type: EventType, payload: Mapping[str, Any] | None = None
    ) -> JobEvent:
        """锁内读取游标、追加事件并 fsync；返回时其他进程即可读取。"""
        with self._locked(job_id) as path:
            history = self._read(job_id, path)
            event = JobEvent(
                event_id=len(history) + 1,
                job_id=job_id,
                type=type,
                payload=dict(payload or {}),
                created_at=self._clock(),
            )
            self._append(path, [event])
            return event

    def events(self, job_id: str) -> tuple[JobEvent, ...]:
        with self._locked(job_id) as path:
            return tuple(self._read(job_id, path))

    def since(self, job_id: str, last_event_id: int = 0) -> list[JobEvent]:
        return [event for event in self.events(job_id) if event.event_id > last_event_id]

    def last_event_id(self, job_id: str) -> int:
        events = self.events(job_id)
        return events[-1].event_id if events else 0

    def restore(self, job_id: str, events: Iterable[JobEvent]) -> None:
        """兼容启动回放；已有磁盘历史仅验证前缀，不重复追加或覆盖。"""
        restored = list(events)
        _validate_history(job_id, restored)
        with self._locked(job_id) as path:
            existing = self._read(job_id, path)
            if existing:
                if existing[: len(restored)] != restored:
                    raise _history_error(job_id, "回放与持久事件前缀不一致")
            elif restored:
                self._append(path, restored)
