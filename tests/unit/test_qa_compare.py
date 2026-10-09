"""稳定化比对器：三种模式与**易变标识不产假差异**（DD-10.4 / FR-M5-11 / 13）。

这一条的门禁价值全在"归一化"上：不归一化就必然 flaky，而 flaky 的比对结果会让人
得出"改了一处、报了全篇"的结论，最终把比对关掉。故用例的主体是**对立断言**：
按 Word 刷新的方式重排 ``_Toc`` 书签名、再加一个 ``w:rsid``，比对必须仍判"一致"；
而改掉真正的内容之后，必须报出差异——两端都钉住，才说明归一化既没有漏也没有过度。

一处刻意**不**归一化的形态也在这里钉住：只改书签**定义侧**而不改引用侧，比对必须
报出差异。那是一条真实的不一致（目录指向不存在的书签），把它当成"易变噪音"抹掉，
等于让"域刷新后目录点不动"这件事永远查不出来。
"""

from __future__ import annotations

import re

import pytest
from lxml import etree

from docfidelity.ooxml import DocxPackage, find_first, iter_elements, normalized_parts, qn
from docfidelity.qa import (
    CONTRACT_MODE_NOTE,
    STRUCTURE_MODE_NOTE,
    TEXT_MODE_NOTE,
    compare,
    metrics_of,
)
from tests.fixtures.qa_fixture import RenderFixture, render_fixture

pytestmark = pytest.mark.unit

_DOCUMENT_PART = "word/document.xml"
_TOC_NAME_RE = re.compile(r"_Toc(\d+)")


@pytest.fixture(scope="module")
def artifact(tmp_path_factory: pytest.TempPathFactory) -> RenderFixture:
    return render_fixture(tmp_path_factory.mktemp("qa-compare"))


def _open(path) -> DocxPackage:
    """打开产物（可写：本文件里有几处要在内存里造"另一个版本"）。"""
    return DocxPackage.open(path, read_only=False)


def _document_tree(pkg: DocxPackage):
    part = pkg.find_part(_DOCUMENT_PART)
    assert part is not None
    return part.read_tree


def _shift_toc_numbers(pkg: DocxPackage, delta: int = 1000) -> int:
    """把 ``_Toc<n>`` 的**定义侧与引用侧一起**平移，模拟 Word 刷新域后的重编号。"""
    tree = _document_tree(pkg)
    touched = 0
    for element in tree.iter():
        for name, value in list(element.attrib.items()):
            replaced = _TOC_NAME_RE.sub(lambda m: f"_Toc{int(m.group(1)) + delta:08d}", value)
            if replaced != value:
                element.set(name, replaced)
                touched += 1
        if element.tag == qn("w:instrText") and element.text and "_Toc" in element.text:
            element.text = _TOC_NAME_RE.sub(
                lambda m: f"_Toc{int(m.group(1)) + delta:08d}", element.text
            )
            touched += 1
    return touched


# ------------------------------------------------------------------ 一致 / 不一致
def test_identical_packages_compare_equal(artifact: RenderFixture) -> None:
    diff = compare(artifact.open(), artifact.open(), mode="text")
    assert diff.equal
    assert diff.counts() == {"added": 0, "removed": 0, "changed": 0}


def test_volatile_identifiers_do_not_produce_a_diff(artifact: RenderFixture) -> None:
    """按 Word 的方式重排 ``_Toc`` 编号 + 加一个 ``w:rsid`` → 必须仍判"一致"。"""
    baseline = artifact.open()
    candidate = _open(artifact.path)
    assert _shift_toc_numbers(candidate) > 0, "产物里应当有 _Toc 书签（目录重建的产物）"

    paragraph = next(iter(iter_elements(_document_tree(candidate), "w:p")), None)
    assert paragraph is not None
    paragraph.set(qn("w:rsidR"), "00ABCDEF")

    diff = compare(baseline, candidate, mode="text")
    assert diff.equal, f"易变标识不该产生差异：{diff.summary()}"


def test_dangling_bookmark_reference_is_not_normalized_away(artifact: RenderFixture) -> None:
    """只改定义侧、不改引用侧 → 这是一条**真实**的不一致，必须报出来。"""
    baseline = artifact.open()
    candidate = _open(artifact.path)
    starts = [
        item
        for item in iter_elements(_document_tree(candidate), "w:bookmarkStart")
        if (item.get(qn("w:name")) or "").startswith("_Toc")
    ]
    assert starts
    starts[0].set(qn("w:name"), "_Toc99999999")

    assert not compare(baseline, candidate, mode="text").equal


