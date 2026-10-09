"""渲染目检：按关键词挑页、**降级必须是结论的一部分**（DD-10.5 / FR-M5-12）。

本模块的三条纪律各有其对立断言：

1. **按关键词定位，不按页数猜**（需求 1.2 第 6 条 / 硬规则 4）。故这里既验证
   "命中关键词的页被挑出来"，也验证"没有关键词时不做无谓的过滤"。
2. **降级要带原因**。``degraded=True`` 而没有 ``reason`` 是一个**无法处置**的结论
   ——使用者看不到该装什么、该改什么。
3. **"没做目检"必须与"目检通过"可区分**。故转换器/抽取器都做成可注入的，使本模块
   在没有 Office 的机器上也能跑出真实结论。
"""

from __future__ import annotations

from pathlib import Path
from types import ModuleType

import pytest

from docfidelity.qa import SpotcheckResult, spotcheck
from docfidelity.qa.spotcheck import office_converter

pytestmark = pytest.mark.unit


def _converter(pages: list[str] | None = None, *, fail: bool = False):
    def convert(_docx: Path, out_dir: Path) -> Path | None:
        if fail:
            return None
        target = out_dir / "fake.pdf"
        target.write_bytes(b"%PDF-1.4 fake")
        return target

    return convert


def _extractor(pages: list[str] | None):
    def extract(_pdf: Path) -> list[str] | None:
        return pages

    return extract


@pytest.fixture()
def docx(tmp_path: Path) -> Path:
    target = tmp_path / "artifact.docx"
    target.write_bytes(b"PK\x03\x04 fake docx")
    return target


# ------------------------------------------------------------------ 按关键词挑页
def test_pages_matching_the_keyword_are_picked(docx: Path, tmp_path: Path) -> None:
    result = spotcheck(
        docx,
        keywords=["范围"],
        work_dir=tmp_path,
        converter=_converter(),
        extractor=_extractor(["封面\n", "概述\n", "范围\n"]),
    )
    assert result.degraded is False
    assert result.reason is None
    assert result.pages == [3]
    assert result.pages_total == 3
    assert result.keywords == ["范围"]


def test_multiple_keywords_union_the_pages(docx: Path, tmp_path: Path) -> None:
    result = spotcheck(
        docx,
        keywords=["概述", "图示"],
        work_dir=tmp_path,
        converter=_converter(),
        extractor=_extractor(["概述\n", "范围\n", "图示\n"]),
    )
    assert result.pages == [1, 3]


def test_without_keywords_every_page_is_taken(docx: Path, tmp_path: Path) -> None:
    """没给关键词 → 不做无谓过滤；但这不是"按页数猜"，只是"全给"。"""
    result = spotcheck(
        docx,
        work_dir=tmp_path,
        converter=_converter(),
        extractor=_extractor(["a", "b", "c"]),
    )
    assert result.pages == [1, 2, 3]


def test_max_pages_caps_the_selection(docx: Path, tmp_path: Path) -> None:
    """目检是"抽看"而不是"逐页审"：超过上限只报计数。"""
    result = spotcheck(
        docx,
        max_pages=2,
        work_dir=tmp_path,
        converter=_converter(),
        extractor=_extractor(["x", "x", "x", "x"]),
    )
    assert result.pages == [1, 2]
    assert result.pages_total == 4


def test_no_page_matches_yields_an_empty_selection_not_an_error(docx: Path, tmp_path: Path) -> None:
    """执行了但没命中：这不是降级（不是"没做"），规则侧会判 minor。"""
    result = spotcheck(
        docx,
        keywords=["不存在"],
        work_dir=tmp_path,
        converter=_converter(),
        extractor=_extractor(["a", "b"]),
    )
    assert result.degraded is False
    assert result.pages == []


# ------------------------------------------------------------------ 降级
def test_missing_file_degrades_with_a_reason(tmp_path: Path) -> None:
    result = spotcheck(tmp_path / "does-not-exist.docx", keywords=["x"])
    assert result.degraded is True
    assert result.reason


def test_missing_converter_degrades_with_a_reason(docx: Path, tmp_path: Path) -> None:
    """一个转换器都没有是**环境事实**（DD-20 风险 3），不是异常。"""
    result = spotcheck(docx, keywords=["x"], work_dir=tmp_path, converter=_converter(fail=True))
    assert result.degraded is True
    assert "转换器" in (result.reason or "")


