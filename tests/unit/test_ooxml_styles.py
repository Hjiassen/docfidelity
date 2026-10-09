"""样式层单测（DD-4.2）。

最重要的一条：**``tblStyle`` 的映射命中数必须 > 0**。只验证段落样式重映射的测试
会假通过——产物报告"重映射成功"，而表格样式仍指向旧 styleId，Word 静默回退到
默认表格样式，表头掉色且不报错（1.2 第 8 条）。
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from lxml import etree

from docfidelity.ooxml import (
    DOCUMENT_PART,
    DocxPackage,
    ensure_child_in_order,
    ensure_paragraph_properties,
    find_first,
    qn,
    serialize,
    set_attr,
)
from docfidelity.ooxml.styles import (
    adopt_styles,
    align_style_definitions,
    clone_style_if_missing,
    detect_direct_formatting,
    read_styles,
    remap_style_ids,
    style_element_index,
    style_ids_by_name,
    used_styles,
    used_styles_in_package,
    validate_style_refs,
)
from docfidelity.render.order import P_PR_ORDER
from docfidelity.render.tables import TablePolicy, fix_cell_layout

pytestmark = pytest.mark.unit


class TestReadStyles:
    def test_reads_all_styles_with_stable_ids(self, sample_docx: Path) -> None:
        pkg = DocxPackage.open(sample_docx, read_only=True)
        styles = read_styles(pkg)
        assert len(styles) > 100
        identifiers = [style.style_id for style in styles]
        assert len(identifiers) == len(set(identifiers))

    def test_conditional_table_formatting_is_not_lost(self, sample_docx: Path) -> None:
        pkg = DocxPackage.open(sample_docx, read_only=True)
        table_style = next(s for s in read_styles(pkg) if s.style_id == "TableGrid")
        assert [item.type for item in table_style.tbl_style_pr] == ["firstRow"]
        conditional = table_style.tbl_style_pr[0]
        assert conditional.shading_fill == "D9E2F3"
        assert conditional.bold is True
        assert conditional.raw_xml is not None

    def test_east_asian_font_is_read_separately_from_latin(self, sample_docx: Path) -> None:
        pkg = DocxPackage.open(sample_docx, read_only=True)
        normal = next(s for s in read_styles(pkg) if s.style_id == "Normal")
        assert normal.font_latin == "Calibri"
        assert normal.font_east_asian == "等线"
        assert normal.size_pt == 10.5

    def test_raw_xml_keeps_unmodelled_properties(self, sample_docx: Path) -> None:
        """未建模的属性靠 raw_xml 保真——回写时以原件为准，不以模型为准。"""
        pkg = DocxPackage.open(sample_docx, read_only=True)
        table_style = next(s for s in read_styles(pkg) if s.style_id == "TableGrid")
        assert table_style.raw_xml is not None
        assert "tblBorders" in table_style.raw_xml

    def test_reading_styles_does_not_dirty_the_part(self, sample_docx: Path) -> None:
        pkg = DocxPackage.open(sample_docx, read_only=False)
        read_styles(pkg)
        assert not pkg.part("word/styles.xml").is_dirty

    def test_style_index_and_name_lookup_agree(self, sample_docx: Path) -> None:
        pkg = DocxPackage.open(sample_docx, read_only=True)
        assert set(style_element_index(pkg)) == {s.style_id for s in read_styles(pkg)}
        assert style_ids_by_name(pkg)["规范表格"] == "TblSpec"


class TestUsedStyles:
    def test_counts_paragraph_and_table_references_separately(self, sample_docx: Path) -> None:
        pkg = DocxPackage.open(sample_docx, read_only=True)
        used = used_styles_in_package(pkg)
        p_style = {item.style_id: item.count for item in used.p_style}
        tbl_style = {item.style_id: item.count for item in used.tbl_style}
        assert p_style["Heading1"] == 2
        assert p_style["Heading2"] == 1
        assert tbl_style == {"TableGrid": 1}
        assert used.is_used("TableGrid") and used.is_used("Heading1")
        assert not used.is_used("Heading9")

    def test_single_part_scan_does_not_fill_names(self, sample_docx: Path) -> None:
        pkg = DocxPackage.open(sample_docx, read_only=True)
        used = used_styles(pkg.part("word/document.xml"))
        assert used.tbl_style[0].style_id == "TableGrid"
        assert used.tbl_style[0].name is None


class TestRemapStyleIds:
    def test_table_style_reference_is_remapped(self, sample_docx: Path) -> None:
        """本文件的核心断言：``w:tblStyle`` 必须被改到。"""
        pkg = DocxPackage.open(sample_docx, read_only=False)
        report = remap_style_ids(pkg, {("Table Grid", "table"): "TblSpec"})
        assert report.tbl_style_hits >= 1, "表格样式未被重映射——只测段落样式会假通过"
        assert report.missing_refs == []
        assert pkg.part("word/document.xml").is_dirty

    def test_paragraph_and_table_hits_are_counted_separately(self, sample_docx: Path) -> None:
        pkg = DocxPackage.open(sample_docx, read_only=False)
        report = remap_style_ids(
            pkg, {("Table Grid", "table"): "TblSpec", ("heading 1", "paragraph"): "TblSpec"}
        )
        assert report.p_style_hits == 2
        assert report.tbl_style_hits == 1

    def test_zero_table_hits_raises_a_warning(self, docx_factory: Callable[..., Path]) -> None:
        """没有表格却声明了表格样式映射时，必须自己报出来。"""
        target = docx_factory("no-table", with_table=False)
        pkg = DocxPackage.open(target, read_only=False)
        report = remap_style_ids(pkg, {("Table Grid", "table"): "TblSpec"})
        assert report.tbl_style_hits == 0
        assert any("表格样式映射命中 0 次" in item for item in report.warnings)

    def test_unresolved_mapping_is_reported_not_ignored(self, sample_docx: Path) -> None:
        pkg = DocxPackage.open(sample_docx, read_only=False)
        report = remap_style_ids(pkg, {("不存在的样式", "paragraph"): "TblSpec"})
        assert report.unresolved_mappings == [("不存在的样式", "paragraph")]
        assert report.total_hits == 0

    def test_definition_pointers_follow_the_mapping(self, sample_docx: Path) -> None:
        """``w:basedOn`` 也要跟着改，否则重映射后的样式继承链会断。"""
        pkg = DocxPackage.open(sample_docx, read_only=False)
        report = remap_style_ids(pkg, {("Table Grid", "table"): "TblSpec"})
        assert report.definition_pointers_updated >= 1, "TblSpec 的 basedOn 指向 TableGrid"


class TestValidateStyleRefs:
    def test_clean_template_has_no_missing_reference(self, sample_docx: Path) -> None:
        pkg = DocxPackage.open(sample_docx, read_only=True)
        assert validate_style_refs(pkg) == []

    def test_missing_reference_is_detected(self, sample_docx: Path) -> None:
        from lxml import etree

        pkg = DocxPackage.open(sample_docx, read_only=False)
        body = pkg.part("word/document.xml").tree.find(
            "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}body"
        )
        assert body is not None
        paragraph = etree.SubElement(
            body, "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}p"
        )
        properties = etree.SubElement(
            paragraph, "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}pPr"
        )
        style = etree.SubElement(
            properties, "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}pStyle"
        )
        style.set("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}val", "GhostStyle")
        assert validate_style_refs(pkg) == ["GhostStyle"]


class TestDirectFormatting:
    def test_detects_paragraph_and_run_level_direct_formatting(self, sample_docx: Path) -> None:
        pkg = DocxPackage.open(sample_docx, read_only=True)
        findings = detect_direct_formatting(pkg)
        evidence = {item.evidence for item in findings}
        assert "jc" in evidence
        assert "b" in evidence
        assert all(item.rule_id == "FR-M5-01" for item in findings)

    def test_styled_paragraphs_are_not_reported(self, sample_docx: Path) -> None:
        """纯样式段落不得误报：误报会把真实问题淹掉。"""
        pkg = DocxPackage.open(sample_docx, read_only=True)
        findings = detect_direct_formatting(pkg)
        assert len(findings) == 2

    def test_toc_field_characters_are_not_direct_formatting(self, sample_docx: Path) -> None:
        """目录条目里的域字符属于结构，不属于"直设格式"。"""
        pkg = DocxPackage.open(sample_docx, read_only=True)
        findings = detect_direct_formatting(pkg)
        assert all("fldChar" not in (item.evidence or "") for item in findings)
        assert all("instrText" not in (item.evidence or "") for item in findings)


class TestCloneAndAlign:
    def test_reuse_wins_over_creating(self, sample_docx: Path) -> None:
        pkg = DocxPackage.open(sample_docx, read_only=False)
        existing = next(s for s in read_styles(pkg) if s.style_id == "TableGrid")
        result = clone_style_if_missing(pkg, existing)
        assert result.created is False
        assert result.style_id == "TableGrid"
        assert not pkg.part("word/styles.xml").is_dirty

    def test_reuse_matches_by_name_and_type_not_by_id(self, sample_docx: Path) -> None:
        """同一体例在不同模板里 styleId 不同，显示名才稳定。"""
        pkg = DocxPackage.open(sample_docx, read_only=False)
        reference = next(s for s in read_styles(pkg) if s.style_id == "TblSpec")
        renamed = reference.model_copy(update={"style_id": "SomeOtherId"})
        result = clone_style_if_missing(pkg, renamed)
        assert result.created is False
        assert result.style_id == "TblSpec"

    def test_missing_style_is_created_with_a_free_id(self, sample_docx: Path) -> None:
        pkg = DocxPackage.open(sample_docx, read_only=False)
        reference = next(s for s in read_styles(pkg) if s.style_id == "TblSpec")
        fresh = reference.model_copy(update={"style_id": "BrandNew", "name": "品牌表格"})
        result = clone_style_if_missing(pkg, fresh)
        assert result.created is True
        assert result.style_id == "BrandNew"
        assert "BrandNew" in style_element_index(pkg)

    def test_id_collision_is_renamed_and_reported(self, sample_docx: Path) -> None:
        pkg = DocxPackage.open(sample_docx, read_only=False)
        reference = next(s for s in read_styles(pkg) if s.style_id == "TblSpec")
        colliding = reference.model_copy(update={"style_id": "TableGrid", "name": "另一个表格"})
        result = clone_style_if_missing(pkg, colliding)
        assert result.created is True
        assert result.style_id.startswith("TableGrid_df")
        assert result.renamed_from == "TableGrid"

    def test_align_keeps_style_id_and_syncs_definition(self, sample_docx: Path) -> None:
        from docfidelity.ooxml.doc import qn

        pkg = DocxPackage.open(sample_docx, read_only=False)
        reference = read_styles(pkg)

        normal = style_element_index(pkg)["Normal"]
        size = normal.find(f"{qn('w:rPr')}/{qn('w:sz')}")
        assert size is not None
        size.set(qn("w:val"), "99")

        report = align_style_definitions(pkg, reference)
        assert "Normal" in report.updated
        restored = style_element_index(pkg)["Normal"]
        assert restored.get(qn("w:styleId")) == "Normal"
        assert restored.find(f"{qn('w:rPr')}/{qn('w:sz')}").get(qn("w:val")) == "21"  # type: ignore[union-attr]

    def test_align_reports_styles_absent_from_target(self, sample_docx: Path) -> None:
        pkg = DocxPackage.open(sample_docx, read_only=False)
        reference = read_styles(pkg)
        missing = reference[0].model_copy(update={"style_id": "NotThereAtAll"})
        report = align_style_definitions(pkg, [*reference, missing])
        assert report.missing_in_target == ["NotThereAtAll"]


class TestAdoptStyles:
    def test_clone_then_remap_order_is_respected(self, sample_docx: Path) -> None:
        """先补定义再改引用：反过来会留下"引用指向不存在的样式"的产物。"""
        pkg = DocxPackage.open(sample_docx, read_only=False)
        reference = next(s for s in read_styles(pkg) if s.style_id == "TblSpec")
        fresh = reference.model_copy(update={"style_id": "Adopted", "name": "引入表格"})
        report = adopt_styles(pkg, [fresh], {("Table Grid", "table"): "Adopted"})
        assert report.created_style_ids == ["Adopted"]
        assert report.remap.tbl_style_hits >= 1
        assert report.remap.missing_refs == []


class TestCellIndentNormalization:
    """``fix_cell_layout`` 写入的**全零**缩进不算直设格式（FR-M5-01 / FR-M4-08）。

    渲染层按 DD-9.4 在单元格段落上写 ``w:ind/@w:left="0"`` / ``@w:firstLine="0"``，
    目的是清零从 ``Normal`` 继承来的缩进、避免换字号时单元格内容跑版。那是引擎
    **声明过**的版式归一，不是"没下沉到样式的残留"——把它计入直设格式，会让每一份
    带注入表格的产物都刷出 blocker，而报告指向的是引擎自己的正确行为。

    判据刻意收紧：只有"段落位于表格单元格内"**且**"``w:ind`` 的每个已设属性都是
    零值"才豁免。故这里同时钉住两端。
    """

    def _build(self) -> tuple[DocxPackage, object, object, object]:
        """一个含"单元格段落 + 正文段落"的最小包。

        返回的元素取自**包自己的树**：``from_parts`` 把部件内容存成字节，之后再改
        构造期的临时元素改不到包里——那会让用例"改了却没生效"，从而把一条真实的
        检测失败伪装成"白名单生效了"。
        """
        document = etree.Element(qn("w:document"))
        body = etree.SubElement(document, qn("w:body"))
        table = etree.SubElement(body, qn("w:tbl"))
        cell = etree.SubElement(etree.SubElement(table, qn("w:tr")), qn("w:tc"))
        etree.SubElement(cell, qn("w:p"))
        etree.SubElement(body, qn("w:p"))
        pkg = DocxPackage.from_parts({DOCUMENT_PART: serialize(document)}, read_only=False)

        root = pkg.part(DOCUMENT_PART).read_tree
        table = next(root.iter(qn("w:tbl")))
        cell = next(table.iter(qn("w:tc")))
        cell_paragraph = find_first(cell, "w:p")
        body_paragraph = next(
            item
            for item in table.getparent().iter(qn("w:p"))
            if item.getparent() is table.getparent()
        )
        return pkg, table, cell_paragraph, body_paragraph

    def _indent(self, paragraph: object) -> object:
        return ensure_child_in_order(ensure_paragraph_properties(paragraph), "w:ind", P_PR_ORDER)

    def test_engine_zero_indent_inside_a_cell_is_not_direct_formatting(self) -> None:
        """渲染层的 ``fix_cell_layout`` 写的全零缩进必须被豁免。"""
        pkg, table, _cell_paragraph, _body = self._build()
        cell = next(table.iter(qn("w:tc")))
        assert fix_cell_layout(cell, policy=TablePolicy()) == 1

        assert detect_direct_formatting(pkg) == [], "引擎自己写的全零缩进归零不该被判成直设格式"

    def test_non_zero_indent_inside_a_cell_is_still_direct_formatting(self) -> None:
        pkg, _table, cell_paragraph, _body = self._build()
        set_attr(self._indent(cell_paragraph), "w:left", "1134")
        assert any("ind" in (item.evidence or "") for item in detect_direct_formatting(pkg))

    def test_zero_indent_on_a_body_paragraph_is_still_direct_formatting(self) -> None:
        """白名单只认单元格：正文段上的零值缩进照样表达"覆盖样式的首行缩进"。"""
        pkg, _table, _cell, body_paragraph = self._build()
        set_attr(self._indent(body_paragraph), "w:firstLine", "0")
        assert any("ind" in (item.evidence or "") for item in detect_direct_formatting(pkg))
