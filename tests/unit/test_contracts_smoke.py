"""契约层模型自检。

覆盖四类不变量：

1. 全部契约模型可生成 JSON Schema（前端表单校验与对外交付都依赖它）
2. 关键模型的构造 / 序列化 / 反序列化往返
3. IR 硬约束：序列化结果中不得出现样式痕迹
4. 状态迁移表与阶段版本表的自洽性
"""

from __future__ import annotations

import importlib
import json
from typing import get_args

import pytest
from pydantic import BaseModel, ValidationError

from docfidelity.contracts.base import FormatModel
from docfidelity.contracts.ir import FORBIDDEN_IR_TOKENS, ContentIR, ContentUnit, OriginRef
from docfidelity.contracts.job import (
    JOB_TRANSITIONS,
    STAGE_IMPL_VERSION,
    STAGE_NAMES,
    Job,
    JobStatus,
    StageName,
)
from docfidelity.contracts.qa import Finding, Location, Report, RuleSeverity

pytestmark = pytest.mark.unit

_CONTRACT_MODULES = (
    "docfidelity.contracts.template",
    "docfidelity.contracts.ir",
    "docfidelity.contracts.domain",
    "docfidelity.contracts.profile",
    "docfidelity.contracts.fact",
    "docfidelity.contracts.job",
    "docfidelity.contracts.qa",
    "docfidelity.contracts.config",
)


def _collect_contract_models() -> dict[str, type[BaseModel]]:
    found: dict[str, type[BaseModel]] = {}
    for module_path in _CONTRACT_MODULES:
        module = importlib.import_module(module_path)
        for obj in vars(module).values():
            if (
                isinstance(obj, type)
                and issubclass(obj, FormatModel)
                and obj is not FormatModel
                and obj.__module__ == module.__name__
            ):
                found[f"{obj.__module__.rsplit('.', 1)[-1]}.{obj.__name__}"] = obj
    return found


CONTRACT_MODELS = _collect_contract_models()


def _sample_ir() -> ContentIR:
    return ContentIR(
        project_id="p_demo",
        doc_key="detail",
        profile_id="demo_profile",
        template_ref="TPL-0001@1.0",
        units=[
            ContentUnit(unit_id="ch#1", type="heading", level=1, text="概述"),
            ContentUnit(
                unit_id="ch#2",
                type="para",
                text="正文内容",
                trace=["demo:1/2"],
                origin=OriginRef(source="demo-source", locator="row-3"),
            ),
            ContentUnit(
                unit_id="ch#3",
                type="table",
                fields={"columns": ["A", "B"], "rows": [["1", "2"]]},
                children=[],
            ),
        ],
    )


class TestModelInventory:
    def test_contract_models_collected(self) -> None:
        # 契约层应有足够规模的模型；数量骤降通常意味着模块被误改
        assert len(CONTRACT_MODELS) >= 40, sorted(CONTRACT_MODELS)

    @pytest.mark.parametrize("name", sorted(CONTRACT_MODELS))
    def test_json_schema_generates(self, name: str) -> None:
        schema = CONTRACT_MODELS[name].model_json_schema()
        # 自引用模型（ContentUnit / ChapterNode / FieldSchema / ProjectionSpec）的顶层
        # 是 $ref + $defs，其余模型是内联 object。两种形态都必须能解析到 properties。
        if "$ref" in schema:
            ref = schema["$ref"]
            assert ref.startswith("#/$defs/"), ref
            target = schema.get("$defs", {}).get(ref.rsplit("/", 1)[-1])
            assert target is not None, f"{name} 的 $ref 未在 $defs 中找到"
            assert target.get("type") == "object"
            assert "properties" in target
        else:
            assert schema.get("type") == "object"
            assert "properties" in schema


