"""样式类规则的正反例：FR-M5-01 / 02 / 03（DD-10.2 的样式组）。

三条规则回答同一问题的三个侧面——「产物有没有偷偷改动样式层」。故每条都配**对立
断言**：干净产物必须**一条都不报**，改坏一处必须恰好报那一条。

其中最要紧的一组对立断言是关于 ``w:ind`` 的：渲染层的 ``fix_cell_layout``
（DD-9.4 / FR-M4-08）**故意**把单元格段落的继承缩进清零，写成 ``w:ind/@w:left="0"``。
若把它算作"直设格式残留"，每一份带注入表格的产物都会刷出 blocker，而报告指向的是
引擎自己的正确行为。故这里同时钉住两端：**单元格里的全零缩进不报**，
**正文里的非零缩进照报**。
"""

from __future__ import annotations

import pytest

from docfidelity.contracts.qa import Finding
from docfidelity.ooxml import (
    DocxPackage,
    ensure_child_in_order,
    find_first,
    iter_elements,
    paragraph_text,
    set_attr,
)
from docfidelity.qa import default_catalog, run
from tests.fixtures.docx_edit import (
    add_paragraph_attribute,
    cell_paragraphs,
    content_paragraph,
    content_paragraphs,
    front_matter_tables,
    injected_tables,
    make_bold,
    row_cells,
    set_paragraph_text,
)
from tests.fixtures.qa_fixture import RenderFixture, qa_context, render_fixture

pytestmark = pytest.mark.unit


@pytest.fixture(scope="module")
def artifact(tmp_path_factory: pytest.TempPathFactory) -> RenderFixture:
    return render_fixture(tmp_path_factory.mktemp("qa-style"))


def _rule(rule_id: str):
    return default_catalog().catalog()[rule_id]


def _run(rule_id: str, context) -> list[Finding]:
    """只跑一条规则；返回它自己的判定（不含上游）。"""
    return run(context=context, rules=[_rule(rule_id)]).findings


def _matching(findings: list[Finding], needle: str) -> list[Finding]:
    return [item for item in findings if needle in item.message]