def test_missing_extractor_degrades_with_a_reason(docx: Path, tmp_path: Path) -> None:
    result = spotcheck(
        docx,
        keywords=["x"],
        work_dir=tmp_path,
        converter=_converter(),
        extractor=_extractor(None),
    )
    assert result.degraded is True
    assert "文本" in (result.reason or "")


def test_degraded_result_never_claims_pages(docx: Path, tmp_path: Path) -> None:
    """降级结果不得同时给出页——那会让"没做目检"看起来像"目检通过"。"""
    result = spotcheck(docx, keywords=["x"], work_dir=tmp_path, converter=_converter(fail=True))
    assert result.degraded is True
    assert result.pages == []
    assert result.pages_total == 0


def test_payload_is_jsonable(docx: Path, tmp_path: Path) -> None:
    import json

    result = spotcheck(
        docx, keywords=["a"], work_dir=tmp_path, converter=_converter(), extractor=_extractor(["a"])
    )
    payload = result.as_payload()
    assert json.loads(json.dumps(payload))["pages"] == [1]


def test_render_images_defaults_to_off(docx: Path, tmp_path: Path) -> None:
    """默认不出图：目检的产物是一份结论，不是一堆 PNG。"""
    result = spotcheck(
        docx, keywords=["a"], work_dir=tmp_path, converter=_converter(), extractor=_extractor(["a"])
    )
    assert result.images == []


def test_result_dataclass_defaults(tmp_path: Path) -> None:
    empty = SpotcheckResult()
    assert empty.degraded is False
    assert empty.reason is None
    assert empty.pages == []


@pytest.mark.parametrize("failure", [None, "export", "visible", "alerts"])
@pytest.mark.parametrize("initial_references", [0, 1])
def test_office_conversion_owns_its_instance_and_closes_its_document_on_failure(
    docx: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str | None,
    initial_references: int,
) -> None:
    import sys

    class Document:
        closed = False

        def SaveAs(self, target: str, *, FileFormat: int) -> None:
            assert FileFormat == 17
            if failure == "export":
                raise OSError("PDF export failed")
            Path(target).write_bytes(b"%PDF-1.4 converted")

        def Close(self, save_changes: bool) -> None:
            assert save_changes is False
            self.closed = True

    class Documents:
        def __init__(self, document: Document) -> None:
            self.document = document

        def Open(self, path: str) -> Document:
            assert Path(path) == docx
            return self.document

    class Application:
        closed = False

        def __init__(self) -> None:
            self.document = Document()
            self.Documents = Documents(self.document)

        def __setattr__(self, name: str, value: object) -> None:
            if (name == "Visible" and failure == "visible") or (
                name == "DisplayAlerts" and failure == "alerts"
            ):
                raise OSError("Word property unavailable")
            object.__setattr__(self, name, value)

        def Quit(self) -> None:
            self.closed = True

    user_application = Application()
    own_application = Application()
    references = initial_references
    apartment = ModuleType("pythoncom")

    def initialize() -> None:
        nonlocal references
        references += 1

    def uninitialize() -> None:
        nonlocal references
        references -= 1

    def create_application(_name: str) -> Application:
        if references != initial_references + 1:
            raise OSError("CoInitialize has not been called")
        return own_application

    apartment.COINIT_APARTMENTTHREADED = 2  # type: ignore[attr-defined]
    apartment.CoInitializeEx = lambda mode: initialize() if mode == 2 else None  # type: ignore[attr-defined]
    apartment.CoUninitialize = uninitialize  # type: ignore[attr-defined]
    client = ModuleType("win32com.client")
    client.Dispatch = lambda _name: user_application  # type: ignore[attr-defined]
    client.DispatchEx = create_application  # type: ignore[attr-defined]
    parent = ModuleType("win32com")
    parent.client = client  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "win32com", parent)
    monkeypatch.setitem(sys.modules, "win32com.client", client)
    monkeypatch.setitem(sys.modules, "pythoncom", apartment)
    monkeypatch.setattr(sys.modules["docfidelity.qa.spotcheck"], "office_available", lambda: True)

    result = office_converter(docx, tmp_path)

    assert result == (None if failure else tmp_path / "artifact.pdf")
    assert user_application.closed is False
    assert own_application.closed is True
    assert own_application.document.closed is (failure not in {"visible", "alerts"})
    assert references == initial_references