class TestIrPurity:
    def test_ir_dump_has_no_style_tokens(self) -> None:
        payload = json.dumps(_sample_ir().model_dump(), ensure_ascii=False)
        for token in FORBIDDEN_IR_TOKENS:
            assert token not in payload, f"IR 中出现了样式痕迹 {token!r}（决策一）"

    def test_ir_roundtrip(self) -> None:
        ir = _sample_ir()
        restored = ContentIR.model_validate(ir.model_dump())
        assert restored.doc_key == ir.doc_key
        assert [unit.unit_id for unit in restored.walk()] == [unit.unit_id for unit in ir.walk()]

    def test_unit_ids_are_unique_and_walkable(self) -> None:
        ir = _sample_ir()
        assert ir.duplicate_unit_ids() == []
        assert len(list(ir.walk())) == 3

    def test_find_returns_unit(self) -> None:
        ir = _sample_ir()
        found = ir.find("ch#2")
        assert found is not None
        assert found.text == "正文内容"

    def test_blank_unit_id_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ContentUnit(unit_id="   ", type="para")


class TestStateMachines:
    def test_transition_table_covers_all_statuses(self) -> None:
        assert set(JOB_TRANSITIONS) == set(get_args(JobStatus))

    @pytest.mark.parametrize("terminal", ["succeeded"])
    def test_terminal_states_are_final(self, terminal: str) -> None:
        assert JOB_TRANSITIONS[terminal] == frozenset()

    def test_failed_and_canceled_are_resumable(self) -> None:
        # DD-11.2 列了 failed -> running（retry_stage / resume_from）与 failed -> queued
        # （restart）；DD-11.4 的 rerun_failed 把 failed 与 canceled 一并重置。
        # 失败/取消若是终态，FR-M7-03 / FR-M7-16 的"重试 / 从失败阶段续跑 / 只重跑失败子作业"
        # 在契约层就不可表达。
        for status in ("failed", "canceled"):
            assert {"running", "queued"} <= JOB_TRANSITIONS[status]

    def test_partial_failure_can_be_canceled(self) -> None:
        assert JOB_TRANSITIONS["partial_failure"] == frozenset({"canceled"})

    def test_queued_can_only_start_or_end(self) -> None:
        assert JOB_TRANSITIONS["queued"] == frozenset({"running", "canceled", "failed"})

    def test_running_can_reach_every_outcome(self) -> None:
        assert {"succeeded", "failed", "canceled", "awaiting_decision"} <= JOB_TRANSITIONS[
            "running"
        ]

    def test_job_can_transition_helper(self) -> None:
        job = Job(
            job_id="j1",
            kind="document",
            project_id="p1",
            created_at="2026-09-20T10:00:00Z",  # type: ignore[arg-type]
            inputs_hash="h",
        )
        assert job.can_transition_to("running")
        assert not job.can_transition_to("succeeded")

    def test_stage_impl_version_covers_all_stages(self) -> None:
        assert set(STAGE_IMPL_VERSION) == set(STAGE_NAMES)
        assert set(STAGE_NAMES) == set(get_args(StageName))

    def test_stage_versions_are_positive(self) -> None:
        assert all(value >= 1 for value in STAGE_IMPL_VERSION.values())


class TestReportSemantics:
    def test_report_missing_is_distinguishable_from_pass(self) -> None:
        # 「门禁未执行」= 没有 Report 对象；「全通过」= Report 存在且 verdict=pass
        report = Report(report_id="r1", job_id="j1", generated_at="2026-09-20T10:00:00Z")  # type: ignore[arg-type]
        assert report.verdict == "pass"
        assert report.findings == []

    def test_waived_findings_are_not_counted(self) -> None:
        report = Report(
            report_id="r1",
            job_id="j1",
            generated_at="2026-09-20T10:00:00Z",  # type: ignore[arg-type]
            findings=[
                Finding(
                    rule_id="FR-M5-01",
                    severity="blocker",
                    message="样式不合规",
                    location=Location(part="word/styles.xml"),
                    status="waived",
                ),
                Finding(rule_id="FR-M5-03", severity="minor", message="表头不规范"),
            ],
        )
        assert report.counts_by_severity() == {"blocker": 0, "major": 0, "minor": 1}

    def test_severity_values(self) -> None:
        assert set(get_args(RuleSeverity)) == {"blocker", "major", "minor"}
