"""模型准入必须由真实置信度或独立约束校验提供证据。"""

from __future__ import annotations

from typing import Any

import pytest

from docfidelity.agents.recorder import MemoryRecorder
from docfidelity.agents.routing import AdapterOutcome, DecisionContext, build_routing, dispatch
from docfidelity.agents.sdk_adapter import ScriptedAdapter
from docfidelity.contracts.config import DecisionPointRoute, RoutingConfig
from docfidelity.contracts.errors import EngineError

pytestmark = pytest.mark.unit
POINT = "placement.section_slot"


def _context(policy: str = "require_confidence", **answer: Any) -> DecisionContext:
    route = DecisionPointRoute.load({"confidence_threshold": 0.0, "admission_policy": policy})
    outcome = {
        "ok": True,
        "schema_ok": True,
        "confidence": None,
        "payload": {"unit_ref": "u1", "chapter_path": ["甲", "乙"]},
    }
    outcome.update(answer)
    return DecisionContext(
        table=build_routing(RoutingConfig(decision_points={POINT: route})),
        adapter=ScriptedAdapter(script={POINT: AdapterOutcome(**outcome)}),
        recorder=MemoryRecorder(job_id="j1"),
        job_id="j1",
        max_retries=0,
    )


def _dispatch(ctx: DecisionContext, **kwargs: Any) -> Any:
    args = {
        "target": "u1",
        "candidates": ["候选一", "候选二"],
        "context": {"placement_constraints": {"候选一": ["甲", "乙"], "候选二": ["甲", "丙"]}},
    }
    args.update(kwargs)
    return dispatch(POINT, {"unit_ref": "u1", "summary": "内容"}, ctx, **args)


def test_missing_confidence_at_zero_threshold_still_requires_confirmation() -> None:
    ctx = _context()
    result = _dispatch(ctx)
    assert result.source == "fallback"
    assert result.pending is True
    assert result.confidence is None
    assert ctx.recorder.decisions[0].confidence is None


def test_independent_placement_admission_keeps_missing_confidence_and_evidence() -> None:
    ctx = _context("independent_validation")
    result = _dispatch(ctx)
    assert result.source == "model"
    assert result.status == "auto"
    assert result.chosen == "候选一"
    assert result.confidence is None
    assert result.admission_evidence["policy"] == "independent_validation"
    assert result.admission_evidence["accepted"] is True
    assert result.admission_evidence["validator"] == "placement_constraints_v1"
    assert result.admission_evidence["constraints_digest"]
    assert ctx.recorder.decisions[0].admission_evidence == result.admission_evidence


@pytest.mark.parametrize(
    "payload",
    [
        {"unit_ref": "other", "chapter_path": ["甲", "乙"]},
        {"unit_ref": "u1", "chapter_path": ["乙"]},
        {"unit_ref": "u1", "chapter_path": ["甲", "新章节"]},
        {"unit_ref": "u1", "chapter_path": []},
        {"unit_ref": "u1", "chapter_path": ["甲", "乙"], "slot_id": "invented"},
        {"chapter_path": ["甲", "乙"]},
    ],
)
def test_independent_admission_rejects_invalid_reference_path_or_schema(
    payload: dict[str, Any],
) -> None:
    ctx = _context("independent_validation", payload=payload)
    result = _dispatch(ctx)
    assert result.source == "fallback"
    assert result.pending is True
    assert result.admission_evidence["accepted"] is False


@pytest.mark.parametrize("constraints", [None, {}, {"候选一": ["其他"]}, {"候选一": "甲"}])
def test_independent_admission_requires_frozen_compatible_constraints(constraints: Any) -> None:
    ctx = _context("independent_validation")
    result = _dispatch(
        ctx, context={} if constraints is None else {"placement_constraints": constraints}
    )
    assert result.source == "fallback"
    assert result.pending is True


def test_frozen_constraint_cannot_admit_a_path_outside_supplied_candidates() -> None:
    ctx = _context("independent_validation")
    assert _dispatch(ctx, candidates=["候选二"]).source == "fallback"


def test_placement_without_candidates_falls_back_before_model_call() -> None:
    ctx = _context("independent_validation")
    result = _dispatch(ctx, candidates=[])
    assert result.source == "fallback"
    assert result.pending is True
    assert result.attempts == 0
    assert ctx.recorder.model_calls == []
    assert ctx.adapter.calls == []


def test_confidence_cannot_bypass_placement_reference_integrity() -> None:
    ctx = _context(confidence=1.0, payload={"unit_ref": "other", "chapter_path": ["甲", "乙"]})
    assert _dispatch(ctx).source == "fallback"


def test_frozen_constraint_does_not_override_an_explicit_chapter_path_list() -> None:
    ctx = _context("independent_validation")
    result = _dispatch(
        ctx,
        context={
            "placement_constraints": {"候选一": ["甲", "乙"], "候选二": ["甲", "丙"]},
            "chapter_paths": [["甲", "丙"]],
        },
    )
    assert result.source == "fallback"


def test_confidence_placement_validates_exact_supplied_chapter_paths() -> None:
    ctx = _context(confidence=1.0)
    result = _dispatch(ctx, candidates=["甲 / 乙"], context={"chapter_paths": [["甲", "乙"]]})
    assert result.source == "model"
    assert result.chosen == "甲 / 乙"


def test_confidence_placement_rejects_a_path_only_containing_known_titles() -> None:
    ctx = _context(confidence=1.0)
    result = _dispatch(ctx, candidates=["甲", "乙"], context={"chapter_paths": [["甲"], ["乙"]]})
    assert result.source == "fallback"


def test_independent_policy_is_rejected_for_unsupported_points() -> None:
    route = DecisionPointRoute.load({"admission_policy": "independent_validation"})
    with pytest.raises(EngineError, match="独立校验"):
        build_routing(RoutingConfig(decision_points={"dedup.semantic_duplicate": route}))


def test_admission_policy_is_part_of_the_resolved_posture() -> None:
    ctx = _context("independent_validation")
    record = next(row for row in ctx.table.describe() if row["id"] == POINT)
    assert record["admission_policy"] == "independent_validation"


def test_retry_token_totals_include_all_attempts() -> None:
    ctx = _context(tokens_in=9, tokens_out=4)
    ctx.max_retries = 2
    result = _dispatch(ctx)
    assert result.tokens_in == 27
    assert result.tokens_out == 12
    assert result.missing_confidence_count == 3
    assert ctx.recorder.token_totals() == (27, 12)
    assert ctx.recorder.model_calls[0].attempts == 3