def test_real_text_change_is_reported(artifact: RenderFixture) -> None:
    baseline = artifact.open()
    candidate = _open(artifact.path)
    target = next(iter(iter_elements(_document_tree(candidate), "w:t")))
    target.text = "改过的文字"

    diff = compare(baseline, candidate, mode="text")
    assert not diff.equal
    assert any(entry.startswith(_DOCUMENT_PART) for entry in diff.snapshot.changed)


def test_added_part_is_reported(artifact: RenderFixture) -> None:
    baseline = artifact.open()
    candidate = _open(artifact.path)
    candidate.add_binary_part("word/probe.xml", "application/xml", b"<probe/>")
    diff = compare(baseline, candidate, mode="structure")
    assert "word/probe.xml" in diff.snapshot.added


def test_normalized_parts_is_ordered_by_part_name(artifact: RenderFixture) -> None:
    names = list(normalized_parts(artifact.open()))
    assert names == sorted(names)


# ------------------------------------------------------------------ 三种模式
def test_each_mode_carries_its_own_note(artifact: RenderFixture) -> None:
    assert compare(artifact.open(), artifact.open(), mode="text").notes == [TEXT_MODE_NOTE]
    assert compare(artifact.open(), artifact.open(), mode="structure").notes == [
        STRUCTURE_MODE_NOTE
    ]


def test_structure_mode_reports_metric_changes(artifact: RenderFixture) -> None:
    """结构模式要能回答"结构变了没有"——段落数变化必须落到 ``metric:`` 条目上。"""
    baseline = artifact.open()
    candidate = _open(artifact.path)
    body = find_first(_document_tree(candidate), "w:body")
    assert body is not None
    paragraph = etree.SubElement(body, qn("w:p"))
    paragraph.insert(0, etree.Element(qn("w:pPr")))
    etree.SubElement(paragraph, qn("w:r"))

    diff = compare(baseline, candidate, mode="structure")
    assert any(entry.startswith("metric:paragraphs") for entry in diff.snapshot.changed)


def test_contract_mode_diffs_json_fields(artifact: RenderFixture) -> None:
    baseline = _open(artifact.path)
    candidate = _open(artifact.path)
    baseline.add_binary_part("contract/probe.json", "application/json", b'{"a": 1, "b": {"c": 2}}')
    candidate.add_binary_part("contract/probe.json", "application/json", b'{"a": 1, "b": {"c": 3}}')

    diff = compare(baseline, candidate, mode="contract")
    assert diff.notes == [CONTRACT_MODE_NOTE]
    assert any("b.c" in entry for entry in diff.snapshot.changed)
    assert diff.snapshot.normalized is True


def test_contract_mode_ignores_non_json_parts(artifact: RenderFixture) -> None:
    """契约模式只看 ``.json`` 部件：否则它会把整份 XML 当成"契约差异"报出来。"""
    baseline = artifact.open()
    candidate = _open(artifact.path)
    target = next(iter(iter_elements(_document_tree(candidate), "w:t")))
    target.text = "改过的文字"

    assert compare(baseline, candidate, mode="contract").equal


# ------------------------------------------------------------------ 计数与摘要
def test_metrics_of_counts_document_shape(artifact: RenderFixture) -> None:
    metrics = metrics_of(artifact.open())
    assert metrics["paragraphs"] > 0
    assert metrics["tables"] >= 2  # 前置版块表 + 注入表
    assert metrics["table_rows"] > 0
    assert metrics["images"] == 1
    assert set(metrics) == {
        "paragraphs",
        "style_refs",
        "tables",
        "table_rows",
        "images",
        "page_breaks",
    }


def test_diff_result_summary_and_counts(artifact: RenderFixture) -> None:
    assert "一致" in compare(artifact.open(), artifact.open(), mode="text").summary()

    candidate = _open(artifact.path)
    candidate.add_binary_part("word/probe.xml", "application/xml", b"<probe/>")
    changed = compare(artifact.open(), candidate, mode="structure")
    assert changed.counts()["added"] == 1
    assert "有差异" in changed.summary()


def test_references_are_carried_through(artifact: RenderFixture) -> None:
    diff = compare(
        artifact.open(), artifact.open(), mode="text", baseline_ref="v1", candidate_ref="v2"
    )
    assert diff.snapshot.baseline_ref == "v1"
    assert diff.snapshot.candidate_ref == "v2"
