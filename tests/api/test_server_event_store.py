"""独立 API / worker 必须共享持久事件及严格递增的作业游标。"""

from __future__ import annotations

import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from docfidelity.contracts.errors import EngineError
from docfidelity.pipeline import EventLog, StageRegistry
from server.api.deps import build_state
from server.main import APP_STATE_ATTRIBUTE, create_app
from server.settings import ServerSettings
from server.storage import MemoryRepository
from tests.fixtures.server_fixture import build_harness, build_registry

pytestmark = pytest.mark.api


def event_log(root: Path) -> EventLog:
    state = build_state(
        settings=ServerSettings(_env_file=None, storage_root=root),
        repository=MemoryRepository(),
        stages=StageRegistry(),
    )
    return state.runner.events


def test_independent_logs_share_continuous_ids_and_live_history(tmp_path: Path) -> None:
    api = event_log(tmp_path)
    worker = event_log(tmp_path)
    first = api.emit("j_shared", "job", {"status": "queued"})
    second = worker.emit_stage("j_shared", "ingest", "running")
    third = api.heartbeat("j_shared")
    fourth = worker.emit("j_shared", "artifact", {"artifact_id": "a_1"})

    assert [event.event_id for event in (first, second, third, fourth)] == [1, 2, 3, 4]
    assert [event.event_id for event in api.events("j_shared")] == [1, 2, 3, 4]
    assert [event.event_id for event in worker.since("j_shared", 2)] == [3, 4]
    assert api.last_event_id("j_shared") == worker.last_event_id("j_shared") == 4


def test_restart_continues_ids_without_requiring_restore(tmp_path: Path) -> None:
    event_log(tmp_path).emit("j_restart", "job", {})
    restarted = event_log(tmp_path)

    assert restarted.emit("j_restart", "log", {}).event_id == 2
    assert restarted.last_event_id("j_restart") == 2


def test_restore_is_compatible_with_disk_history_and_newer_worker_events(tmp_path: Path) -> None:
    first = event_log(tmp_path)
    first.emit("j_restore", "job", {})
    history = first.events("j_restore")
    second = event_log(tmp_path)
    second.emit("j_restore", "log", {})

    first.restore("j_restore", history)

    assert [event.event_id for event in first.events("j_restore")] == [1, 2]
    assert first.emit("j_restore", "log", {}).event_id == 3


def test_restore_can_import_continuous_history_into_an_empty_store(tmp_path: Path) -> None:
    previous = EventLog()
    previous.emit("j_import", "job", {})
    previous.emit("j_import", "log", {})
    restored = event_log(tmp_path)

    restored.restore("j_import", previous.events("j_import"))

    assert restored.emit("j_import", "log", {}).event_id == 3
    assert len(restored.events("j_import")) == 3


def test_threads_using_independent_logs_also_serialize_event_ids(tmp_path: Path) -> None:
    logs = (event_log(tmp_path), event_log(tmp_path))

    def write(log: EventLog) -> None:
        for _ in range(10):
            log.emit("j_threads", "log", {})

    with ThreadPoolExecutor(max_workers=2) as executor:
        for result in executor.map(write, logs):
            assert result is None

    assert [event.event_id for event in logs[0].events("j_threads")] == list(range(1, 21))


def test_api_started_before_worker_observes_worker_events_in_sse(tmp_path: Path) -> None:
    repository = MemoryRepository()
    seeded = build_harness(tmp_path, repository=repository)
    project_id = seeded.new_project()
    app = create_app(
        settings=ServerSettings(_env_file=None, storage_root=tmp_path),
        repository=repository,
        stages=build_registry(),
        start_worker=False,
    )
    with TestClient(app) as client:
        api = getattr(app.state, APP_STATE_ATTRIBUTE)
        parent = api.services.submit_build(project_id, doc_keys=["overview"])
        worker = build_state(
            settings=ServerSettings(_env_file=None, storage_root=tmp_path),
            repository=repository,
            stages=build_registry(),
        )
        worker.runner.run_until_idle()
        response = client.get(f"/api/v1/jobs/{parent.job_id}/events")

        assert response.status_code == 200
        assert '"done":1' in response.text
        assert "event: stream_end" in response.text


_WRITER_SCRIPT = """
import sys
import time
from pathlib import Path
from docfidelity.pipeline import StageRegistry
from server.api.deps import build_state
from server.settings import ServerSettings
from server.storage import MemoryRepository
state = build_state(settings=ServerSettings(_env_file=None, storage_root=Path(sys.argv[1])),
                    repository=MemoryRepository(), stages=StageRegistry())
print('READY', flush=True)
assert sys.stdin.readline().strip() == 'GO'
for index in range(30):
    state.runner.events.emit('j_parallel', 'log', {'writer': sys.argv[2], 'index': index})
    time.sleep(0.001)
"""


def test_two_real_processes_allocate_unique_continuous_ids(tmp_path: Path) -> None:
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", _WRITER_SCRIPT, str(tmp_path), name],
            cwd=Path(__file__).resolve().parents[2],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for name in ("api", "worker")
    ]
    try:
        for process in processes:
            assert process.stdout is not None and process.stdout.readline().strip() == "READY"
        for process in processes:
            assert process.stdin is not None
            process.stdin.write("GO\n")
            process.stdin.flush()
        for process in processes:
            _, error = process.communicate(timeout=30)
            assert process.returncode == 0, error
        events = event_log(tmp_path).events("j_parallel")

        assert [event.event_id for event in events] == list(range(1, 61))
        assert {(event.payload["writer"], event.payload["index"]) for event in events} == {
            (name, index) for name in ("api", "worker") for index in range(30)
        }
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=5)


@pytest.mark.parametrize("bad_history", ["invalid-json\n", "duplicate", "other-job", "unfinished"])
def test_bad_history_is_isolated_and_never_silently_rewritten(
    tmp_path: Path, bad_history: str
) -> None:
    log = event_log(tmp_path)
    first = log.emit("j_bad", "job", {})
    path = tmp_path / "jobs" / "j_bad" / "events.jsonl"
    if bad_history == "duplicate":
        raw = first.model_dump_json() + "\n" + first.model_dump_json() + "\n"
    elif bad_history == "other-job":
        raw = first.model_copy(update={"job_id": "j_other"}).model_dump_json() + "\n"
    elif bad_history == "unfinished":
        raw = first.model_dump_json()
    else:
        raw = bad_history
    path.write_text(raw, encoding="utf-8")

    with pytest.raises(EngineError) as caught:
        log.events("j_bad")
    assert caught.value.code == "E_CHECKPOINT_MISSING"
    with pytest.raises(EngineError):
        log.emit("j_bad", "log", {})
    assert path.read_text(encoding="utf-8") == raw
    assert log.emit("j_healthy", "job", {}).event_id == 1
    assert log.last_event_id("j_healthy") == 1


def test_startup_restores_healthy_logs_and_isolates_a_broken_job(tmp_path: Path) -> None:
    state = build_state(
        settings=ServerSettings(_env_file=None, storage_root=tmp_path),
        repository=MemoryRepository(),
        stages=StageRegistry(),
    )
    state.runner.events.emit("j_broken", "job", {})
    state.runner.events.emit("j_good", "job", {})
    (tmp_path / "jobs" / "j_broken" / "events.jsonl").write_text("bad\n", encoding="utf-8")

    assert state.runner.restore_events() == 1
    assert state.runner.events.emit("j_good", "log", {}).event_id == 2
