"""默认阶段的语义落位、降级和续跑证据。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from docfidelity.agents import AdapterOutcome, ScriptedAdapter
from docfidelity.agents.routing import DecisionRequest
from docfidelity.builder import build_registry
from docfidelity.builder.inputs import FACTS_REL, IR_REL
from docfidelity.contracts.base import FormatModel
from docfidelity.contracts.ir import ContentIR, ContentUnit
from docfidelity.contracts.job import StageCheckpoint
from docfidelity.contracts.profile import ProjectionSpec
from docfidelity.pipeline import (
    EventLog,
    FileCheckpointStore,
    Workspace,
    build_session,
    run_stage,
)
from docfidelity.pipeline.checkpoint import decide_reuse
from tests.fixtures.compose_fixture import api_models, api_profile, api_snapshot
from tests.fixtures.pipeline_fixture import document_job

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("stage,version", [("compose", 2), ("render", 4), ("qa", 2)])
def test_old_build_checkpoints_cannot_hide_new_semantic_ir(stage: str, version: int) -> None:
    checkpoint = StageCheckpoint(stage=stage, version=version, inputs_hash="old-input")
    assert decide_reuse(checkpoint, stage=stage, inputs_hash="old-input").must_run


class PlacementAdapter(ScriptedAdapter):
    def call(
        self, request: DecisionRequest, output_type: type[FormatModel] | None
    ) -> AdapterOutcome:
        self.calls.append(request)
        return AdapterOutcome(
            ok=True,
            schema_ok=True,
            confidence=0.95,
            chosen=request.candidates[-1],
            payload={"unit_ref": request.target, "chapter_path": [request.candidates[-1]]},
        )


def unbound_payload() -> dict[str, Any]:
    profile = api_profile()
    profile.projections = [profile.projections[0].model_copy(update={"slot": None})]
    first = profile.genre.chapter_skeleton[0]
    first.slots[0].required = False
    second = first.model_copy(deep=True)
    second.title = "补充说明"
    second.slots[0].slot_id = "extra"
    profile.genre.chapter_skeleton = [first, second]
    return {
        "project_id": "demo",
        "doc_key": "detail",
        "template_ref": "TPL-API@1.0",
        "source_root": ".",
        "sources": [],
        "domain_models": [model.model_dump(mode="json") for model in api_models()],
        "profile": profile.model_dump(mode="json"),
        "office_enabled": False,
    }


def prepare_workspace(tmp_path: Path) -> Workspace:
    workspace = Workspace(tmp_path, document_job().job_id)
    snapshot = api_snapshot()
    snapshot.merged = snapshot.merged[:1]
    workspace.put_json(FACTS_REL, snapshot.model_dump(mode="json"))
    return workspace


@pytest.mark.parametrize("mode", ["admitted", "rejected", "disabled", "failed"])
def test_default_compose_consumes_decisions_and_persists_evidence(
    tmp_path: Path, mode: str
) -> None:
    job = document_job()
    workspace = prepare_workspace(tmp_path / "job")
    adapter = PlacementAdapter() if mode != "failed" else ScriptedAdapter()
    events = EventLog()
    session = build_session(
        job_id=job.job_id,
        events=events,
        adapter=adapter,
        model_enabled=mode != "disabled",
        baseline={"placement.section_slot": 0.0 if mode == "rejected" else 1.0},
        max_retries=0,
    )
    result = run_stage(
        job=job,
        stage="compose",
        registry=build_registry(),
        store=FileCheckpointStore(tmp_path / "checkpoints"),
        workspace=workspace,
        inputs_hash=job.inputs_hash,
        payload=unbound_payload(),
        decisions=session,
    )
    assert result.status == "succeeded"
    ir = ContentIR.model_validate(workspace.read_json(IR_REL))
    destination = 1 if mode == "admitted" else 0
    assert ir.units[destination].children[0].text == "创建订单"
    assert not ir.units[1 - destination].children
    assert len(session.decisions()) == 1
    assert bool(session.pending_decisions()) == (mode != "admitted")
    assert len(adapter.calls) == (1 if mode in ("admitted", "failed") else 0)
    evidence = workspace.read_json(result.outputs["decisions"])
    assert evidence["placements"][0]["applied"] == ir.units[destination].text
    assert len(evidence["decisions"]) == 1
    assert len(evidence["model_calls"]) == len(session.model_calls())
    assert any(item.rule_id == "NFR-26" for item in result.findings) == (mode != "admitted")


def test_resume_preserves_pending_and_does_not_call_model_again(tmp_path: Path) -> None:
    job = document_job()
    events = EventLog()
    workspace = prepare_workspace(tmp_path / "job")
    store = FileCheckpointStore(tmp_path / "checkpoints")
    adapter = ScriptedAdapter()
    session = build_session(job_id=job.job_id, events=events, adapter=adapter, max_retries=0)
    args = {
        "job": job,
        "stage": "compose",
        "registry": build_registry(),
        "store": store,
        "workspace": workspace,
        "inputs_hash": job.inputs_hash,
        "payload": unbound_payload(),
    }
    first = run_stage(**args, decisions=session)
    restored = EventLog()
    restored.restore(job.job_id, events.events(job.job_id))
    resumed = build_session(job_id=job.job_id, events=restored, adapter=adapter, max_retries=0)
    second = run_stage(**args, decisions=resumed)
    assert second.reuse.reusable
    assert second.outputs == first.outputs
    assert len(adapter.calls) == 1
    assert len(resumed.pending_decisions()) == 1
    assert len(resumed.model_calls()) == 1


def test_table_placement_request_contains_columns_and_sample_rows(tmp_path: Path) -> None:
    payload = unbound_payload()
    payload["profile"]["projections"] = [
        api_profile().projections[2].model_copy(update={"slot": None}).model_dump(mode="json")
    ]
    for chapter in payload["profile"]["genre"]["chapter_skeleton"]:
        chapter["slots"][0]["unit_types"] = ["table"]
    adapter = PlacementAdapter()
    job = document_job()
    result = run_stage(
        job=job,
        stage="compose",
        registry=build_registry(),
        store=FileCheckpointStore(tmp_path / "checkpoints"),
        workspace=prepare_workspace(tmp_path / "job"),
        inputs_hash=job.inputs_hash,
        payload=payload,
        decisions=build_session(job_id=job.job_id, adapter=adapter),
    )
    assert result.status == "succeeded"
    assert "module" in adapter.calls[0].inputs["summary"]
    assert "/orders" in adapter.calls[0].inputs["summary"]


def test_non_heading_root_relocates_nested_headings(tmp_path: Path) -> None:
    payload = unbound_payload()
    child = ProjectionSpec(
        unit="section", for_each="endpoint", layout="one_section_per", fields=["summary"]
    )
    payload["profile"]["projections"][0]["children"] = [child.model_dump(mode="json")]
    payload["profile"]["genre"]["chapter_skeleton"][1]["level"] = 4
    job = document_job()
    workspace = prepare_workspace(tmp_path / "job")
    result = run_stage(
        job=job,
        stage="compose",
        registry=build_registry(),
        store=FileCheckpointStore(tmp_path / "checkpoints"),
        workspace=workspace,
        inputs_hash=job.inputs_hash,
        payload=payload,
        decisions=build_session(job_id=job.job_id, adapter=PlacementAdapter()),
    )
    assert result.status == "succeeded"
    ir = ContentIR.model_validate(workspace.read_json(IR_REL))
    assert ir.units[1].children[0].children[0].level == 6


@pytest.mark.parametrize("fields", [{"caption": "财务结构图"}, {"items": ["采购清单", "库存汇总"]}])
def test_visible_fields_are_available_without_exposing_image_paths(fields: dict[str, Any]) -> None:
    from docfidelity.builder.stages import _placement_summary

    fields = {**fields, "image_ref": "private/images/source.png"}
    summary = _placement_summary(ContentUnit(unit_id="u1", type="figure", fields=fields))
    assert summary
    assert "private" not in summary


def test_empty_semantic_content_falls_back_without_requesting_model(tmp_path: Path) -> None:
    payload = unbound_payload()
    payload["profile"]["projections"][0]["fields"] = []
    job = document_job()
    adapter = PlacementAdapter()
    session = build_session(job_id=job.job_id, adapter=adapter)
    result = run_stage(
        job=job,
        stage="compose",
        registry=build_registry(),
        store=FileCheckpointStore(tmp_path / "checkpoints"),
        workspace=prepare_workspace(tmp_path / "job"),
        inputs_hash=job.inputs_hash,
        payload=payload,
        decisions=session,
    )
    assert result.status == "succeeded"
    assert adapter.calls == []
    assert len(session.pending_decisions()) == 1
