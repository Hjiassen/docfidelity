"""结构快照的投影语义（DD-17.2 / TASK-P0-19）。

本文件守的是**两件相反的事**，缺任一件这个模块就白写：

1. 对易变标识（rsid、书签编号、关系 ID、时间戳）**视而不见**——不然基线每周都会红，
   而红的原因与"改动是否有害"无关；
2. 对真实的结构改动**立刻可见**——不然它就只是个总在通过的装饰。

第 2 条尤其要防"检查器恒返回空"这类缺陷：断言一个空列表很容易写成一个恒真的条件，
故这里都用**两份真产物**对比，让差异确实产生再消失。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from docfidelity.ooxml import DocxPackage
from docfidelity.qa.snapshot import (
    BREAK_BEFORE_LOCAL,
    BREAK_LOCAL,
    diff_snapshots,
    structure_snapshot,
)
from docfidelity.render.pagination import BREAK_BEFORE_LOCAL as RENDER_BREAK_BEFORE
from docfidelity.render.pagination import BREAK_LOCAL as RENDER_BREAK_LOCAL
from tests.fixtures.docx_builder import build_sample_docx
from tests.fixtures.docx_perturb import shuffle_volatile_identifiers

pytestmark = pytest.mark.unit


def _snapshot(path: Path, doc_key: str = "sample") -> dict:
    with DocxPackage.open(path, read_only=True) as pkg:
        return structure_snapshot(pkg, doc_key=doc_key)


def test_volatile_identifiers_do_not_reach_the_snapshot(tmp_path: Path) -> None:
    """同一份模板被 Word 重存（rsid / 书签号 / 关系 ID / 时间戳全变）后，快照逐字相同。

    这是整个 golden 方案成立的前提：不成立的话，基线比对的结果取决于"有没有人
    用 Word 打开过这份文件"。
    """
    original = build_sample_docx(tmp_path / "original.docx")
    shuffled = shuffle_volatile_identifiers(original, tmp_path / "shuffled.docx")

    before, after = _snapshot(original), _snapshot(shuffled)
    assert before == after, f"易变标识泄漏进了快照：{diff_snapshots(before, after)}"


def test_a_removed_table_shows_up_as_a_difference(tmp_path: Path) -> None:
    """抽掉一张表 ⇒ 差异条目点明"tables 少了一条"，而不是只报"不一样"。"""
    with_table = build_sample_docx(tmp_path / "with.docx", with_table=True)
    without = build_sample_docx(tmp_path / "without.docx", with_table=False)

    lines = diff_snapshots(_snapshot(with_table), _snapshot(without))
    assert any(line.startswith("tables:") for line in lines), lines
    assert any("style_refs[TableGrid]" in line for line in lines), lines


def test_tables_in_the_body_are_reachable(tmp_path: Path) -> None:
    """表格在 ``w:body`` 里，不在 ``w:document`` 的直接子层级上。

    回归守卫：``ooxml.find_all`` **只看直接子元素**，当初第一版用
    ``find_all(root, "w:tbl")`` 去数表，恒返回空集——快照里 ``tables`` 永远是 ``[]``，
    而"没有表格"与"找不到表格"在断言层面长得一模一样。
    """
    path = build_sample_docx(tmp_path / "with.docx", with_table=True)
    tables = _snapshot(path)["tables"]
    assert tables, "表格没被找到——检查是不是又用 find_all 去数 body 里的元素"
    assert tables[0]["rows"] == 2
    assert tables[0]["cols"] == 3


def test_sections_counts_the_closing_sectpr(tmp_path: Path) -> None:
    """一份没有分节符的文档仍然有 1 节（``w:body`` 末尾的 ``w:sectPr``）。

    断言"至少 1"而不是"等于 1"：夹具细节会变，而"数不到节"永远是缺陷。
    """
    assert _snapshot(build_sample_docx(tmp_path / "sample.docx"))["sections"] >= 1


def test_break_kind_constants_agree_with_the_renderer() -> None:
    """分页点载体的字面量与渲染层一致。

    本模块**另起了一份**常量（读产物 vs 插分页是两件事），那就得有一条断言钉住它们
    相等——两处各写一份字面量而不校验，改一处即静默错位。
    """
    assert BREAK_LOCAL == RENDER_BREAK_LOCAL
    assert BREAK_BEFORE_LOCAL == RENDER_BREAK_BEFORE


def test_style_histogram_counts_every_reference(tmp_path: Path) -> None:
    """样式直方图是**逐引用计数**，不是"出现过哪些样式"。

    只记集合的话，"Heading1 从 4 次掉到 1 次"这种最该被看见的改动会完全隐形。
    """
    refs = _snapshot(build_sample_docx(tmp_path / "sample.docx"))["style_refs"]
    assert refs.get("Heading1", 0) >= 2, refs
    assert all(count >= 1 for count in refs.values()), refs


def test_doc_key_is_not_part_of_the_comparison(tmp_path: Path) -> None:
    """``doc_key`` 由调用方传，不参与比对。

    它是"这份文档在项目里叫什么"，产物里没有这个事实。参与比对的话，同一个包用两个
    名字取快照会报出差异，而那不是"结构变了"。
    """
    path = build_sample_docx(tmp_path / "sample.docx")
    assert diff_snapshots(_snapshot(path, "a"), _snapshot(path, "b")) == []
    assert _snapshot(path, "a")["doc_key"] == "a"


def test_an_identical_pair_reports_nothing(tmp_path: Path) -> None:
    """同一份文件取两次快照，差异列表为空。

    这条看似废话，但它是上面所有"有差异"断言的对照组：若 ``diff_snapshots`` 恒返回
    非空，那些断言会照常通过。
    """
    path = build_sample_docx(tmp_path / "sample.docx")
    assert diff_snapshots(_snapshot(path), _snapshot(path)) == []