# ------------------------------------------------------------------ FR-M5-01
class TestStyleCompliance:
    def test_clean_artifact_reports_nothing(self, artifact: RenderFixture) -> None:
        """干净产物零发现。

        这条断言同时钉住渲染层的单元格缩进归一：夹具**含注入表格**，
        若 ``w:ind/@w:left="0"`` 被判成直设格式，这里必然红。
        """
        assert _run("FR-M5-01", qa_context(artifact)) == []

    def test_direct_paragraph_attribute_is_flagged(self, artifact: RenderFixture) -> None:
        context = qa_context(artifact)
        target = content_paragraph(context.pkg, 0)
        add_paragraph_attribute(target, "w:jc", "center")

        findings = _run("FR-M5-01", context)
        direct = _matching(findings, "直设格式")
        assert len(direct) == 1
        assert direct[0].severity == "major"
        assert "jc" in (direct[0].evidence or "")

    def test_hanging_style_reference_is_flagged(self, artifact: RenderFixture) -> None:
        """引用了不存在的 styleId：Word 会静默回退，产物"看着正常"地走样。"""
        context = qa_context(artifact)
        target = content_paragraph(context.pkg, 0)
        add_paragraph_attribute(target, "w:pStyle", "GhostStyle")

        findings = _run("FR-M5-01", context)
        missing = _matching(findings, "未定义的样式")
        assert len(missing) == 1
        assert missing[0].severity == "blocker"
        assert "GhostStyle" in (missing[0].evidence or "")

    def test_style_definition_deviation_is_flagged(self, artifact: RenderFixture) -> None:
        """样式**定义**被改动（字体字号偏离契约）同样算样式层被破坏。"""
        context = qa_context(artifact)
        style_id = _perturb_one_style_size(context)
        findings = _run("FR-M5-01", context)
        deviations = _matching(findings, "偏离模板契约")
        assert deviations, f"改动样式 {style_id!r} 的字号后应报出定义偏离"

    def test_legacy_contract_reports_limited_validation_scope(
        self, artifact: RenderFixture
    ) -> None:
        context = qa_context(artifact)
        context.template = context.template.model_copy(deep=True)
        context.template.styles[0].raw_xml = None
        findings = _run("FR-M5-01", context)
        limited = _matching(findings, "检查范围受限")
        assert len(limited) == 1
        assert limited[0].severity == "minor"
        assert context.template.styles[0].style_id in (limited[0].evidence or "")
        assert "重新入库" in (limited[0].suggestion or "")

    @pytest.mark.parametrize("style_type", ["paragraph", "table"])
    def test_deleted_referenced_definition_is_flagged(
        self, artifact: RenderFixture, style_type: str
    ) -> None:
        context = qa_context(artifact)
        expected = next(style for style in context.template.styles if style.type == style_type)
        if style_type == "paragraph":
            add_paragraph_attribute(
                content_paragraph(context.pkg, 0), "w:pStyle", expected.style_id
            )
        else:
            table = injected_tables(context)[0]
            properties = ensure_child_in_order(table, "w:tblPr", ())
            set_attr(
                ensure_child_in_order(properties, "w:tblStyle", ()), "w:val", expected.style_id
            )
        part = context.pkg.find_part("word/styles.xml")
        assert part is not None
        definition = next(
            node
            for node in iter_elements(part.read_tree, "w:style")
            if node.get("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}styleId")
            == expected.style_id
        )
        part.read_tree.remove(definition)
        findings = _run("FR-M5-01", context)
        assert _matching(findings, "未定义的样式")
        assert _matching(findings, "缺少模板契约中的样式定义")

    @pytest.mark.parametrize("property_name", ["w:color", "w:spacing", "w:tblBorders"])
    def test_unmodelled_style_properties_are_checked(
        self, artifact: RenderFixture, property_name: str
    ) -> None:
        context = qa_context(artifact)
        part = context.pkg.find_part("word/styles.xml")
        assert part is not None
        known = {
            style.style_id
            for style in context.template.styles
            if property_name != "w:tblBorders" or style.type == "table"
        }
        style = next(
            style
            for style in iter_elements(part.read_tree, "w:style")
            if style.get("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}styleId")
            in known
        )
        properties = {"w:color": "w:rPr", "w:spacing": "w:pPr", "w:tblBorders": "w:tblPr"}
        parent = ensure_child_in_order(style, properties[property_name], ())
        changed = ensure_child_in_order(parent, property_name, ())
        if property_name == "w:tblBorders":
            changed = ensure_child_in_order(changed, "w:bottom", ())
        set_attr(changed, "w:after" if property_name == "w:spacing" else "w:val", "FF0000")
        assert _matching(_run("FR-M5-01", context), "偏离模板契约")

    def test_non_zero_indent_in_table_cell_is_still_flagged(self, artifact: RenderFixture) -> None:
        """单元格里的**非零**缩进照报：白名单只豁免"全零归零"这一种形态。"""
        context = qa_context(artifact)
        table = injected_tables(context)[0]
        cell = row_cells(table, 1)[0]
        paragraph = cell_paragraphs(cell)[0]
        indentation = add_paragraph_attribute(paragraph, "w:ind")
        set_attr(indentation, "w:left", "1134")  # 2 字符缩进

        findings = _run("FR-M5-01", context)
        assert _matching(findings, "直设格式"), "单元格上的非零缩进必须报出来"


def _perturb_one_style_size(context) -> str:
    """把产物里某个段落样式的 ``w:sz`` 改掉，返回该样式 ID。"""
    contract = {style.style_id for style in context.template.styles}
    part = context.pkg.find_part("word/styles.xml")
    assert part is not None
    for style in iter_elements(part.read_tree, "w:style"):
        style_id = find_first(style, "w:name")
        identifier = style.get(
            "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}styleId"
        )
        if style_id is None or identifier not in contract:
            continue
        size = find_first(find_first(style, "w:rPr"), "w:sz")
        if size is None:
            continue
        original = size.get("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}val")
        if original is None:
            continue
        set_attr(size, "w:val", str(int(original) + 4))
        return identifier
    pytest.skip("夹具里没有可改字号的段落样式")


# ------------------------------------------------------------------ FR-M5-02
class TestTemplateFidelity:
    def test_clean_artifact_reports_nothing(self, artifact: RenderFixture) -> None:
        """干净产物零发现——这条钉住"段落数"的计数口径。

        注入表格的单元格文字**也算内容**：只数 ``w:body`` 的直接子段落会漏掉整张
        表的文字，于是"IR 里写了、产物里也有"的行被判成缺失（段落数对不上）。
        """
        assert _run("FR-M5-02", qa_context(artifact)) == []

    def test_leftover_prototype_text_outside_tables_is_flagged(
        self, artifact: RenderFixture
    ) -> None:
        context = qa_context(artifact)
        target = content_paragraphs(context)[0]
        set_paragraph_text(target, "模板示例里的一句残留文字")

        findings = _run("FR-M5-02", context)
        leaked = _matching(findings, "不属于内容 IR")
        assert len(leaked) == 1
        assert "模板示例里的一句残留文字" in (leaked[0].evidence or "")

    def test_leftover_text_inside_a_table_cell_is_flagged(self, artifact: RenderFixture) -> None:
        """单元格里的残留同样必须被发现——这正是"下钻表格"那条修正的对立面。"""
        context = qa_context(artifact)
        table = injected_tables(context)[0]
        cell = row_cells(table, 1)[0]
        paragraph = cell_paragraphs(cell)[0]
        set_paragraph_text(paragraph, "单元格里的模板残留")

        findings = _run("FR-M5-02", context)
        assert _matching(findings, "不属于内容 IR"), "单元格里的残留文字必须被发现"

    def test_missing_paragraph_count_is_flagged(self, artifact: RenderFixture) -> None:
        """产物段落数少于 IR 预期 → 注入漏了（另一个方向，minor）。"""
        context = qa_context(artifact)
        table = injected_tables(context)[0]
        for paragraph in cell_paragraphs(table):
            set_paragraph_text(paragraph, "")

        findings = _run("FR-M5-02", context)
        assert _matching(findings, "少于内容 IR 预期")


