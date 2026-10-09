"""服务总开关开启时，项目路由仍必须能明确关闭模型。"""

from __future__ import annotations

import pytest

from docfidelity.agents.sdk_adapter import ScriptedAdapter
from docfidelity.contracts.config import RoutingConfig
from docfidelity.pipeline import EventLog, model_session_factory
from docfidelity.pipeline import decisions as decision_module
from tests.fixtures.pipeline_fixture import document_job

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("with_baseline", [False, True])
def test_project_model_switch_prevents_requests_and_baseline_pending(
    monkeypatch: pytest.MonkeyPatch, with_baseline: bool
) -> None:
    adapter = ScriptedAdapter(script={})
    monkeypatch.setattr(decision_module, "OpenAIAgentsAdapter", lambda **_kwargs: adapter)
    factory = model_session_factory(
        model="test-model",
        base_url="http://127.0.0.1:9/v1",
        baseline={"placement.section_slot": 1.0} if with_baseline else None,
    )
    session = factory(document_job(), RoutingConfig(model_enabled=False), EventLog())
    routing = {record["id"]: record for record in session.routing()}
    assert routing["placement.section_slot"]["effective"] == "rule"
    outcome = session.decide("placement.section_slot", {}, candidates=["s1"])
    assert adapter.calls == []
    assert not session.table.route("placement.section_slot").baseline_pending
    assert "模型总开关关闭" in outcome.reason
