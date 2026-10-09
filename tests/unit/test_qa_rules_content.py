"""内容与追溯类规则的正反例：FR-M5-08 / 09 / 10 / 12 / 17（DD-10.2 的内容组）。

这一组的共同点是判据**不在版面上**，而在"说得通吗"这一层。故它们的正反例都靠
**换输入**而不是改产物：换术语表、换追溯矩阵、换目检结果、换 IR。

两条纪律在这里各钉一次：

* **降级必须写进报告**（FR-M5-12）：渲染服务不可用时报告要说"没做目检，原因是 X"，
  而不是安静返回空——"没检查"与"检查通过"必须在报告里可区分。
* **类型无关检索必须把 ``trace`` / ``origin`` 列白名单**（FR-M5-17）：那两个字段存
  ``kind:key`` 引用句柄（DD-3.2（2）的已知边界），若不剔除，任何带追溯的 IR 都会被
  判成"引擎里出现了文档类型名"，而它其实是**数据**。
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from docfidelity.contracts.config import Terminology, TerminologyEntry
from docfidelity.contracts.ir import ContentIR, ContentUnit
from docfidelity.contracts.profile import ChapterSkeletonItem, DocTypeProfile
from docfidelity.contracts.qa import Finding, TraceEdge, TraceMatrix
from docfidelity.contracts.template import (
    BlockLocator,
    PlaceholderEntry,
    PlaceholderList,
    SlotSpec,
)
from docfidelity.qa import SpotcheckResult, default_catalog, run, spotcheck
from tests.fixtures.qa_fixture import (
    QA_DOC_TYPE,
    RenderFixture,
    qa_context,
    qa_profile,
    render_fixture,
)
from tests.fixtures.render_fixture import CHAPTER_PARAGRAPH

pytestmark = pytest.mark.unit


@pytest.fixture(scope="module")
def artifact(tmp_path_factory: pytest.TempPathFactory) -> RenderFixture:
    return render_fixture(tmp_path_factory.mktemp("qa-content"))


def _run(rule_id: str, context) -> list[Finding]:
    return run(context=context, rules=[default_catalog().catalog()[rule_id]]).findings


def _matching(findings: list[Finding], needle: str) -> list[Finding]:
    return [item for item in findings if needle in item.message]


def _ir_with_text(text: str, *, trace: list[str] | None = None) -> ContentIR:
    return ContentIR(
        project_id="probe",
        doc_key="probe",
        profile_id=QA_DOC_TYPE,
        template_ref="TPL-DEMO@1.0",
        units=[
            ContentUnit(
                unit_id="c1#p0",
                type="para",
                text=text,
                trace=list(trace or []),
            )
        ],
    )


# ------------------------------------------------------------------ FR-M5-08
class TestTerminology:
    def test_no_terminology_capability_skips_the_rule(self, artifact: RenderFixture) -> None:
        context = qa_context(artifact)
        assert "terminology" not in context.capabilities()
        assert _run("FR-M5-08", context) == []

    def test_clean_terminology_reports_nothing(self, artifact: RenderFixture) -> None:
        terms = Terminology(scope="project", entries=[TerminologyEntry(term="演示系统")])
        assert _run("FR-M5-08", qa_context(artifact, terminology=terms)) == []

    def test_forbidden_alias_in_the_artifact_is_blocked(self, artifact: RenderFixture) -> None:
        """禁用旧称出现在**产物段落**里即阻断（不只看 IR：渲染期可能又拼回去）。"""
        terms = Terminology(
            scope="project",
            entries=[TerminologyEntry(term="注入的正文第一段。", forbidden=["注入的正文"])],
        )
        findings = _run("FR-M5-08", qa_context(artifact, terminology=terms))
        stale = _matching(findings, "禁用写法")
        assert stale and stale[0].severity == "blocker"
        assert "注入的正文" in (stale[0].evidence or "")

    def test_forbidden_alias_only_in_the_ir_is_also_blocked(self, artifact: RenderFixture) -> None:
        terms = Terminology(
            scope="project",
            entries=[TerminologyEntry(term="规范写法", forbidden=["IR 里的旧称"])],
        )
        ir = _ir_with_text("IR 里的旧称出现在这里")
        findings = _run("FR-M5-08", qa_context(replace(artifact, ir=ir), terminology=terms))
        assert _matching(findings, "禁用写法")

    def test_unresolved_variable_is_major(self, artifact: RenderFixture) -> None:
        terms = Terminology(scope="project", entries=[TerminologyEntry(term="演示系统")])
        ir = _ir_with_text("这一段含未解析变量 {{未定义变量}}。")
        findings = _run("FR-M5-08", qa_context(replace(artifact, ir=ir), terminology=terms))
        unresolved = _matching(findings, "未能解析的术语变量")
        assert unresolved and unresolved[0].severity == "major"
        assert "未定义变量" in (unresolved[0].evidence or "")


# ------------------------------------------------------------------ FR-M5-09
class TestCompleteness:
    def test_no_residue_no_slots_no_unresolved_reports_nothing(
        self, artifact: RenderFixture
    ) -> None:
        """占位符清单清空后，三条判定分支都不该报。"""
        context = _with_placeholders(artifact, [])
        assert _run("FR-M5-09", context) == []

    def test_placeholder_residue_is_reported(self, artifact: RenderFixture) -> None:
        """产物里还印着登记的占位原文 → 残留。

        判据是**文本探针**，不是"定位到的那段是否有字"：门禁手里只有产物，位置型
        定位符（``(//w:p)[n]``）在产物里已经指向别的段落，按定位判会把整篇正文都
        算成占位残留。故 ``locator`` 只贡献"占位当初在哪个部件"这一项信息。
        """
        context = _with_placeholders(
            artifact,
            [
                PlaceholderEntry(
                    text=CHAPTER_PARAGRAPH,
                    locator=BlockLocator(part="word/document.xml", xpath="(//w:p)[1]"),
                )
            ],
        )
        findings = _run("FR-M5-09", context)
        residue = _matching(findings, "占位文本残留")
        assert residue and residue[0].severity == "major"
        assert CHAPTER_PARAGRAPH in (residue[0].evidence or "")

    def test_stale_locator_alone_is_not_residue(self, artifact: RenderFixture) -> None:
        """定位符指向的段落非空、但产物里查不到登记的原文 → **不是**残留。

        这条钉的是 P0-18 发现的那处误报：``locate_placeholders`` 的定位符是位置型的，
        产物侧的序号已经因为裁剪与注入而漂移，于是"那一格里有字"被当成了"占位没清掉"。
        产物里一个字都没少清，报告上却写着"仍有 3 处占位残留"。
        """
        context = _with_placeholders(
            artifact,
            [
                PlaceholderEntry(
                    text="未清理的占位示例文本",
                    locator=BlockLocator(part="word/document.xml", xpath="//w:body/w:p[1]"),
                )
            ],
        )
        assert not _matching(_run("FR-M5-09", context), "占位文本残留")

    def test_unfilled_required_slot_is_reported(self, artifact: RenderFixture) -> None:
        """声明了必填槽位、而该章节在产物里没有任何内容 → 槽位未落位。"""
        profile = _profile_with_required_slot("范围")
        ir = ContentIR(
            project_id="probe",
            doc_key="probe",
            profile_id=QA_DOC_TYPE,
            template_ref="TPL-DEMO@1.0",
            units=[
                ContentUnit(
                    unit_id="c1",
                    type="heading",
                    level=1,
                    text="概述",
                    children=[ContentUnit(unit_id="c1#p0", type="para", text="正文")],
                ),
                ContentUnit(unit_id="c2", type="heading", level=1, text="范围"),
                ContentUnit(
                    unit_id="c3",
                    type="heading",
                    level=1,
                    text="图示",
                    children=[ContentUnit(unit_id="c3#p0", type="para", text="正文")],
                ),
            ],
        )
        context = _with_placeholders(replace(artifact, ir=ir), [])
        context.profile = profile
        assert _matching(_run("FR-M5-09", context), "槽位未按体例落位")

    def test_variable_chapter_title_is_resolved_before_matching(
        self, artifact: RenderFixture
    ) -> None:
        """骨架标题里的 ``{{变量}}`` 要先过术语替换，再与 IR 标题比对。

        不替换的话，**每一个标题带变量的章节**都会被判成"必填槽位未填充"——
        产物里那一章明明有内容。IR 侧的标题已经过 ``glossary_apply``，骨架侧不过，
        两边就不是在比同一个字符串。
        """
        profile = qa_profile()
        profile.genre.chapter_skeleton.append(
            ChapterSkeletonItem(
                level=1,
                title="{{演示系统}}概述",
                slots=[SlotSpec(slot_id="probe-slot", required=True, locator="chapter[4]")],
            )
        )
        ir = ContentIR(
            project_id="probe",
            doc_key="probe",
            profile_id=QA_DOC_TYPE,
            template_ref="TPL-DEMO@1.0",
            units=[
                ContentUnit(
                    unit_id="c1",
                    type="heading",
                    level=1,
                    text="演示系统概述",
                    children=[ContentUnit(unit_id="c1#p0", type="para", text="正文")],
                )
            ],
        )
        context = qa_context(
            replace(artifact, ir=ir),
            profile=profile,
            terminology=Terminology(scope="project", entries=[TerminologyEntry(term="演示系统")]),
        )
        assert not _matching(_run("FR-M5-09", context), "槽位未按体例落位")

    def test_chapter_with_content_does_not_trigger_slot_findings(
        self, artifact: RenderFixture
    ) -> None:
        """有内容的章节不参检：产物侧归不到**槽位**，只在章节粒度判，宁可不报也不误报。"""
        profile = _profile_with_required_slot("范围")
        context = _with_placeholders(artifact, [])
        context.profile = profile
        assert not _matching(_run("FR-M5-09", context), "槽位未按体例落位")

    def test_unresolved_items_are_minor_and_do_not_block(self, artifact: RenderFixture) -> None:
        ir = _ir_with_text("正文")
        ir.unresolved = ["slot:missing", "trace:unknown"]
        context = _with_placeholders(replace(artifact, ir=ir), [])
        pending = _matching(_run("FR-M5-09", context), "待确认项")
        assert pending and pending[0].severity == "minor"


def _with_placeholders(fixture: RenderFixture, entries: list[PlaceholderEntry]) -> Any:
    """把模板包里的占位符清单换成给定内容（其余契约不变）。

    这样"占位残留"这一支的判据不依赖于夹具模板自带的占位符清单——否则断言会
    跟着模板入库结果一起漂移，而测试要钉的是**规则**的行为。
    """
    template = fixture.template.model_copy(
        update={"placeholders": PlaceholderList(entries=list(entries))}
    )
    return qa_context(replace(fixture, template=template))


def _profile_with_required_slot(chapter_title: str) -> DocTypeProfile:
    """把 ``chapter_title`` 那一章声明为"含必填槽位"（其余骨架不变）。

    断言"确实改到了"：改不到时槽位根本不参与校验，正例会**假通过**——而这正是
    "反例能抓到"这类断言的常见死法。
    """
    profile = qa_profile()
    targets = [item for item in profile.genre.chapter_skeleton if item.title == chapter_title]
    assert targets, f"夹具骨架里没有章节 {chapter_title!r}"
    for item in targets:
        item.slots = [SlotSpec(slot_id="probe-slot", required=True, locator="chapter.section[1]")]
    return profile


# ------------------------------------------------------------------ FR-M5-10
class TestTraceability:
    def test_complete_matrix_reports_nothing(self, artifact: RenderFixture) -> None:
        traces = TraceMatrix(
            edges=[TraceEdge(unit_id="c1#p0", object_ref="x:1", source="seed", locator="row 1")]
        )
        assert _run("FR-M5-10", qa_context(artifact, traces=traces)) == []

    def test_orphans_are_major_not_blocker(self, artifact: RenderFixture) -> None:
        """``orphans`` 是**信息**而非错误：判成 blocker 会逼人给内容编一个来源。"""
        traces = TraceMatrix(orphans=["obj:1"], unused=["obj:2"])
        findings = _matching(
            _run("FR-M5-10", qa_context(artifact, traces=traces)), "追溯矩阵不完整"
        )
        assert findings and findings[0].severity == "major"

    def test_no_matrix_capability_skips_the_rule(self, artifact: RenderFixture) -> None:
        context = qa_context(artifact)
        assert "trace" not in context.capabilities()
        assert _run("FR-M5-10", context) == []


# ------------------------------------------------------------------ FR-M5-12
class TestSpotcheck:
    def test_degradation_is_reported_with_a_reason(self, artifact: RenderFixture) -> None:
        """降级是**结论的一部分**：报告必须说"没做目检，原因是 X"。"""
        result = spotcheck(artifact.path, keywords=["概述"], converter=lambda *_: None)
        assert result.degraded and result.reason

        findings = _run("FR-M5-12", qa_context(artifact, spotcheck=result))
        degraded = _matching(findings, "已降级")
        assert degraded and degraded[0].severity == "major"
        assert "原因" in (degraded[0].evidence or "")

    def test_pages_located_by_keyword_report_nothing(self, artifact: RenderFixture) -> None:
        result = _fake_spotcheck(artifact.path, pages=[1, 2], keywords=["概述"])
        assert _run("FR-M5-12", qa_context(artifact, spotcheck=result)) == []

    def test_executed_but_no_page_matched_is_minor(self, artifact: RenderFixture) -> None:
        result = _fake_spotcheck(artifact.path, pages=[], keywords=["不存在的关键词"])
        findings = _run("FR-M5-12", qa_context(artifact, spotcheck=result))
        assert _matching(findings, "未定位到任何页面")

    def test_no_spotcheck_capability_skips_the_rule(self, artifact: RenderFixture) -> None:
        context = qa_context(artifact)
        assert "spotcheck" not in context.capabilities()
        assert _run("FR-M5-12", context) == []


def _fake_spotcheck(docx: Path, *, pages: list[int], keywords: list[str]) -> SpotcheckResult:
    """注入式目检：把 ``docx -> PDF`` 与 ``PDF -> 每页文本`` 都换成假的。

    这样"挑页逻辑"与"文案"在没有 Office 的 CI 上也能被验证；真转换器只需在
    有 Office 的机器上由 :func:`docfidelity.qa.spotcheck` 走一遍。
    """
    page_texts = ["概述\n范围\n" if index in pages else "其它\n" for index in (1, 2, 3)]
    return spotcheck(
        docx,
        keywords=keywords,
        converter=lambda _docx, out: _touch(out / "fake.pdf"),
        extractor=lambda _pdf: page_texts,
    )


def _touch(path: Path) -> Path:
    path.write_bytes(b"%PDF-1.4 fake")
    return path


# ------------------------------------------------------------------ FR-M5-17
class TestTypeAgnostic:
    def test_clean_ir_reports_nothing(self, artifact: RenderFixture) -> None:
        assert _run("FR-M5-17", qa_context(artifact)) == []

    def test_doc_type_name_in_the_engine_surface_is_blocked(self, artifact: RenderFixture) -> None:
        """``unit_id`` 属**引擎表面**（由投影路径派生），出现文档类型名即违规。"""
        ir = ContentIR(
            project_id="probe",
            doc_key="probe",
            profile_id=QA_DOC_TYPE,
            template_ref="TPL-DEMO@1.0",
            units=[ContentUnit(unit_id="概要设计#1", type="para", text="正文")],
        )
        findings = _run("FR-M5-17", qa_context(replace(artifact, ir=ir)))
        hit = _matching(findings, "类型无关检索命中")
        assert hit and hit[0].severity == "blocker"
        assert "概要设计" in (hit[0].evidence or "")

    def test_trace_handles_are_whitelisted(self, artifact: RenderFixture) -> None:
        """``trace`` 存 ``kind:key`` 句柄，属**数据**：不得被当成引擎里的类型名。"""
        ir = _ir_with_text("正文", trace=["概要设计:1"])
        assert _run("FR-M5-17", qa_context(replace(artifact, ir=ir))) == []

    def test_origin_handles_are_whitelisted(self, artifact: RenderFixture) -> None:
        from docfidelity.contracts.ir import OriginRef

        ir = ContentIR(
            project_id="probe",
            doc_key="probe",
            profile_id=QA_DOC_TYPE,
            template_ref="TPL-DEMO@1.0",
            units=[
                ContentUnit(
                    unit_id="c1#p0",
                    type="para",
                    text="正文",
                    origin=OriginRef(source="详细设计", locator="row 3"),
                )
            ],
        )
        assert _run("FR-M5-17", qa_context(replace(artifact, ir=ir))) == []

    def test_source_scan_is_off_by_default(self) -> None:
        """``params.roots`` 为空即不扫源码：CI 每次提交已扫，构建期再扫只是拖慢。"""
        assert default_catalog().catalog()["FR-M5-17"].params.get("roots") == []
