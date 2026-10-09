"""域结果回读（刷新专用）单测。

这份用例守着一条**真被踩过**的约束：``Word.Document.Save()`` 不是"把算好的页码写
下来"，而是把整个包重写一遍——``word/styles.xml`` 会被换成 Word 自己的紧凑 styleId
方案，模板里写死的 ``Normal`` / ``Heading1`` 随之消失。于是：

* 产物不再是模板的产物（"样式层零改动"当场失效）；
* 本机装没装 Word 成了产物字节的输入，NFR-02 的确定性随之失效；
* 下游 QA 按模板口径核对时全线误报。

故刷新改成：Word 只在临时副本上算，页码由我们自己写进自己的包。下面第 4 条就是
这条承诺的可断言形式——**除 ``word/document.xml`` 外，全部部件逐字节不变**。

第 5 条守住配对方式：目录条目的 ``PAGEREF`` 可能嵌在跨条目的外层 ``TOC`` 域里，
按"嵌套深度 == 1"筛会漏掉它，只能按**指令**配对。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from lxml import etree

from docfidelity.ooxml import (
    DocxPackage,
    apply_toc_page_numbers,
    field_results,
    office_available,
    qn,
    refresh_with_office,
    toc_entry_results,
)
from tests.fixtures.render_fixture import docx_part_bytes, template_docx_path

pytestmark = pytest.mark.unit

_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def _paragraph(xml: str) -> etree._Element:
    return etree.fromstring(f'<w:p xmlns:w="{_W}">{xml}</w:p>')


# ------------------------------------------------------------------ 配对
def test_field_results_finds_a_nested_pageref() -> None:
    """``PAGEREF`` 嵌在外层 ``TOC`` 域里时也要读出来（按指令配对，不按深度）。

    真实目录的结构是"外层 ``TOC`` 域的 ``begin`` 在第一条、``end`` 在最后一条"，
    条目自己的 ``PAGEREF`` 因此处在第 2 层。按固定深度筛会漏——而漏掉的后果是
    "页码写不回去"，页面上看着一切正常。
    """
    paragraph = _paragraph(
        '<w:r><w:fldChar w:fldCharType="begin"/></w:r>'
        '<w:r><w:instrText> TOC \\o "1-3" \\h </w:instrText></w:r>'
        '<w:r><w:fldChar w:fldCharType="separate"/></w:r>'
        '<w:hyperlink w:anchor="_Toc1">'
        "<w:r><w:t>概述</w:t></w:r>"
        '<w:r><w:fldChar w:fldCharType="begin"/></w:r>'
        "<w:r><w:instrText> PAGEREF _Toc1 \\h </w:instrText></w:r>"
        '<w:r><w:fldChar w:fldCharType="separate"/></w:r>'
        "<w:r><w:t>4</w:t></w:r>"
        '<w:r><w:fldChar w:fldCharType="end"/></w:r>'
        "</w:hyperlink>"
    )

    results = field_results(paragraph)

    assert [item.instruction.strip() for item in results] == ["PAGEREF _Toc1 \\h"]
    assert results[0].text == "4"
    assert [node.text for node in results[0].nodes] == ["4"]


def test_field_results_ignores_fields_without_a_result() -> None:
    """只有 ``begin`` 没有 ``separate`` 的域（未计算）不该被当成"结果为空"。"""
    paragraph = _paragraph(
        '<w:r><w:fldChar w:fldCharType="begin"/></w:r>'
        "<w:r><w:instrText> PAGEREF _Toc1 \\h </w:instrText></w:r>"
    )
    assert field_results(paragraph) == []


# ------------------------------------------------------------------ 读与写
def test_toc_entry_results_reads_the_placeholder_numbers(tmp_path: Path) -> None:
    """模板目录条目的 ``PAGEREF`` 结果就是占位页码（重建后必然过期）。"""
    with DocxPackage.open(template_docx_path(tmp_path), read_only=True) as pkg:
        entries = toc_entry_results(pkg)

    assert [item.text for item in entries] == ["1", "1"]
    assert all(item.instruction.strip().startswith("PAGEREF") for item in entries)


def test_apply_toc_page_numbers_writes_every_entry(tmp_path: Path) -> None:
    path = template_docx_path(tmp_path)
    with DocxPackage.open(path, read_only=False) as pkg:
        assert apply_toc_page_numbers(pkg, ["7", "9"]) == 2
        pkg.save(path)

    with DocxPackage.open(path, read_only=True) as pkg:
        assert [item.text for item in toc_entry_results(pkg)] == ["7", "9"]


def test_apply_toc_page_numbers_refuses_on_count_mismatch(tmp_path: Path) -> None:
    """条目数与页码数不一致时**一处不改**：宁可留占位页码，也不要错位。

    错位的错法页面上看不出来——读者看到的每个页码都是对的数字、错的位置。
    """
    path = template_docx_path(tmp_path)
    with DocxPackage.open(path, read_only=False) as pkg:
        assert apply_toc_page_numbers(pkg, ["7", "9", "11"]) == 0
        assert [item.text for item in toc_entry_results(pkg)] == ["1", "1"]


def test_page_number_write_touches_document_part_only(tmp_path: Path) -> None:
    """回写页码**只**动 ``word/document.xml``：其余部件逐字节不变。

    这就是"刷新非破坏性"的可断言形式。让它退化成"Word 存一次盘"的写法，样式层
    会被整份替换，而离线检查（lxml 良构 / python-docx 可读 / 包可解压）全都拦不住。
    """
    path = template_docx_path(tmp_path)
    before = docx_part_bytes(path)

    with DocxPackage.open(path, read_only=False) as pkg:
        assert apply_toc_page_numbers(pkg, ["7", "9"]) == 2
        pkg.save(path)

    after = docx_part_bytes(path)
    assert set(after) == set(before)
    assert after["word/document.xml"] != before["word/document.xml"]
    for name, payload in before.items():
        if name != "word/document.xml":
            assert after[name] == payload, f"未触碰的部件被改写了：{name}"


# ------------------------------------------------------------------ 降级
def test_refresh_without_office_leaves_the_file_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """本机没有 Word 时如实返回 ``False``，产物一个字节都不动（NFR-05 降级）。"""
    monkeypatch.setattr("docfidelity.ooxml.refresh.office_available", lambda: False)
    path = template_docx_path(tmp_path)
    before = docx_part_bytes(path)

    assert refresh_with_office(path) is False
    assert docx_part_bytes(path) == before


def test_office_availability_is_reported_consistently() -> None:
    """探测是布尔、不抛异常——``render`` 要先据此决定"降级还是真刷"。"""
    assert isinstance(office_available(), bool)


def test_refresh_never_calls_word_on_a_missing_artifact(tmp_path: Path) -> None:
    """产物不存在时直接返回 ``False``：不该为了"试试看"去起一个 Word 进程。"""
    assert refresh_with_office(tmp_path / "没有这个文件.docx") is False


def test_field_result_markers_use_the_namespace_prefix() -> None:
    """``field_results`` 认得的是 ``w:`` 命名空间下的标签（裸标签会被静默忽略）。"""
    plain = etree.fromstring('<p><r><fldChar fldCharType="begin"/></r></p>')
    assert field_results(plain) == []
    assert qn("w:fldChar").endswith("}fldChar")


@pytest.mark.parametrize("initial_references", [0, 1])
def test_office_refresh_initializes_and_releases_the_calling_threads_com(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, initial_references: int
) -> None:
    import sys
    from types import ModuleType

    references = initial_references
    application_closed = False
    apartment = ModuleType("pythoncom")

    def initialize() -> None:
        nonlocal references
        references += 1

    def uninitialize() -> None:
        nonlocal references
        references -= 1

    class Fields:
        def Update(self) -> None:
            assert references == initial_references + 1

    class Document:
        TablesOfContents: tuple[object, ...] = ()

        def __init__(self) -> None:
            self.Fields = Fields()

        def Save(self) -> None:
            pass

        def Close(self, save_changes: bool) -> None:
            assert references == initial_references + 1 and save_changes is False

    class Documents:
        def Open(self, path: str) -> Document:
            assert Path(path).is_file()
            return Document()

    class Application:
        def __init__(self) -> None:
            self.Documents = Documents()

        def Quit(self) -> None:
            nonlocal application_closed
            assert references == initial_references + 1
            application_closed = True

    def create_application(_name: str) -> Application:
        if references != initial_references + 1:
            raise OSError("CoInitialize has not been called")
        return Application()

    apartment.COINIT_APARTMENTTHREADED = 2  # type: ignore[attr-defined]
    apartment.CoInitializeEx = lambda mode: initialize() if mode == 2 else None  # type: ignore[attr-defined]
    apartment.CoUninitialize = uninitialize  # type: ignore[attr-defined]
    client = ModuleType("win32com.client")
    client.DispatchEx = create_application  # type: ignore[attr-defined]
    parent = ModuleType("win32com")
    parent.client = client  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "pythoncom", apartment)
    monkeypatch.setitem(sys.modules, "win32com", parent)
    monkeypatch.setitem(sys.modules, "win32com.client", client)
    monkeypatch.setattr("docfidelity.ooxml.refresh.office_available", lambda: True)

    path = template_docx_path(tmp_path)
    assert refresh_with_office(path) is True
    assert application_closed is True
    assert references == initial_references
