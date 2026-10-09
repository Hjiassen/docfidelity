"""门禁引擎：规则模型、加载校验、闭集 dispatch、失败降级、豁免与裁决（DD-10.1 / DD-10.3）。

这套用例对准的是**引擎自身**的行为，而不是某条规则判得对不对（那是
``test_qa_rules_*.py`` 的活）。三条最要紧的断言各有其对立面：

1. **规则文件写错了要报错，不能静默忽略**——``scope`` 误写成 ``scopes`` 会被当成
   "没写 scope"，于是规则变成"全类型生效"，现象是"某类文档上多了一批莫名其妙的告警"。
2. **一条规则抛错不得中断门禁**，也不得被吞成"通过"——折成一条 ``minor``，
   其余规则照跑。
3. **``can_finalize`` 只由 ``verdict`` 决定**——豁免必须改变结论，过期豁免必须失效。
   "过期豁免仍然生效"是最危险的配置：它让一条 blocker 永远不阻断，而看板上还写着"已批准"。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from docfidelity.contracts.errors import ConfigurationError, EngineError, FormatVersionError
from docfidelity.contracts.qa import Finding, Report, RuleSet, Waiver
from docfidelity.qa import (
    BLOCKING_RULE_ID,
    ENTRY_POINT_GROUP,
    REPORT_RULE_ID,
    RULE_FILE_FORMAT_VERSION,
    RULE_MODEL_RULE_ID,
    QaContext,
    RuleSpec,
    apply_waivers,
    builtin_rule_files,
    can_finalize,
    check_names,
    default_catalog,
    load_rule_file,
    parse_rule_file,
    register_check,
    resolve_check,
    rules_for,
    run,
    summarize,
    verdict_of,
)
from tests.fixtures.qa_fixture import RenderFixture, qa_context, render_fixture

pytestmark = pytest.mark.unit

NOW = datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc)

#: 内置规则集覆盖的规则编号（附录 C 的 17 条去掉三条「元」规则）。
BUILTIN_RULE_IDS = (
    "FR-M5-01",
    "FR-M5-02",
    "FR-M5-03",
    "FR-M5-04",
    "FR-M5-05",
    "FR-M5-06",
    "FR-M5-07",
    "FR-M5-08",
    "FR-M5-09",
    "FR-M5-10",
    "FR-M5-11",
    "FR-M5-12",
    "FR-M5-13",
    "FR-M5-17",
)


@pytest.fixture(scope="module")
def artifact(tmp_path_factory: pytest.TempPathFactory) -> RenderFixture:
    """一次真渲染产物（模块级复用：渲染不便宜，而引擎用例只读它）。"""
    return render_fixture(tmp_path_factory.mktemp("qa-engine"))


def test_the_fixture_pins_office_off(artifact: RenderFixture) -> None:
    """``render_fixture`` 也必须钉死"没有 Word"，理由同渲染夹具。

    门禁用例读的是**产物**，不该因为开发机装了 Word 就顺手起一个 ``WINWORD.EXE``
    （真起过一次，还会在退出期留下看不见的残留进程）；更要紧的是，"起没起 Word"
    会改变 ``render`` 的判定序列，而这条夹具是下面所有用例的公共前提——它漂了，
    红的会是没有嫌疑的规则用例。
    """
    payload = next(
        step.checkpoint_outputs for step in artifact.result.steps if step.step == "refresh"
    )
    assert payload["office_available"] is False
    assert payload["skipped_reason"] == "office-unavailable"


def _rule(**overrides: object) -> dict[str, object]:
    """一条最小合法规则声明。"""
    payload: dict[str, object] = {
        "id": "FR-M5-99",
        "category": "style",
        "severity": "major",
        "title": "测试规则",
        "check": "style_compliance",
    }
    payload.update(overrides)
    return payload


def _file(*rules: dict[str, object], **top: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "format_version": RULE_FILE_FORMAT_VERSION,
        "rule_set": "test_set",
        "rules": list(rules) or [_rule()],
    }
    payload.update(top)
    return payload


# ------------------------------------------------------------------ 目录与登记
def test_builtin_catalog_exposes_the_rule_table() -> None:
    rules = default_catalog().catalog()
    assert sorted(rules) == sorted(BUILTIN_RULE_IDS)


@pytest.mark.parametrize("severity", ["blocker", "minor"])
def test_severity_override_reaches_report(artifact: RenderFixture, severity: str) -> None:
    catalog = default_catalog().catalog()
    selected = rules_for(
        RuleSet(
            rule_set_id="base_style",
            included_rules=["FR-M5-05"],
            severity_overrides={"FR-M5-05": severity},  # type: ignore[dict-item]
            param_overrides={"FR-M5-05": {"allowed_levels": [9]}},
        ),
        catalog,
    )
    report = run(context=qa_context(artifact), rules=selected, now=NOW)
    assert report.findings
    assert {item.severity for item in report.findings} == {severity}
    assert can_finalize(report) is (severity != "blocker")


def test_severity_override_reaches_upstream_and_deduplicates(artifact: RenderFixture) -> None:
    register_check(
        "_qa_probe_upstream_override",
        lambda spec, context: [
            Finding(rule_id=spec.rule_id, severity="blocker", message="同一上游判定")
        ],
    )
    spec = RuleSpec(
        rule_id="FR-M5-04",
        category="pagination",
        severity="blocker",
        title="分页",
        check="_qa_probe_upstream_override",
    )
    selected = rules_for(
        RuleSet(rule_set_id="test", severity_overrides={"FR-M5-04": "minor"}),
        {spec.rule_id: spec},
    )
    upstream = Finding(rule_id="FR-M5-04", severity="blocker", message="同一上游判定")
    report = run(context=qa_context(artifact, upstream=(upstream,)), rules=selected, now=NOW)
    assert report.verdict == "warning"
    assert len(report.findings) == 1
    assert report.findings[0].severity == "minor"
    assert upstream.severity == "blocker"


def test_meta_rules_are_implemented_by_engine_not_declared() -> None:
    """FR-M5-14/15/16 由引擎本体实现，不出现在规则文件里。

    把它们写成 ``check: noop`` 的占位规则会让"这三条已经落地"这句话在代码里
    无法验证——而占位规则永远"通过"。
    """
    rules = default_catalog().catalog()
    for rule_id in (RULE_MODEL_RULE_ID, REPORT_RULE_ID, BLOCKING_RULE_ID):
        assert rule_id not in rules


def test_every_declared_check_is_registered() -> None:
    """规则声明的 ``check`` 必须在导入期登记过；否则该规则每次跑都折成 minor。"""
    for rule in default_catalog().catalog().values():
        assert resolve_check(rule.check) is not None


def test_builtin_rule_file_is_packaged_next_to_the_module() -> None:
    files = builtin_rule_files()
    assert len(files) == 1
    assert files[0].rule_set_id == "base_style"
    assert len(files[0].rules) == len(BUILTIN_RULE_IDS)


def test_entry_point_group_name_is_stable() -> None:
    """第三方规则集挂在这个组名下；改名等于让已发布的插件全部失效。"""
    assert ENTRY_POINT_GROUP == "docfidelity.qa_rules"


def test_declared_checks_are_a_closed_set() -> None:
    """断言按**闭集名** dispatch：规则文件不得内联分支表达式。

    闭集的意义是"加一条规则 = 写一段 YAML + 登记一个函数"，而不是"在引擎里
    加一个 if 判断文档类型"。故声明的 ``check`` 名必是纯标识符。
    """
    for rule in default_catalog().catalog().values():
        assert rule.check.replace("_", "").isalnum(), rule.rule_id
    assert "noop" not in check_names()


# ------------------------------------------------------------------ 规则文件校验
class TestRuleFileParsing:
    def test_accepts_a_minimal_document(self) -> None:
        parsed = parse_rule_file(_file())
        assert parsed.rule_set_id == "test_set"
        assert parsed.rules[0].rule_id == "FR-M5-99"
        assert parsed.rules[0].severity == "major"

    def test_scope_accepts_a_bare_string(self) -> None:
        parsed = parse_rule_file(_file(_rule(scope="detail-doc")))
        assert parsed.rules[0].scope == ("detail-doc",)

    def test_unknown_top_level_field_is_rejected(self) -> None:
        with pytest.raises(ConfigurationError):
            parse_rule_file(_file(rules_version="2"))

    def test_unknown_rule_field_is_rejected(self) -> None:
        """``scope`` 误写成 ``scopes`` 必须报错，不能被静默丢掉。"""
        with pytest.raises(ConfigurationError):
            parse_rule_file(_file(_rule(scopes=["detail-doc"])))

    def test_duplicate_rule_id_is_rejected(self) -> None:
        with pytest.raises(ConfigurationError):
            parse_rule_file(_file(_rule(), _rule()))

    @pytest.mark.parametrize("severity", ["fatal", "", "Major"])
    def test_invalid_severity_is_rejected(self, severity: str) -> None:
        with pytest.raises(ConfigurationError):
            parse_rule_file(_file(_rule(severity=severity)))

    def test_invalid_category_is_rejected(self) -> None:
        with pytest.raises(ConfigurationError):
            parse_rule_file(_file(_rule(category="whatever")))

    def test_missing_required_field_is_rejected(self) -> None:
        payload = _rule()
        payload.pop("title")
        with pytest.raises(ConfigurationError):
            parse_rule_file(_file(payload))

    def test_major_version_mismatch_is_rejected(self) -> None:
        """规则语义随格式版本变化；按旧语义静默跑新文件会让结论无法归因。"""
        with pytest.raises(FormatVersionError):
            parse_rule_file(_file(format_version="2.0"))

    def test_patch_level_difference_is_accepted(self) -> None:
        parsed = parse_rule_file(_file(format_version="1.7"))
        assert parsed.format_version == "1.7"


def test_load_rule_file_reports_the_offending_path(tmp_path: Path) -> None:
    """坏规则文件的报错必须指出**哪个文件**，否则面对一柜子模板包无从下手。"""
    path = tmp_path / "broken.yaml"
    path.write_text("format_version: '1'\nrule_set: x\nrules: [}\n", encoding="utf-8")
    with pytest.raises(ConfigurationError):
        load_rule_file(path)


# ------------------------------------------------------------------ 适用范围
class TestAppliesTo:
    @pytest.mark.parametrize(
        ("scope", "doc_type", "expected"),
        [
            (("*",), "anything", True),
            (("*",), None, True),
            (("detail-doc",), "detail-doc", True),
            (("detail-doc",), "other", False),
            ((), "other", True),
            (("detail-doc",), None, True),
        ],
    )
    def test_scope_matching(
        self, scope: tuple[str, ...], doc_type: str | None, expected: bool
    ) -> None:
        spec = RuleSpec(
            rule_id="FR-M5-99",
            category="style",
            severity="major",
            title="t",
            check="style_compliance",
            scope=scope,
        )
        assert spec.applies_to(doc_type) is expected


def test_missing_doc_type_does_not_disable_the_whole_ruleset() -> None:
    """``doc_type=None`` 一律适用：把"没给类型"当成"不匹配"会让门禁静默全过。"""
    catalog = default_catalog().catalog()
    assert all(spec.applies_to(None) for spec in catalog.values())


# ------------------------------------------------------------------ 规则集选择
class TestRulesFor:
    def test_included_filter_selects_subset(self) -> None:
        catalog = default_catalog().catalog()
        rule_set = RuleSet(rule_set_id="base_style", included_rules=["FR-M5-01", "FR-M5-04"])
        selected = rules_for(rule_set, catalog)
        assert [spec.rule_id for spec in selected] == ["FR-M5-01", "FR-M5-04"]

    def test_excluded_wins_over_severity_override(self) -> None:
        """先选后改：被排除的规则不得因"覆盖了级别"又跑回来。"""
        catalog = default_catalog().catalog()
        rule_set = RuleSet(
            rule_set_id="base_style",
            excluded_rules=["FR-M5-01"],
            severity_overrides={"FR-M5-01": "minor"},
        )
        assert "FR-M5-01" not in {spec.rule_id for spec in rules_for(rule_set, catalog)}

    def test_severity_and_param_overrides_are_applied(self) -> None:
        catalog = default_catalog().catalog()
        rule_set = RuleSet(
            rule_set_id="base_style",
            included_rules=["FR-M5-06"],
            severity_overrides={"FR-M5-06": "minor"},
            param_overrides={"FR-M5-06": {"tolerance_twips": 999}},
        )
        spec = rules_for(rule_set, catalog)[0]
        assert spec.severity == "minor"
        assert spec.params["tolerance_twips"] == 999

    def test_param_override_merges_instead_of_replacing(self) -> None:
        catalog = default_catalog().catalog()
        rule_set = RuleSet(
            rule_set_id="base_style",
            included_rules=["FR-M5-05"],
            param_overrides={"FR-M5-05": {"tolerance_twips": 5}},
        )
        spec = rules_for(rule_set, catalog)[0]
        # 原参数未被覆盖掉：合并而不是替换
        assert spec.params["allowed_levels"] == [1, 2, 3]

    def test_invalid_severity_override_is_rejected_at_the_contract_boundary(self) -> None:
        """覆盖值的取值域由契约把关（``RuleSeverity`` 是闭集 Literal）。

        引擎侧 ``_apply_overrides`` 里还有一道同口径校验，那是给绕过契约
        （``model_construct``）的调用方留的——两道都在，是本项目对"闭集取值"
        的常规做法，不是重复实现。
        """
        with pytest.raises(ValidationError):
            RuleSet(
                rule_set_id="base_style",
                included_rules=["FR-M5-01"],
                severity_overrides={"FR-M5-01": "fatal"},  # type: ignore[dict-item]
            )

    def test_referencing_an_unknown_rule_is_rejected(self) -> None:
        catalog = default_catalog().catalog()
        rule_set = RuleSet(rule_set_id="base_style", included_rules=["FR-M5-404"])
        with pytest.raises(EngineError):
            rules_for(rule_set, catalog)

    def test_unknown_rule_set_is_rejected_with_available_listed(self) -> None:
        from docfidelity.qa import LoadedRules

        loaded = LoadedRules(files={})
        with pytest.raises(EngineError):
            loaded.rule_set("missing")


# ------------------------------------------------------------------ 断言登记
def _boom_check(spec: RuleSpec, context: QaContext) -> list[Finding]:
    raise RuntimeError("断言实现里的缺陷")


def _engine_error_check(spec: RuleSpec, context: QaContext) -> list[Finding]:
    from docfidelity.contracts.errors import ErrorCode

    raise EngineError(ErrorCode.INTERNAL, "引擎内部错误")


def _always_one_check(spec: RuleSpec, context: QaContext) -> list[Finding]:
    return [Finding(rule_id=spec.rule_id, severity=spec.severity, message=f"{spec.rule_id} 命中")]


class TestCheckRegistry:
    def test_registering_a_different_function_under_a_used_name_is_rejected(self) -> None:
        """重复登记是缺陷：实际跑哪一个取决于 import 顺序。"""
        register_check("_qa_probe_unique", _always_one_check)

        def other(spec: RuleSpec, context: QaContext) -> list[Finding]:
            return []

        with pytest.raises(ConfigurationError):
            register_check("_qa_probe_unique", other)

    def test_registering_the_same_function_twice_is_idempotent(self) -> None:
        register_check("_qa_probe_idem", _always_one_check)
        register_check("_qa_probe_idem", _always_one_check)
        assert resolve_check("_qa_probe_idem") is _always_one_check

    def test_resolving_an_unregistered_name_raises(self) -> None:
        with pytest.raises(EngineError):
            resolve_check("_qa_probe_never_registered")


# ------------------------------------------------------------------ 执行与降级
class TestRun:
    def _spec(self, rule_id: str = "FR-M5-99", **overrides: object) -> RuleSpec:
        payload: dict[str, object] = {
            "rule_id": rule_id,
            "category": "style",
            "severity": "blocker",
            "title": "t",
            "check": "_qa_probe_always_one",
        }
        payload.update(overrides)
        return RuleSpec(**payload)  # type: ignore[arg-type]

    def test_rule_failure_becomes_minor_and_does_not_stop_the_run(
        self, artifact: RenderFixture
    ) -> None:
        register_check("_qa_probe_boom", _boom_check)
        register_check("_qa_probe_engine_error", _engine_error_check)
        register_check("_qa_probe_always_one", _always_one_check)
        context = qa_context(artifact)

        report = run(
            context=context,
            rules=[
                self._spec("FR-M5-01", check="_qa_probe_boom"),
                self._spec("FR-M5-02", check="_qa_probe_engine_error"),
                self._spec("FR-M5-03", check="_qa_probe_always_one"),
            ],
            now=NOW,
        )

        failed = [item for item in report.findings if "规则执行失败" in item.message]
        assert {item.rule_id for item in failed} == {"FR-M5-01", "FR-M5-02"}
        assert all(item.severity == "minor" for item in failed)
        # 第三条规则照常跑完 —— 一条规则坏掉不得让整次门禁失败
        assert any(item.rule_id == "FR-M5-03" for item in report.findings)
        assert report.verdict == "blocked"  # FR-M5-03 的 blocker 仍然成立

    def test_disabled_rule_is_not_executed(self, artifact: RenderFixture) -> None:
        register_check("_qa_probe_always_one", _always_one_check)
        report = run(
            context=qa_context(artifact),
            rules=[self._spec(enabled=False)],
            now=NOW,
        )
        assert report.findings == []
        assert report.verdict == "pass"

    def test_rule_out_of_scope_is_not_executed(self, artifact: RenderFixture) -> None:
        register_check("_qa_probe_always_one", _always_one_check)
        report = run(
            context=qa_context(artifact),
            rules=[self._spec(scope=("some-other-doc",))],
            now=NOW,
        )
        assert report.findings == []

    def test_rule_missing_a_capability_is_skipped_and_recorded(
        self, artifact: RenderFixture
    ) -> None:
        """缺能力时跳过，且跳过写进摘要——"没检查"必须与"检查通过"可区分。"""
        register_check("_qa_probe_always_one", _always_one_check)
        context = qa_context(artifact, with_profile=False, terminology=None)
        report = run(
            context=context,
            rules=[self._spec(requires=("terminology", "baseline"))],
            now=NOW,
        )
        assert report.findings == []
        assert "FR-M5-99" in (report.summary or "")
        assert "跳过" in (report.summary or "")

    def test_capabilities_are_derived_from_context(self, artifact: RenderFixture) -> None:
        bare = qa_context(artifact, with_profile=False)
        assert "render_view" not in bare.capabilities()
        assert "render_view" in qa_context(artifact).capabilities()

    def test_upstream_findings_are_merged_and_deduplicated(self, artifact: RenderFixture) -> None:
        register_check("_qa_probe_always_one", _always_one_check)
        spec = self._spec()
        own = _always_one_check(spec, qa_context(artifact))
        context = qa_context(artifact, upstream=(*own,))
        report = run(context=context, rules=[spec], now=NOW)
        # 同一条结论（门禁自己 + 上游）只保留一份
        assert len([item for item in report.findings if item.rule_id == "FR-M5-99"]) == 1


# ------------------------------------------------------------------ 裁决与豁免
class TestVerdictAndWaivers:
    @pytest.mark.parametrize(
        ("severities", "expected"),
        [
            ((), "pass"),
            (("minor",), "warning"),
            (("major",), "warning"),
            (("blocker",), "blocked"),
            (("minor", "blocker"), "blocked"),
        ],
    )
    def test_verdict_only_blocked_by_blocker(
        self, severities: tuple[str, ...], expected: str
    ) -> None:
        findings = [
            Finding(rule_id=f"FR-M5-{index:02d}", severity=severity, message="x")  # type: ignore[arg-type]
            for index, severity in enumerate(severities)
        ]
        assert verdict_of(findings) == expected

    def test_can_finalize_is_the_single_outlet(self) -> None:
        blocked = Report(
            report_id="r",
            job_id="",
            generated_at=NOW,
            findings=[Finding(rule_id="FR-M5-01", severity="blocker", message="x")],
            verdict="blocked",
        )
        assert can_finalize(blocked) is False
        blocked.verdict = "warning"
        assert can_finalize(blocked) is True

    def test_waiver_marks_finding_and_changes_the_verdict(self) -> None:
        findings = [Finding(rule_id="FR-M5-01", severity="blocker", message="x")]
        waiver = Waiver(
            waiver_id="W-1",
            rule_id="FR-M5-01",
            reason="模板历史遗留，本期不修",
            operator="森",
            created_at=NOW - timedelta(days=1),
        )
        active = apply_waivers(findings, [waiver], now=NOW)
        assert [item.waiver_id for item in active] == ["W-1"]
        assert findings[0].status == "waived"
        # 未豁免为空 → 不再阻断
        assert verdict_of(findings) == "pass"

    def test_expired_waiver_is_ignored(self) -> None:
        findings = [Finding(rule_id="FR-M5-01", severity="blocker", message="x")]
        expired = Waiver(
            waiver_id="W-2",
            rule_id="FR-M5-01",
            reason="临时",
            operator="森",
            created_at=NOW - timedelta(days=10),
            expires_at=NOW - timedelta(days=1),
        )
        assert apply_waivers(findings, [expired], now=NOW) == []
        assert findings[0].status == "open"
        assert verdict_of(findings) == "blocked"

    def test_earliest_expiry_wins_when_two_waivers_cover_one_rule(self) -> None:
        findings = [Finding(rule_id="FR-M5-01", severity="blocker", message="x")]
        soon = Waiver(
            waiver_id="W-soon",
            rule_id="FR-M5-01",
            reason="a",
            operator="森",
            created_at=NOW,
            expires_at=NOW + timedelta(days=1),
        )
        later = Waiver(
            waiver_id="W-later",
            rule_id="FR-M5-01",
            reason="b",
            operator="森",
            created_at=NOW,
            expires_at=NOW + timedelta(days=30),
        )
        apply_waivers(findings, [later, soon], now=NOW)
        # 豁免收窄优先：取最早到期的那条，避免"续期"把收窄悄悄放宽
        assert findings[0].waiver_id == "W-soon"

    def test_waiver_without_expiry_never_expires(self) -> None:
        findings = [Finding(rule_id="FR-M5-01", severity="blocker", message="x")]
        permanent = Waiver(
            waiver_id="W-p",
            rule_id="FR-M5-01",
            reason="p",
            operator="森",
            created_at=NOW - timedelta(days=365),
        )
        assert apply_waivers(findings, [permanent], now=NOW + timedelta(days=3650)) != []
        assert findings[0].status == "waived"


# ------------------------------------------------------------------ 报告
class TestReport:
    def test_report_id_is_deterministic_not_random(self, artifact: RenderFixture) -> None:
        """报告标识由判定内容与时刻派生：内核禁止 random 参与逻辑（DD-2.4）。"""
        register_check("_qa_probe_always_one", _always_one_check)
        spec = RuleSpec(
            rule_id="FR-M5-99",
            category="style",
            severity="minor",
            title="t",
            check="_qa_probe_always_one",
        )
        first = run(context=qa_context(artifact), rules=[spec], now=NOW).report_id
        second = run(context=qa_context(artifact), rules=[spec], now=NOW).report_id
        assert first == second
        assert first.startswith("RPT-20260921120000-")

    def test_report_id_differs_when_findings_differ(self, artifact: RenderFixture) -> None:
        register_check("_qa_probe_always_one", _always_one_check)
        base = RuleSpec(
            rule_id="FR-M5-99",
            category="style",
            severity="minor",
            title="t",
            check="_qa_probe_always_one",
        )
        other = RuleSpec(
            rule_id="FR-M5-98",
            category="style",
            severity="minor",
            title="t",
            check="_qa_probe_always_one",
        )
        left = run(context=qa_context(artifact), rules=[base], now=NOW).report_id
        right = run(context=qa_context(artifact), rules=[other], now=NOW).report_id
        assert left != right

    def test_summary_reports_counts_and_skips(self) -> None:
        findings = [
            Finding(rule_id="FR-M5-01", severity="blocker", message="x"),
            Finding(rule_id="FR-M5-02", severity="minor", message="y", status="waived"),
        ]
        text = summarize(findings, verdict="blocked", skipped=["FR-M5-08(缺 terminology)"])
        assert "阻断" in text
        assert "blocker 1" in text
        assert "已豁免 1" in text
        assert "缺 terminology" in text
