"""版式与结构类规则的正反例：FR-M5-04 / 05 / 06 / 07 / 11 / 13（DD-10.2 的版式组）。

这一组里最值得对立的两个方向：

* **几何判据（06 / 07）必须真的取到对象**。``w:tbl`` 挂在 ``w:body`` 下、
  ``wp:extent`` 挂在 ``w:drawing`` 里，都**不是**部件根的直接子元素——按直接子元素
  取会恒返回空，"表格越界""图片越界"两条规则于是永远"通过"，而报告上看不出区别。
  故这里两个方向都钉：干净产物零发现，**改宽一处必须报出来**。
* **对照型规则（11 / 13）必须先归一化**。同一份文件与自己比对必须判"一致"；
  只有真差异才报。否则 ``_Toc`` 书签 ID 这类易变标识会让每次构建都报差异。
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from docfidelity.contracts.ir import ContentIR, ContentUnit
from docfidelity.contracts.qa import Finding
from docfidelity.ooxml import DocxPackage, find_all, find_first, iter_elements
from docfidelity.qa import default_catalog, run
from tests.fixtures.docx_edit import (
    add_paragraph_attribute,
    body_of,
    content_paragraphs,
    injected_tables,
    set_grid_width,
    set_picture_width,
)
from tests.fixtures.qa_fixture import QA_DOC_TYPE, RenderFixture, qa_context, render_fixture

pytestmark = pytest.mark.unit

#: 版心宽度量级参考：A4 单栏在 2 万 twips 上下。取远大于它的值来造越界。
_HUGE_WIDTH_TWIPS = 90000
#: 同理，图片宽度用远超版心的 EMU 值（1 inch = 914400 EMU）。
_HUGE_WIDTH_EMU = 40_000_000


@pytest.fixture(scope="module")
def artifact(tmp_path_factory: pytest.TempPathFactory) -> RenderFixture:
    return render_fixture(tmp_path_factory.mktemp("qa-layout"))


def _run(rule_id: str, context) -> list[Finding]:
    return run(context=context, rules=[default_catalog().catalog()[rule_id]]).findings


def _matching(findings: list[Finding], needle: str) -> list[Finding]:
    return [item for item in findings if needle in item.message]


def _ir(*units: ContentUnit) -> ContentIR:
    return ContentIR(
        project_id="probe",
        doc_key="probe",
        profile_id=QA_DOC_TYPE,
        template_ref="TPL-DEMO@1.0",
        units=list(units),
    )


# ------------------------------------------------------------------ FR-M5-04
class TestPagination:
    def test_clean_artifact_has_no_off_whitelist_break(self, artifact: RenderFixture) -> None:
        """干净产物零发现。

        这条同时钉住目录**条目克隆**的那处修正：目录块的第一条带着"目录另起一页"的
        ``pageBreakBefore``（版块级属性），新建条目若把它一起深拷贝过来，产物就会
        多出若干白名单之外的分页点，本条规则随即判 blocker。
        """
        assert _run("FR-M5-04", qa_context(artifact)) == []

    def test_undocked_page_break_is_blocked(self, artifact: RenderFixture) -> None:
        context = qa_context(artifact)
        target = content_paragraphs(context)[0]
        add_paragraph_attribute(target, "w:pageBreakBefore")

        findings = _run("FR-M5-04", context)
        blocked = _matching(findings, "未声明的分页点")
        assert len(blocked) == 1
        assert blocked[0].severity == "blocker"

    def test_whitelisted_break_on_front_matter_is_accepted(self, artifact: RenderFixture) -> None:
        """封面 / 修订记录 / 目录三处的强制分页是**声明过的**，不得判违规。"""
        findings = _run("FR-M5-04", qa_context(artifact))
        assert all("sdt" not in (item.evidence or "") for item in findings)


# ------------------------------------------------------------------ FR-M5-05
class TestStructure:
    def test_clean_ir_has_no_structural_findings(self, artifact: RenderFixture) -> None:
        assert _run("FR-M5-05", qa_context(artifact)) == []

    def test_level_out_of_range_is_flagged(self, artifact: RenderFixture) -> None:
        ir = _ir(
            ContentUnit(unit_id="c1", type="heading", level=4, text="过深的标题"),
            ContentUnit(unit_id="c1#p0", type="para", text="正文"),
        )
        findings = _run("FR-M5-05", qa_context(replace(artifact, ir=ir)))
        assert _matching(findings, "不在允许范围")

    def test_skipped_level_is_flagged(self, artifact: RenderFixture) -> None:
        """1 级直接跳到 3 级：编号不连续。"""
        ir = _ir(
            ContentUnit(
                unit_id="c1",
                type="heading",
                level=1,
                text="一级",
                children=[ContentUnit(unit_id="c1#p0", type="para", text="正文")],
            ),
            ContentUnit(unit_id="c3", type="heading", level=3, text="三级"),
            ContentUnit(unit_id="c3#p0", type="para", text="正文"),
        )
        findings = _run("FR-M5-05", qa_context(replace(artifact, ir=ir)))
        assert _matching(findings, "层级跳号")

    def test_empty_section_is_flagged(self, artifact: RenderFixture) -> None:
        ir = _ir(
            ContentUnit(unit_id="c1", type="heading", level=1, text="空章节"),
            ContentUnit(
                unit_id="c2",
                type="heading",
                level=1,
                text="有内容",
                children=[ContentUnit(unit_id="c2#p0", type="para", text="正文")],
            ),
        )
        findings = _run("FR-M5-05", qa_context(replace(artifact, ir=ir)))
        assert _matching(findings, "空章节")

    def test_heading_right_after_heading_is_minor(self, artifact: RenderFixture) -> None:
        ir = _ir(
            ContentUnit(unit_id="c1", type="heading", level=1, text="一"),
            ContentUnit(
                unit_id="c2",
                type="heading",
                level=1,
                text="二",
                children=[ContentUnit(unit_id="c2#p0", type="para", text="正文")],
            ),
        )
        findings = _run("FR-M5-05", qa_context(replace(artifact, ir=ir)))
        orphan = _matching(findings, "孤立标题")
        assert orphan and orphan[0].severity == "minor"

    def test_allowed_levels_come_from_rule_params(self, artifact: RenderFixture) -> None:
        """阈值由规则声明（``params.allowed_levels``），实现里不得写死。"""
        spec = default_catalog().catalog()["FR-M5-05"]
        assert spec.params.get("allowed_levels") == [1, 2, 3]


# ------------------------------------------------------------------ FR-M5-06
class TestTableOverflow:
    def test_clean_artifact_reports_nothing(self, artifact: RenderFixture) -> None:
        assert _run("FR-M5-06", qa_context(artifact)) == []

    def test_over_wide_table_is_blocked(self, artifact: RenderFixture) -> None:
        """造一张远超版心的表——这条同时证明规则**真的取到了** ``w:tbl``。"""
        context = qa_context(artifact)
        table = injected_tables(context)[0]
        set_grid_width(table, _HUGE_WIDTH_TWIPS)

        findings = _run("FR-M5-06", context)
        blocked = _matching(findings, "超出版心")
        assert len(blocked) == 1
        assert blocked[0].severity == "blocker"
        assert str(_HUGE_WIDTH_TWIPS) in (blocked[0].evidence or "")

    def test_tolerance_comes_from_rule_params(self) -> None:
        assert "tolerance_twips" in default_catalog().catalog()["FR-M5-06"].params


# ------------------------------------------------------------------ FR-M5-07
class TestPictureOverflow:
    def test_clean_artifact_reports_nothing(self, artifact: RenderFixture) -> None:
        """夹具的图在版心内，且渲染层已为图片段落置 ``keepNext``。"""
        assert _run("FR-M5-07", qa_context(artifact)) == []

    def test_over_wide_picture_is_blocked(self, artifact: RenderFixture) -> None:
        context = qa_context(artifact)
        set_picture_width(context.pkg, _HUGE_WIDTH_EMU)

        findings = _run("FR-M5-07", context)
        blocked = _matching(findings, "超出可用区域")
        assert len(blocked) == 1
        assert blocked[0].severity == "blocker"

    def test_picture_without_keep_next_is_flagged(self, artifact: RenderFixture) -> None:
        """图与图注可能被分页拆开：可机械判定的那一半必须报出来。"""
        context = qa_context(artifact)
        paragraph = _picture_paragraph(context.pkg)
        properties = find_first(paragraph, "w:pPr")
        keep = find_first(properties, "w:keepNext")
        assert keep is not None, "渲染层本应给图片段落置 keepNext"
        properties.remove(keep)

        findings = _run("FR-M5-07", context)
        assert _matching(findings, "未置 keepNext")


def _picture_paragraph(pkg: DocxPackage):
    for paragraph in iter_elements(body_of(pkg), "w:p"):
        for candidate_run in find_all(paragraph, "w:r"):
            if any(
                find_first(candidate_run, f"w:{name}") is not None for name in ("drawing", "pict")
            ):
                return paragraph
    pytest.fail("产物里没有包含图片的段落")


# ------------------------------------------------------------------ FR-M5-11 / 13
class TestComparisonRules:
    def test_identical_package_compares_equal(self, artifact: RenderFixture) -> None:
        """同一份文件与自己比对必须判"一致"——这是"先归一化"的直接结果。

        若不归一化，``_Toc`` 书签 ID、关系 ID 这类每次读盘都会被重写的字段
        会让这条规则每次构建都报差异，最终被人关掉。
        """
        context = qa_context(artifact, baseline=artifact.open(), baseline_ref="base")
        assert _run("FR-M5-11", context) == []
        assert _run("FR-M5-13", context) == []

    def test_missing_baseline_skips_the_rule(self, artifact: RenderFixture) -> None:
        """没给基线时规则被跳过（能力门禁），而不是"通过与否"二选一。"""
        context = qa_context(artifact)
        assert "baseline" not in context.capabilities()
        assert _run("FR-M5-11", context) == []

    def test_real_difference_is_reported(self, artifact: RenderFixture) -> None:
        """基线换成模板原件（结构完全不同）→ 必须报出差异。"""
        baseline = DocxPackage.open(artifact.template_path, read_only=True)
        context = qa_context(artifact, baseline=baseline, baseline_ref="template")
        assert _matching(_run("FR-M5-11", context), "归一化后仍不一致")
        assert _matching(_run("FR-M5-13", context), "结构存在差异")

    def test_regression_baseline_is_minor(self, artifact: RenderFixture) -> None:
        baseline = DocxPackage.open(artifact.template_path, read_only=True)
        context = qa_context(artifact, baseline=baseline, baseline_ref="template")
        findings = _matching(_run("FR-M5-13", context), "结构存在差异")
        assert findings[0].severity == "minor"
