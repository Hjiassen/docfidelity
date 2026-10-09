"""提交时输入冻结：排队、阶段推进与续跑共用同一份声明。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from docfidelity.pipeline import StageContext, StageOutcome
from server.api.deps import build_state
from server.storage import SqlAlchemyRepository
from server.storage.engine import create_engine_for, create_schema
from tests.fixtures.server_fixture import build_harness, build_registry

pytestmark = pytest.mark.api


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
def test_persisted_config_body_uses_the_repository_assigned_version(
    tmp_path: Path, backend: str
) -> None:
    engine = create_engine_for(f"sqlite:///{(tmp_path / 'config.db').as_posix()}")
    create_schema(engine)
    harness = build_harness(
        tmp_path, repository=(SqlAlchemyRepository(engine) if backend == "sqlite" else None)
    )
    try:
        project_id = harness.new_project()
        original = harness.services.get_config(project_id)
        stored = harness.services.put_config(
            project_id, original.model_copy(update={"version": 999}), author="second"
        )

        assert stored.version == 2
        assert stored.config.version == 2
        assert harness.services.get_config(project_id).version == 2
        assert harness.services.config_version(project_id, 2).version == 2
        assert harness.services.config_version(project_id, 1).version == 1
    finally:
        engine.dispose()


def test_queued_job_keeps_the_config_used_to_compute_its_inputs_hash(tmp_path: Path) -> None:
    harness = build_harness(tmp_path)
    project_id = harness.new_project()
    parent = harness.services.submit_build(project_id, doc_keys=["overview"])
    child = harness.services.job_children(parent.job_id)[0]
    config = harness.services.get_config(project_id)
    changed = config.model_copy(
        update={
            "documents": [
                entry.model_copy(update={"template_ref": "changed@2"}) for entry in config.documents
            ]
        }
    )
    harness.services.put_config(project_id, changed, author="changed")

    harness.runner.run_once()

    payload = harness.stages["render"].calls[0].payload
    assert payload["config_version"] == 1
    assert payload["template_ref"] == "demo-package@1"
    assert payload["inputs_hash"] == child.inputs_hash


def test_config_update_between_stages_does_not_change_the_submitted_payload(tmp_path: Path) -> None:
    harness = build_harness(tmp_path)
    project_id = harness.new_project()

    def change_config(context: StageContext) -> StageOutcome:
        config = harness.services.get_config(project_id)
        changed = config.model_copy(
            update={
                "documents": [
                    entry.model_copy(update={"template_ref": "mid-build@2"})
                    for entry in config.documents
                ]
            }
        )
        harness.services.put_config(project_id, changed, author="mid-build")
        return StageOutcome(outputs={"ingest": context.workspace.put_text("work/in.txt", "ok")})

    harness.runner.registry.register("ingest", change_config, replace=True)
    harness.services.submit_build(project_id, doc_keys=["overview"])
    harness.runner.run_once()

    assert harness.stages["compose"].calls[0].payload["config_version"] == 1
    assert harness.stages["compose"].calls[0].payload["template_ref"] == "demo-package@1"


@pytest.mark.parametrize("action", ["retry", "resume"])
def test_restarted_worker_reuses_the_original_payload_on_retry(tmp_path: Path, action: str) -> None:
    first = build_harness(tmp_path, stages=build_registry(render={"failures": 1}))
    project_id = first.new_project()
    parent = first.services.submit_build(project_id, doc_keys=["overview"])
    first.runner.run_once()
    child = first.services.job_children(parent.job_id)[0]
    config = first.services.get_config(project_id)
    changed = config.model_copy(
        update={
            "documents": [
                entry.model_copy(update={"template_ref": "after-failure@2"})
                for entry in config.documents
            ]
        }
    )
    first.services.put_config(project_id, changed, author="after-failure")
    restarted = build_harness(tmp_path, repository=first.repository)
    if action == "retry":
        restarted.services.retry(child.job_id, mode="full")
    else:
        restarted.services.resume(child.job_id)

    restarted.runner.run_once()

    assert restarted.stages["render"].calls[0].payload["config_version"] == 1
    assert restarted.stages["render"].calls[0].payload["template_ref"] == "demo-package@1"


@pytest.mark.parametrize(
    "corruption",
    [
        {"routing": "broken"},
        {"model": {}},
        {"model": {"enabled": "false", "base_url": "http://model.invalid/v1", "name": "m"}},
    ],
)
def test_a_valid_json_snapshot_with_invalid_fields_fails_the_job_instead_of_stalling(
    tmp_path: Path, corruption: dict[str, Any]
) -> None:
    harness = build_harness(tmp_path)
    parent = harness.services.submit_build(harness.new_project(), doc_keys=["overview"])
    child = harness.services.job_children(parent.job_id)[0]
    workspace = harness.runner.workspace(child.job_id)
    payload = json.loads(workspace.read_text("inputs/build_payload.json"))
    workspace.put_json("inputs/build_payload.json", {**payload, **corruption})
    restarted = build_state(
        settings=harness.settings, repository=harness.repository, stages=harness.runner.registry
    )

    outcome = restarted.runner.run_once()

    assert outcome is not None and outcome.status == "failed"
    failed = restarted.repository.get_job(child.job_id)
    assert failed.error is not None and failed.error.code == "E_CONFIG_INVALID"
