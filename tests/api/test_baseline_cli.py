"""部署侧评测入口：项目单点姿态须能产出生产准入接受的报告。"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
from scripts import run_baseline as cli

from docfidelity.agents.baseline import default_cases, endpoint_digest, load_status
from docfidelity.agents.routing import AdapterOutcome
from docfidelity.contracts.config import DecisionPointRoute, RoutingConfig
from docfidelity.pipeline import EventLog, model_session_factory
from server import settings as settings_module
from tests.fixtures.pipeline_fixture import document_job

pytestmark = pytest.mark.api
POINT = "placement.section_slot"


@pytest.fixture(autouse=True)
def isolated_deployment_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """使用临时部署目录，隔离本机配置与环境变量。"""
    monkeypatch.chdir(tmp_path)
    for name in tuple(os.environ):
        if name.startswith(settings_module.ENV_PREFIX):
            monkeypatch.delenv(name)


class CaseAdapter:
    """只替代外部推理；样本评测、指纹计算、报告落盘与生产准入均走真实实现。"""

    def __init__(self) -> None:
        self.payloads = {case.target: dict(case.expect) for case in default_cases()}

    def available(self) -> bool:
        return True

    def call(self, request: Any, output_type: Any) -> AdapterOutcome:
        return AdapterOutcome(
            ok=True,
            schema_ok=True,
            confidence=None if request.decision_point == POINT else 1.0,
            model="local-test",
            payload=self.payloads[request.target],
        )


@pytest.mark.parametrize("project_wrapper", [False, True])
def test_cli_report_for_a_single_point_posture_is_admitted_in_production(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, project_wrapper: bool
) -> None:
    routing = RoutingConfig(
        decision_points={POINT: DecisionPointRoute(admission_policy="independent_validation")}
    )
    payload = routing.model_dump(mode="json")
    if project_wrapper:
        payload = {"project_id": "p-test", "routing": payload}
    source = tmp_path / "routing.json"
    source.write_text(json.dumps(payload), encoding="utf-8")
    report_path = tmp_path / "baseline.json"
    monkeypatch.setattr(cli, "_build_adapter", lambda *_args: CaseAdapter())
    assert (
        cli.main(
            [
                "--model",
                "local-test",
                "--base-url",
                "http://127.0.0.1:9/v1",
                "--routing",
                str(source),
                "--out",
                str(report_path),
            ]
        )
        == 0
    )
    report = load_status(report_path)
    assert report.by_point[POINT] == 1.0
    assert report.endpoint_digest == endpoint_digest("http://127.0.0.1:9/v1")
    factory = model_session_factory(
        model="local-test", base_url="http://127.0.0.1:9/v1", baseline_path=report_path
    )
    session = factory(document_job(), routing, EventLog())
    records = {record["id"]: record for record in session.routing()}
    assert records[POINT]["effective"] == "small_model"
    assert records["dedup.semantic_duplicate"]["confidence_threshold"] == 0.7
    assert records["dedup.semantic_duplicate"]["effective"] == "small_model"


def test_cli_default_report_uses_the_same_dotenv_storage_root_as_the_service(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".env").write_text(
        "DOCFIDELITY_STORAGE_ROOT=configured-storage\n"
        "DOCFIDELITY_MODEL_NAME=local-test\n"
        "DOCFIDELITY_MODEL_BASE_URL=http://127.0.0.1:9/v1\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(cli, "_build_adapter", lambda *_args: CaseAdapter())

    routing_file = tmp_path / "routing.json"
    routing_file.write_text(
        json.dumps({"decision_points": {POINT: {"admission_policy": "independent_validation"}}}),
        encoding="utf-8",
    )
    assert cli.main(["--routing", str(routing_file)]) == 0

    report_path = tmp_path / "configured-storage" / "baseline.json"
    report = load_status(report_path)
    assert report.model == "local-test"
    assert report.by_point[POINT] == 1.0
    assert not (tmp_path / "var" / "baseline.json").exists()


def test_cli_output_uses_endpoint_fingerprint_and_reports_actual_usage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "_build_adapter", lambda *_args: CaseAdapter())
    url = "http://127.0.0.1:9/v1"
    assert cli.main(["--model", "local-test", "--base-url", url, "--no-save"]) == 0
    captured = capsys.readouterr().out
    assert url not in captured
    assert endpoint_digest(url) in captured
    assert "token" in captured
    assert "缺失置信度" in captured


def test_zero_threshold_report_cannot_admit_unscored_placement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "_build_adapter", lambda *_args: CaseAdapter())
    report_path = tmp_path / "baseline.json"
    assert (
        cli.main(
            [
                "--model",
                "local-test",
                "--base-url",
                "http://127.0.0.1:9/v1",
                "--confidence-threshold",
                "0",
                "--out",
                str(report_path),
            ]
        )
        == 0
    )
    assert load_status(report_path).by_point[POINT] == 0.0


@pytest.mark.parametrize(
    "content",
    [
        "{broken",
        "[]",
        '{"routing":null}',
        '{"decision_points":42}',
        '{"decision_points":{"unknown.point":{"route":"small_model"}}}',
        '{"decision_points":{"gate.judge":{"route":"small_model"}}}',
    ],
)
def test_bad_routing_input_is_rejected_before_adapter_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, content: str
) -> None:
    source = tmp_path / "bad-routing.json"
    source.write_text(content, encoding="utf-8")

    def unexpected_adapter(*_args: Any) -> Any:
        pytest.fail("无效路由配置不得开始模型评测")

    monkeypatch.setattr(cli, "_build_adapter", unexpected_adapter)
    with pytest.raises(SystemExit, match="路由配置"):
        cli.main(["--routing", str(source), "--out", str(tmp_path / "baseline.json")])
    assert not (tmp_path / "baseline.json").exists()


def test_routing_file_and_global_threshold_cannot_be_combined(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as caught:
        cli._parse_args(
            ["--routing", str(tmp_path / "routing.json"), "--confidence-threshold", "0"]
        )
    assert caught.value.code == 2
