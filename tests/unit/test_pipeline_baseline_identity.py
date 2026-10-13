"""基线不能跨端点复用，也不能使用缺失部署身份的旧报告。"""

from pathlib import Path

import pytest

from docfidelity.agents import (
    BaselineReport,
    build_routing,
    default_cases,
    endpoint_digest,
    save_report,
)
from docfidelity.agents.baseline import baseline_digests
from docfidelity.pipeline import EventLog, model_session_factory
from tests.fixtures.pipeline_fixture import document_job

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("fingerprint", ["", "different-deployment", "matching"])
def test_baseline_endpoint_identity_must_match(tmp_path: Path, fingerprint: str) -> None:
    url = "http://127.0.0.1:9/v1"
    cases, routing = baseline_digests(default_cases(), build_routing(model_enabled=True).describe())
    path = save_report(
        BaselineReport(
            model="m-1",
            cases_digest=cases,
            routing_digest=routing,
            endpoint_digest=endpoint_digest(url) if fingerprint == "matching" else fingerprint,
            by_point={"placement.section_slot": 1.0},
        ),
        tmp_path / "baseline.json",
    )
    factory = model_session_factory(model="m-1", base_url=url, baseline_path=path)
    session = factory(document_job("j-b"), None, EventLog())
    route = session.table.route("placement.section_slot")
    assert route.effective == ("small_model" if fingerprint == "matching" else "rule")
    assert ("端点" in route.reason) == (fingerprint != "matching")
