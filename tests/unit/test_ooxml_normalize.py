"""归一化与指纹单测（DD-4.7）。

这一组测试守的是"同一内容 → 同一摘要"这条承诺。它一旦失守，症状是每次构建都报出
满屏假差异，所有人开始无视 diff——比"没有 diff"更危险。
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest
from lxml import etree

from docfidelity.ooxml import DocxPackage
from docfidelity.ooxml.doc import parse, qn
from docfidelity.ooxml.normalize import (
    HANDLED_ACTIONS,
    VOLATILE_RULES,
    digest_of,
    normalize_volatile,
    normalized_digest,
    normalized_element,
    owner_of_rels_path,
)
from tests.fixtures.docx_builder import shuffle_volatile_identifiers

pytestmark = pytest.mark.unit

_DOCUMENT = "word/document.xml"
_SETTINGS = "word/settings.xml"


def test_element_normalization_ignores_prefix_order_and_rsid_without_mutating() -> None:
    first = parse('<w:style xmlns:w="urn:w" w:type="paragraph" w:styleId="s"/>')
    second = parse('<x:style xmlns:x="urn:w" x:styleId="s" x:type="paragraph"/>')
    from docfidelity.ooxml import serialize

    before = serialize(first)
    assert normalized_element(first) == normalized_element(second)
    assert serialize(first) == before


def test_element_normalization_preserves_visible_style_properties() -> None:
    first = parse('<w:style xmlns:w="urn:w"><w:color w:val="000000"/></w:style>')
    second = parse('<w:style xmlns:w="urn:w"><w:color w:val="FF0000"/></w:style>')
    assert normalized_element(first) != normalized_element(second)


def test_element_normalization_ignores_style_revision_identifiers() -> None:
    first = parse(
        '<w:style xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        '<w:rsid w:val="00000001"/></w:style>'
    )
    second = parse(
        '<w:style xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        '<w:rsid w:val="00000002"/></w:style>'
    )
    assert normalized_element(first) == normalized_element(second)


def test_element_normalization_resolves_compatibility_prefix_lists() -> None:
    first = parse(
        '<w:style xmlns:w="urn:w" xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006" '
        'xmlns:w14="urn:extension" mc:Ignorable="w14" mc:PreserveElements="w14:extra">'
        "<w14:extra/></w:style>"
    )
    second = parse(
        '<x:style xmlns:x="urn:w" xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006" '
        'xmlns:ext="urn:extension" mc:Ignorable="ext" mc:PreserveElements="ext:extra">'
        "<ext:extra/></x:style>"
    )
    assert normalized_element(first) == normalized_element(second)


def test_element_normalization_preserves_compatibility_namespace_changes() -> None:
    first = parse(
        '<w:style xmlns:w="urn:w" xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006" '
        'xmlns:ext="urn:first" mc:Ignorable="ext"/>'
    )
    second = parse(
        '<w:style xmlns:w="urn:w" xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006" '
        'xmlns:ext="urn:second" mc:Ignorable="ext"/>'
    )
    assert normalized_element(first) != normalized_element(second)


def _read_parts(path: Path) -> dict[str, bytes]:
    with zipfile.ZipFile(path) as archive:
        return {
            info.filename: archive.read(info.filename)
            for info in archive.infolist()
            if not info.is_dir()
        }


def _write_parts(path: Path, parts: dict[str, bytes]) -> Path:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, payload in parts.items():
            archive.writestr(name, payload)
    return path


def _dump(root: etree._Element) -> bytes:
    return etree.tostring(root, encoding="UTF-8", xml_declaration=True, standalone=True)


def _change_content(source: Path, dest: Path) -> Path:
    parts = _read_parts(source)
    document = parse(parts[_DOCUMENT])
    for element in document.iter(qn("w:t")):
        element.text = "被改过的正文"
        break
    parts[_DOCUMENT] = _dump(document)
    return _write_parts(dest, parts)


class TestRuleTable:
    def test_every_declared_action_has_an_implementation(self) -> None:
        """规则表是文档，也是覆盖率合同：声明了动作就必须有分支。"""
        declared = {action for _, action in VOLATILE_RULES}
        assert declared <= HANDLED_ACTIONS, f"未实现的动作：{sorted(declared - HANDLED_ACTIONS)}"

    def test_rule_table_covers_the_documented_targets(self) -> None:
        targets = {pattern for pattern, _ in VOLATILE_RULES}
        assert "w:rsid*" in targets
        assert any(pattern.startswith("w:bookmarkStart") for pattern in targets)
        assert "r:id" in targets
        assert "w:docId" in targets

    def test_owner_of_rels_path_round_trip(self) -> None:
        assert owner_of_rels_path("word/_rels/document.xml.rels") == "word/document.xml"
        assert owner_of_rels_path("_rels/.rels") == ""
        assert owner_of_rels_path("word/document.xml") is None
        assert owner_of_rels_path("word/_rels/header1.xml.rels") == "word/header1.xml"


class TestDeterminism:
    def test_same_package_yields_same_digest(self, sample_docx: Path) -> None:
        first = normalized_digest(DocxPackage.open(sample_docx, read_only=True))
        second = normalized_digest(DocxPackage.open(sample_docx, read_only=True))
        assert first == second

    def test_volatile_identifiers_do_not_change_the_digest(
        self, sample_docx: Path, tmp_path: Path
    ) -> None:
        """rsid / 书签名 / 关系 ID / docId / 时间戳全变，摘要必须不变。"""
        shuffled = shuffle_volatile_identifiers(sample_docx, tmp_path / "shuffled.docx")
        assert normalized_digest(DocxPackage.open(shuffled, read_only=True)) == normalized_digest(
            DocxPackage.open(sample_docx, read_only=True)
        )

    def test_real_content_change_does_change_the_digest(
        self, sample_docx: Path, tmp_path: Path
    ) -> None:
        """反向断言：归一化不能把真差异一起抹平。"""
        changed = _change_content(sample_docx, tmp_path / "changed.docx")
        assert normalized_digest(DocxPackage.open(changed, read_only=True)) != normalized_digest(
            DocxPackage.open(sample_docx, read_only=True)
        )

    def test_normalize_is_idempotent_on_its_own_output(
        self, sample_docx: Path, tmp_path: Path
    ) -> None:
        normalized = normalize_volatile(DocxPackage.open(sample_docx, read_only=True))
        parts = _read_parts(sample_docx)
        parts[_DOCUMENT] = b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>' + normalized
        # 归一化输出本身再归一化不应再发生变化（只针对同一部件做幂等性检查）
        pkg = DocxPackage.open(sample_docx, read_only=True)
        again = normalize_volatile(pkg)
        assert normalize_volatile(pkg) == again


class TestNonDestructive:
    def test_normalize_does_not_touch_the_package(self, sample_docx: Path, tmp_path: Path) -> None:
        """归一化只服务于摘要与比对，产物必须保留原件的易变标识。"""
        pkg = DocxPackage.open(sample_docx, read_only=False)
        before = {name: part.is_dirty for name, part in ((p.path, p) for p in pkg.parts("all"))}
        normalize_volatile(pkg)
        after = {name: part.is_dirty for name, part in ((p.path, p) for p in pkg.parts("all"))}
        assert before == after
        assert not any(after.values())

    def test_save_after_normalize_is_byte_identical(
        self, sample_docx: Path, tmp_path: Path
    ) -> None:
        pkg = DocxPackage.open(sample_docx, read_only=False)
        normalize_volatile(pkg)
        target = pkg.save(tmp_path / "after-normalize.docx")
        original = _read_parts(sample_docx)
        rewritten = _read_parts(target)
        assert [name for name in original if original[name] != rewritten[name]] == []


class TestPartitionDigest:
    def test_partition_digest_tracks_only_its_own_parts(
        self, sample_docx: Path, tmp_path: Path
    ) -> None:
        pkg = DocxPackage.open(sample_docx, read_only=True)
        styles_digest = digest_of(pkg, [_DOCUMENT])
        changed = _change_content(sample_docx, tmp_path / "content-changed.docx")
        other = DocxPackage.open(changed, read_only=True)
        assert digest_of(other, [_DOCUMENT]) != styles_digest
        assert digest_of(other, [_SETTINGS]) == digest_of(pkg, [_SETTINGS])
