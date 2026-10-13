"""P13 模型设置必须随提交快照传给独立 worker，密钥仍留在服务端。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from docfidelity.agents import AdapterOutcome, ScriptedAdapter
from docfidelity.contracts.config import RoutingConfig
from docfidelity.contracts.job import Job
from docfidelity.pipeline import DecisionSession, EventLog, build_session
from server.api.deps import build_state
from server.main import APP_STATE_ATTRIBUTE, create_app
from server.settings import ServerSettings
from server.storage import SqlAlchemyRepository
from server.storage.engine import create_engine_for, create_schema
from tests.fixtures.server_fixture import build_harness, build_registry

pytestmark = pytest.mark.api


@pytest.mark.parametrize(
    ("initial_enabled", "patch", "expected"),
    [
        (True, {"model_enabled": False}, None),
        (False, {"model_enabled": True}, "updated"),
        (
            True,
            {"model_name": "replacement", "model_base_url": "http://new.invalid/v1"},
            "replacement",
        ),
    ],
)
def test_api_settings_update_reaches_a_separate_worker_through_the_submitted_job(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    initial_enabled: bool,
    patch: dict[str, Any],
    expected: str | None,
) -> None:
    url = f"sqlite:///{(tmp_path / 'shared.db').as_posix()}"
    api_engine = create_engine_for(url)
    worker_engine = create_engine_for(url)
    create_schema(api_engine)
    api_repository = SqlAlchemyRepository(api_engine)
    worker_repository = SqlAlchemyRepository(worker_engine)

    def offline_factory(**kwargs: Any) -> Any:
        model = kwargs["model"]
        chosen = "worker-local" if kwargs["api_key"] == "WORKER_SECRET" else "unexpected-key"
        adapter = ScriptedAdapter(
            model_name=model,
            script={
                "placement.section_slot": AdapterOutcome(
                    ok=True,
                    confidence=0.99,
                    model=model,
                    chosen=chosen,
                    payload={"unit_ref": "worker-unit", "chapter_path": [chosen]},
                )
            },
        )

        def factory(job: Job, routing: RoutingConfig | None, events: EventLog) -> DecisionSession:
            return build_session(
                routing, job_id=job.job_id, events=events, adapter=adapter, model_enabled=True
            )

        return factory

    monkeypatch.setattr("server.api.deps.model_session_factory", offline_factory)
    settings = ServerSettings(
        _env_file=None,
        storage_root=tmp_path,
        model_enabled=initial_enabled,
        model_name="updated",
        model_base_url="http://old.invalid/v1",
        model_api_key=SecretStr("API_SECRET"),
    )
    seeded = build_harness(tmp_path, repository=api_repository)
    project_id = seeded.new_project()
    app = create_app(
        settings=settings, repository=api_repository, stages=build_registry(), start_worker=False
    )
    # 两个实例的设置与仓储连接均独立，只有数据库和工作区共享。
    worker = build_state(
        settings=ServerSettings(
            _env_file=None,
            storage_root=tmp_path,
            model_enabled=False,
            model_api_key=SecretStr("WORKER_SECRET"),
        ),
        repository=worker_repository,
        stages=build_registry(),
    )
    try:
        with TestClient(app) as client:
            assert client.put("/api/v1/settings", json={"patch": patch}).status_code == 200
            submitted = client.post(
                f"/api/v1/projects/{project_id}/jobs", json={"doc_keys": ["overview"]}
            )
            assert submitted.status_code == 201, submitted.text
            child_id = submitted.json()["child_job_ids"][0]
            child = worker.repository.get_job(child_id)

            session = worker.runner.session_for(child)

            described = session.adapter_description()
            assert described["model"] == expected
            assert described["available"] is (expected is not None)
            decision = session.decide(
                "placement.section_slot",
                {"unit_ref": "worker-unit", "chapters": ["worker-local"]},
                target="worker-unit",
                candidates=["worker-local"],
            )
            if expected is not None:
                assert decision.source == "model" and decision.chosen == "worker-local"
            else:
                assert decision.source == "rule_default"
            snapshot = list((tmp_path / "jobs" / child_id / "inputs").glob("*.json"))
            assert snapshot, "作业必须携带持久化提交快照"
            raw = "".join(path.read_text(encoding="utf-8") for path in snapshot)
            assert "API_SECRET" not in raw and "WORKER_SECRET" not in raw
            state = getattr(app.state, APP_STATE_ATTRIBUTE)
            state.services.update_settings({"model_enabled": not bool(expected)})
            assert worker.runner.session_for(child).adapter_description()["model"] == expected
    finally:
        api_engine.dispose()
        worker_engine.dispose()
