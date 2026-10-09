"""模板指纹单测（DD-7.4 / DD-4.7，FR-M2-06 / FR-M2-18）。

这组测试守的是两条互相对立的承诺，必须同时成立：

* **同内容 → 同指纹**：否则每次构建都报满屏假差异，所有人开始无视 diff
  （比"没有 diff"更危险）。
* **真差异 → 指纹必须变**：否则"版本可比"是假的，换了模板却报"无变化"。

只测其中一条都能轻易通过，所以两条都有。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from lxml import etree

from docfidelity.contracts.template import TemplatePackage
from docfidelity.ooxml import DocxPackage
from docfidelity.platform.hashing import combine_digests
from docfidelity.registry import (
    FINGERPRINT_SECTIONS,
    SOURCE_SECTION,
    ingest_template,
    read_block_assets,
    read_package,
    section_payloads,
    source_digest,
    verify_fingerprint,
)
from tests.fixtures.docx_builder import W, rewrite_part, shuffle_volatile_identifiers

pytestmark = pytest.mark.unit

_DOCUMENT = "word/document.xml"
_STYLES = "word/styles.xml"
_REF = "TPL-1@1.0"


def _ingest(source: Path, version: str = "1.0") -> TemplatePackage:
    result = ingest_template(source, template_id="TPL-1", version=version)
    assert result.ok, [outcome for outcome in result.stage_outcomes if not outcome.ok]
    assert result.package is not None
    return result.package


def _sections(package: TemplatePackage) -> dict[str, str]:
    assert package.fingerprint is not None, "入库必须产出指纹"
    return dict(package.fingerprint.sections)


def _bump_heading_size(root: etree._Element) -> None:
    """把 Heading1 的字号从 14pt 改成 16pt（`w:sz` 半磅）。"""
    for style in root.iter(f"{{{W}}}style"):
        if style.get(f"{{{W}}}styleId") != "Heading1":
            continue
        for size in style.iter(f"{{{W}}}sz"):
            size.set(f"{{{W}}}val", "32")


def _recolour_first_row_shading(root: etree._Element) -> None:
    """改表头条件格式的底纹——正是"看起来样式没变、实际表头掉色"的那一处。"""
    for conditions in root.iter(f"{{{W}}}tblStylePr"):
        if conditions.get(f"{{{W}}}type") != "firstRow":
            continue
        for shading in conditions.iter(f"{{{W}}}shd"):
            shading.set(f"{{{W}}}fill", "FF0000")


def _rename_revision_author(root: etree._Element) -> None:
    """改一个普通表格单元格里的文字（既不是标题、也不是占位词）。"""
    for text in root.iter(f"{{{W}}}t"):
        if text.text == "张三":
            text.text = "李四"


def _style_size(package: TemplatePackage, style_id: str) -> float | None:
    style = package.style_by_id(style_id)
    assert style is not None, f"夹具缺少样式 {style_id}"
    return style.size_pt


def _shading(package: TemplatePackage, style_id: str, condition: str) -> str | None:
    style = package.style_by_id(style_id)
    assert style is not None, f"夹具缺少样式 {style_id}"
    for item in style.tbl_style_pr:
        if item.type == condition:
            return item.shading_fill
    return None


class TestSectionBoundaries:
    def test_every_declared_section_has_a_payload(self, template_docx: Path) -> None:
        payloads = section_payloads(_ingest(template_docx))
        assert set(FINGERPRINT_SECTIONS) <= set(payloads)

    def test_no_undeclared_section_leaks_into_the_payloads(self, template_docx: Path) -> None:
        """反向断言：多算了内容却没登记成分区，就不会有人去看它。"""
        payloads = section_payloads(_ingest(template_docx))
        assert set(payloads) <= set(FINGERPRINT_SECTIONS)

    def test_digest_is_the_combination_of_its_own_sections(self, template_docx: Path) -> None:
        sections = _sections(_ingest(template_docx))
        assert combine_digests(sections) == _ingest(template_docx).fingerprint.digest  # type: ignore[union-attr]

    def test_overall_digest_ignores_section_order(self, template_docx: Path) -> None:
        """分区呈现顺序变了不该被读成"模板变了"。"""
        package = _ingest(template_docx)
        assert package.fingerprint is not None
        reversed_sections = dict(reversed(list(package.fingerprint.sections.items())))
        assert combine_digests(reversed_sections) == package.fingerprint.digest

    def test_missing_source_digest_omits_the_section_instead_of_blanking_it(
        self, template_docx: Path
    ) -> None:
        """没有原件时**不写** ``source``，而不是写空串。"""
        package = _ingest(template_docx)
        stripped = package.model_copy(update={"fingerprint": None})
        computed = __import__(
            "docfidelity.registry.fingerprint", fromlist=["compute_fingerprint"]
        ).compute_fingerprint(stripped)
        assert SOURCE_SECTION not in computed.sections

    def test_blocks_section_covers_binary_assets(self, template_docx: Path) -> None:
        """换掉 ``blocks/`` 里的资产必须改变 ``blocks`` 分区。

        只算索引的话，"封面图换了一张、索引一个字没变"会被判成版块分区没变，
        而那是最容易被忽视、又最影响外观的一类变更。
        """
        package = _ingest(template_docx)
        without = section_payloads(package, block_assets={})
        with_asset = section_payloads(package, block_assets={"cover.png": b"\x89PNG-data"})
        assert with_asset["blocks"] != without["blocks"]
        assert "blocks" in FINGERPRINT_SECTIONS


class TestStability:
    def test_same_source_yields_the_same_fingerprint(self, template_docx: Path) -> None:
        assert _sections(_ingest(template_docx)) == _sections(_ingest(template_docx))

    def test_resaving_the_template_does_not_change_the_fingerprint(
        self, template_docx: Path, tmp_path: Path
    ) -> None:
        """rsid / 书签名 / 关系 ID / docId / 时间戳全变，指纹必须不变（FR-M2-18）。"""
        original = _ingest(template_docx)
        resaved_source = shuffle_volatile_identifiers(template_docx, tmp_path / "resaved.docx")
        resaved = _ingest(resaved_source)

        # 防假通过：先证明这个对照件**真的**被动过，否则"指纹没变"可能只是因为
        # 扰动没生效——那种绿什么都证明不了。
        assert resaved.manifest.source_hash != original.manifest.source_hash
        assert _sections(resaved) == _sections(original)
        assert resaved.fingerprint is not None and original.fingerprint is not None
        assert resaved.fingerprint.digest == original.fingerprint.digest

    def test_body_text_change_only_moves_the_source_section(
        self, template_docx: Path, tmp_path: Path
    ) -> None:
        """正文文字改了、契约没变 —— 这正是 ``source`` 分区存在的理由。

        没有它，这类改动在版本 diff 里是完全不可见的（``manifest.source_hash``
        不能拿来比：Word 每次保存都会变，比它只会满屏假差异）。
        """
        changed_source = rewrite_part(
            template_docx,
            tmp_path / "renamed.docx",
            _DOCUMENT,
            _rename_revision_author,
        )
        before = _sections(_ingest(template_docx))
        after = _sections(_ingest(changed_source))

        for section in FINGERPRINT_SECTIONS:
            assert before[section] == after[section], f"{section} 分区不该变"
        assert before[SOURCE_SECTION] != after[SOURCE_SECTION]

    def test_style_change_moves_the_styles_section(
        self, template_docx: Path, tmp_path: Path
    ) -> None:
        changed_source = rewrite_part(
            template_docx, tmp_path / "resized.docx", _STYLES, _bump_heading_size
        )
        before = _ingest(template_docx)
        after = _ingest(changed_source)
        assert _style_size(after, "Heading1") != _style_size(before, "Heading1")
        assert _sections(after)["styles"] != _sections(before)["styles"]
        assert _sections(after)["page_setup"] == _sections(before)["page_setup"]

    def test_table_conditional_format_change_moves_the_styles_section(
        self, sample_docx: Path, tmp_path: Path
    ) -> None:
        """表头底纹改了，``styles`` 分区必须变。

        条件格式（``w:tblStylePr``）是"克隆样式时最容易丢、丢了又看不出来"的东西，
        它必须落在摘要覆盖范围内。
        """
        changed_source = rewrite_part(
            sample_docx, tmp_path / "shading.docx", _STYLES, _recolour_first_row_shading
        )
        before = _ingest(sample_docx)
        after = _ingest(changed_source)
        assert _shading(after, "TableGrid", "firstRow") != _shading(before, "TableGrid", "firstRow")
        assert _sections(after)["styles"] != _sections(before)["styles"]

    def test_source_digest_is_the_normalized_one(self, template_docx: Path, tmp_path: Path) -> None:
        """``source`` 分区走归一化，**不是**原始字节摘要。"""
        original = _ingest(template_docx)
        resaved_source = shuffle_volatile_identifiers(template_docx, tmp_path / "resaved.docx")
        assert source_digest(DocxPackage.open(resaved_source, read_only=True)) == source_digest(
            DocxPackage.open(template_docx, read_only=True)
        )
        assert _sections(original)[SOURCE_SECTION] == source_digest(
            DocxPackage.open(template_docx, read_only=True)
        )


class TestVerify:
    def test_a_freshly_ingested_package_passes(self, template_docx: Path) -> None:
        package = _ingest(template_docx)
        assert verify_fingerprint(package, block_assets={}) == []

    def test_round_tripped_package_passes(self, template_docx: Path, tmp_path: Path) -> None:
        """从包目录读回来仍然自洽：指纹落盘后能重新校验（DD-7.4 的可用前提）。"""
        source = tmp_path / "template.docx"
        source.write_bytes(template_docx.read_bytes())
        result = ingest_template(
            source, template_id="TPL-1", version="1.0", target_dir=tmp_path / "TPL-1@1.0"
        )
        assert result.ok
        package = read_package(tmp_path / "TPL-1@1.0")
        assert (
            verify_fingerprint(package, block_assets=read_block_assets(tmp_path / "TPL-1@1.0"))
            == []
        )

    def test_missing_fingerprint_is_reported(self, template_docx: Path) -> None:
        package = _ingest(template_docx).model_copy(update={"fingerprint": None})
        problems = verify_fingerprint(package, block_assets={})
        assert any("没有指纹" in problem for problem in problems)

    def test_a_tampered_digest_is_reported(self, template_docx: Path) -> None:
        """只改 ``digest`` 不改 ``sections`` —— 不自洽必须被抓到。"""
        package = _ingest(template_docx)
        assert package.fingerprint is not None
        tampered = package.fingerprint.model_copy(update={"digest": "0" * 64})
        problems = verify_fingerprint(
            package.model_copy(update={"fingerprint": tampered}), block_assets={}
        )
        assert any("不自洽" in problem for problem in problems)

    def test_section_mismatch_is_reported(self, template_docx: Path) -> None:
        """包内容被改过（指纹没跟着更新）必须被抓到。"""
        package = _ingest(template_docx)
        moved = package.page_setup.margins.model_copy(update={"left_twips": 9999})
        broken = package.model_copy(
            update={"page_setup": package.page_setup.model_copy(update={"margins": moved})}
        )
        problems = verify_fingerprint(broken, block_assets={})
        assert any("page_setup" in problem for problem in problems)

    def test_missing_and_unknown_sections_are_reported(self, template_docx: Path) -> None:
        package = _ingest(template_docx)
        assert package.fingerprint is not None
        sections = dict(package.fingerprint.sections)
        sections.pop("placeholders")
        sections["bogus"] = "x"
        tampered = package.fingerprint.model_copy(update={"sections": sections})
        problems = verify_fingerprint(
            package.model_copy(update={"fingerprint": tampered}), block_assets={}
        )
        assert any("缺" in problem and "placeholders" in problem for problem in problems)
        assert any("未知分区" in problem and "bogus" in problem for problem in problems)

    def test_source_mismatch_is_reported(self, template_docx: Path) -> None:
        package = _ingest(template_docx)
        problems = verify_fingerprint(package, block_assets={}, source_digest_value="deadbeef")
        assert any("原件" in problem for problem in problems)

    def test_tampered_fingerprint_file_on_disk_is_caught(
        self, template_docx: Path, tmp_path: Path
    ) -> None:
        """端到端：``fingerprint.json`` 不参与 ``package_hash``，所以改它不会被
        ``read_package`` 拦下 —— 拦住它的必须是自洽校验。

        这条断言是那处"排除"得以成立的前提：排除的理由是"不会有内容被改过而
        无人发现"，如果自洽校验不存在，这个理由就不成立。
        """
        source = tmp_path / "template.docx"
        source.write_bytes(template_docx.read_bytes())
        result = ingest_template(
            source, template_id="TPL-1", version="1.0", target_dir=tmp_path / _REF
        )
        assert result.ok
        directory = tmp_path / _REF
        fingerprint_path = directory / "fingerprint.json"
        payload = fingerprint_path.read_text(encoding="utf-8").replace('"styles"', '"STYLES"')
        fingerprint_path.write_text(payload, encoding="utf-8")

        package = read_package(directory)
        problems = verify_fingerprint(package, block_assets=read_block_assets(directory))
        assert problems, "改过 fingerprint.json 却校验通过"
