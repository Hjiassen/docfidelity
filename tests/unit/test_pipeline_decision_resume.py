"""历史决策保留审计，实际阶段重跑只保留当前待办。"""

from pathlib import Path

import pytest

from docfidelity.pipeline import (
    EventLog,
    MemoryCheckpointStore,
    StageOutcome,
    StageRegistry,
    Workspace,
    build_session,
    pending_decisions_from_events,
    run_stage,
)
from tests.fixtures.pipeline_fixture import document_job

pytestmark = pytest.mark.unit


def test_rerunning_a_stage_supersedes_its_pending_decisions(tmp_path: Path) -> None:
    job = document_job()
    events = EventLog()
    session = build_session(job_id=job.job_id, events=events, model_enabled=False)
    registry = StageRegistry()
    registry.register(
        "compose",
        lambda ctx: (ctx.decide("placement.section_slot", target="old"), StageOutcome())[1],
    )
    args = dict(
        job=job,
        stage="compose",
        registry=registry,
        store=MemoryCheckpointStore(),
        workspace=Workspace(tmp_path, job.job_id),
        inputs_hash=job.inputs_hash,
    )
    run_stage(**args, decisions=session)
    assert len(session.pending_decisions()) == 1
    registry.register("compose", lambda ctx: StageOutcome(), replace=True)
    args["inputs_hash"] = "changed"
    resumed = build_session(job_id=job.job_id, events=events, model_enabled=False)
    run_stage(**args, decisions=resumed)
    assert len(resumed.decisions()) == 1
    assert resumed.pending_decisions() == ()
    assert pending_decisions_from_events(events.events(job.job_id), job_id=job.job_id) == ()


def test_resetting_one_stage_keeps_other_stage_and_legacy_pending() -> None:
    events = EventLog()
    session = build_session(job_id="j1", events=events, model_enabled=False)
    session.decide("placement.section_slot", target="unscoped")
    events.emit_stage("j1", "qa", "running")
    session.decide("placement.section_slot", target="legacy-qa")
    resumed = build_session(job_id="j1", events=events, model_enabled=False)
    resumed.begin_stage("compose")
    resumed.decide("placement.section_slot", target="compose")
    resumed.begin_stage("qa")
    assert {item.target for item in resumed.pending_decisions()} == {"unscoped", "compose"}
    assert {item.target for item in pending_decisions_from_events(events.events("j1"))} == {
        "unscoped",
        "compose",
    }