# ------------------------------------------------------------------ FR-M5-03
class TestHeaderStyle:
    def test_clean_artifact_does_not_report_bold_or_shading(self, artifact: RenderFixture) -> None:
        """干净产物不得报"数据行误加粗"与"底纹残留"。

        夹具的表样式没有 ``firstRow`` 条件格式，故"表头加粗缺失"是预期的
        （那是模板体例问题，不是样式层被破坏）——这条也一并钉住，免得将来有人
        用"补直设加粗"的方式把它糊过去（那会被 FR-M5-01 判违规）。
        """
        findings = _run("FR-M5-03", qa_context(artifact))
        assert not _matching(findings, "数据行误加粗")
        assert not _matching(findings, "残留直设底纹")
        assert _matching(findings, "表头加粗缺失")

    def test_bold_in_a_data_row_is_flagged(self, artifact: RenderFixture) -> None:
        context = qa_context(artifact)
        table = injected_tables(context)[0]
        cell = row_cells(table, 1)[0]  # 第 0 行是表头，第 1 行起是数据行
        make_bold(cell)

        findings = _run("FR-M5-03", context)
        assert _matching(findings, "数据行误加粗")

    def test_residual_shading_is_flagged(self, artifact: RenderFixture) -> None:
        from lxml import etree

        from docfidelity.ooxml import ensure_child_in_order, qn

        context = qa_context(artifact)
        table = injected_tables(context)[0]
        cell = row_cells(table, 1)[0]
        cell_properties = ensure_child_in_order(
            cell, "w:tcPr", ("tcW", "gridSpan", "vMerge", "tcBorders", "shd")
        )
        shading = etree.SubElement(cell_properties, qn("w:shd"))
        set_attr(shading, "w:val", "clear")
        set_attr(shading, "w:fill", "DDDDDD")

        findings = _run("FR-M5-03", context)
        assert _matching(findings, "残留直设底纹")

    def test_front_matter_table_is_not_judged(self, artifact: RenderFixture) -> None:
        """修订记录表属前置版块，必须原样复用——改它的"表头加粗"会同时违反 FR-M5-02。"""
        context = qa_context(artifact)
        tables = front_matter_tables(context)
        assert tables, "夹具里应当有前置版块表格（修订记录）"
        make_bold(row_cells(tables[0], 1)[0])

        findings = _run("FR-M5-03", context)
        assert not _matching(findings, "数据行误加粗")


def test_front_matter_table_content_is_reusable(artifact: RenderFixture) -> None:
    """前置版块的表格文字不属于内容 IR，但不得被判成"模板残留"。"""
    context = qa_context(artifact)
    table = front_matter_tables(context)[0]
    sample = paragraph_text(cell_paragraphs(table)[0]).strip()
    assert sample, "前置版块表格里应有示例文字"
    # 前置版块不在 content_bounds 内，故不参与"产物文字必须来自 IR"的判定
    assert sample not in {
        paragraph_text(item).strip() for item in content_paragraphs(context, include_tables=True)
    }


def test_package_is_unchanged_between_contexts(artifact: RenderFixture) -> None:
    """就地改动只影响当前上下文：每次 ``qa_context`` 都从磁盘重新读包。

    这条守着整套测试的因果性——若包被跨用例共享，"某条规则报了红"就说不清是
    这次改动造成的，还是上一个用例留下的。
    """
    broken = qa_context(artifact)
    set_paragraph_text(content_paragraphs(broken)[0], "改坏了")
    assert _run("FR-M5-02", broken) != []
    assert _run("FR-M5-02", qa_context(artifact)) == []


def test_pkg_type_is_docx_package(artifact: RenderFixture) -> None:
    assert isinstance(qa_context(artifact).pkg, DocxPackage)
