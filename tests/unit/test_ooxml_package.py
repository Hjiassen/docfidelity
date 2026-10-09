"""包与部件的单测（DD-4.1）。

核心断言是那条**字节级纪律**：只读过、没改过的部件在保存后必须一个字节都不变。
它是"样式层零改动"在文件层的落地，也是唯一能挡住"顺手重排一遍 XML"的检查。
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from docfidelity.contracts.errors import EngineError
from docfidelity.ooxml.package import (
    DOCUMENT_PART,
    STYLES_PART,
    DocxPackage,
    Relationship,
    content_type_for_extension,
    is_content_part,
    rels_path_for,
    resolve_target_path,
)

pytestmark = pytest.mark.unit


def _payloads(path: Path) -> dict[str, bytes]:
    with zipfile.ZipFile(path) as archive:
        return {
            info.filename: archive.read(info.filename)
            for info in archive.infolist()
            if not info.is_dir()
        }


class TestOpen:
    def test_open_reads_parts_lazily(self, sample_docx: Path) -> None:
        pkg = DocxPackage.open(sample_docx, read_only=True)
        assert pkg.has_part(DOCUMENT_PART)
        assert pkg.has_part(STYLES_PART)
        assert not pkg.part(DOCUMENT_PART).is_loaded, "打开时不应解析任何 XML"

    def test_missing_part_raises_package_incomplete(self, sample_docx: Path) -> None:
        pkg = DocxPackage.open(sample_docx, read_only=True)
        with pytest.raises(EngineError) as info:
            pkg.part("word/not-there.xml")
        assert info.value.code == "E_PACKAGE_INCOMPLETE"

    def test_missing_file_raises_template_unreadable(self, tmp_path: Path) -> None:
        with pytest.raises(EngineError) as info:
            DocxPackage.open(tmp_path / "absent.docx")
        assert info.value.code == "E_TEMPLATE_UNREADABLE"

    def test_non_zip_raises_template_unreadable(self, tmp_path: Path) -> None:
        broken = tmp_path / "broken.docx"
        broken.write_bytes(b"not a zip at all")
        with pytest.raises(EngineError) as info:
            DocxPackage.open(broken)
        assert info.value.code == "E_TEMPLATE_UNREADABLE"

    def test_content_type_from_override_and_default(self, sample_docx: Path) -> None:
        pkg = DocxPackage.open(sample_docx, read_only=True)
        assert pkg.content_type_of(DOCUMENT_PART).endswith("wordprocessingml.document.main+xml")
        assert pkg.content_type_of("word/numbering.xml").endswith("numbering+xml")

    def test_content_part_classification(self) -> None:
        assert is_content_part("word/document.xml")
        assert is_content_part("word/header1.xml")
        assert is_content_part("word/footnotes.xml")
        assert not is_content_part("word/styles.xml")
        assert not is_content_part("word/_rels/document.xml.rels")

    def test_rels_path_derivation(self) -> None:
        assert rels_path_for("") == "_rels/.rels"
        assert rels_path_for("word/document.xml") == "word/_rels/document.xml.rels"
        assert rels_path_for("document.xml") == "_rels/document.xml.rels"


class TestRelationships:
    def test_resolve_styles_relationship(self, sample_docx: Path) -> None:
        pkg = DocxPackage.open(sample_docx, read_only=True)
        resolved = pkg.resolve_single(DOCUMENT_PART, role="styles")
        assert resolved is not None
        assert resolved.part is not None
        assert resolved.target_path == STYLES_PART

    def test_reading_relationships_does_not_dirty_parts(self, sample_docx: Path) -> None:
        """读关系是最容易"顺手置脏"的路径：它要解析 rels 部件但承诺不改它。"""
        pkg = DocxPackage.open(sample_docx, read_only=False)
        pkg.relationships(DOCUMENT_PART)
        pkg.relationships("")
        assert not pkg.part(rels_path_for(DOCUMENT_PART)).is_dirty
        assert not pkg.part(STYLES_PART).is_dirty
        assert not pkg.part(DOCUMENT_PART).is_dirty

    def test_relative_target_is_resolved_against_owner_directory(self) -> None:
        relationship = Relationship(rel_id="rId1", type="x/y", target="numbering.xml")
        assert resolve_target_path("word/document.xml", relationship) == "word/numbering.xml"

    def test_parent_segments_are_normalized(self) -> None:
        relationship = Relationship(rel_id="rId1", type="x/y", target="../media/a.png")
        assert resolve_target_path("word/charts/chart1.xml", relationship) == "word/media/a.png"

    def test_absolute_target_is_package_rooted(self) -> None:
        relationship = Relationship(
            rel_id="rId1", type="x/y", target="/word/media/a.png", target_mode="Internal"
        )
        assert resolve_target_path("word/document.xml", relationship) == "word/media/a.png"

    def test_external_target_is_left_alone(self) -> None:
        relationship = Relationship(
            rel_id="rId1", type="hyperlink", target="https://example.com", target_mode="External"
        )
        assert resolve_target_path("word/document.xml", relationship) == "https://example.com"


class TestSaveDiscipline:
    def test_read_then_save_is_byte_identical(self, sample_docx: Path, tmp_path: Path) -> None:
        """只读过 → 保存后每个部件字节全等。"""
        pkg = DocxPackage.open(sample_docx, read_only=False)
        for part in pkg.parts("xml"):
            _ = part.read_tree  # 纯读路径，不得置脏
        target = pkg.save(tmp_path / "roundtrip.docx")

        original = _payloads(sample_docx)
        rewritten = _payloads(target)
        assert set(original) == set(rewritten)
        differing = [name for name in original if original[name] != rewritten[name]]
        assert differing == [], f"未修改的部件字节发生了变化：{differing}"

    def test_touching_one_part_leaves_others_untouched(
        self, sample_docx: Path, tmp_path: Path
    ) -> None:
        pkg = DocxPackage.open(sample_docx, read_only=False)
        styles = pkg.part(STYLES_PART)
        _ = styles.tree  # 走可变访问路径 → 置脏
        target = pkg.save(tmp_path / "touched.docx")

        original = _payloads(sample_docx)
        rewritten = _payloads(target)
        assert rewritten[DOCUMENT_PART] == original[DOCUMENT_PART]
        assert rewritten[rels_path_for("")] == original[rels_path_for("")]

    def test_save_is_atomic_and_leaves_no_temp_file(
        self, sample_docx: Path, tmp_path: Path
    ) -> None:
        pkg = DocxPackage.open(sample_docx, read_only=False)
        target = pkg.save(tmp_path / "atomic.docx")
        assert target.is_file()
        assert list(tmp_path.glob("*.dftmp")) == []

    def test_read_only_package_refuses_to_write(self, sample_docx: Path, tmp_path: Path) -> None:
        pkg = DocxPackage.open(sample_docx, read_only=True)
        with pytest.raises(EngineError):
            pkg.save(tmp_path / "should-not-exist.docx")
        assert not (tmp_path / "should-not-exist.docx").exists()


class TestAddParts:
    def test_add_binary_part_registers_image_content_type(self, sample_docx: Path) -> None:
        from tests.fixtures.docx_builder import png_bytes

        pkg = DocxPackage.open(sample_docx, read_only=False)
        pkg.add_binary_part("word/media/new.png", "image/png", png_bytes(4, 3))
        assert pkg.has_part("word/media/new.png")

    def test_ensure_relationship_is_idempotent(self, sample_docx: Path) -> None:
        pkg = DocxPackage.open(sample_docx, read_only=False)
        first = pkg.ensure_relationship("", type="x/y", target="word/document.xml")
        second = pkg.ensure_relationship("", type="x/y", target="word/document.xml")
        assert first.rel_id == second.rel_id

    def test_attach_relationship_allocates_unique_id(self, sample_docx: Path) -> None:
        pkg = DocxPackage.open(sample_docx, read_only=False)
        existing = {item.rel_id for item in pkg.relationships(DOCUMENT_PART)}
        created = pkg.attach_relationship(DOCUMENT_PART, type="x/y", target="word/media/a.png")
        assert created.rel_id not in existing
        assert created.rel_id in {item.rel_id for item in pkg.relationships(DOCUMENT_PART)}

    def test_clone_part_does_not_mutate_source(self, sample_docx: Path) -> None:
        pkg = DocxPackage.open(sample_docx, read_only=False)
        clone = pkg.clone_part(STYLES_PART)
        _ = clone.tree  # 改克隆体，源部件不得受影响
        assert not pkg.part(STYLES_PART).is_dirty

    def test_content_type_for_extension_falls_back(self) -> None:
        assert content_type_for_extension("PNG") == "image/png"
        assert content_type_for_extension("weird") == "application/octet-stream"
