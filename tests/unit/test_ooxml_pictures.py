"""图片放置与缩放原语的单测（DD-4.4 / DD-9.5）。

``place_picture`` 自 P0-03 落地以来没有直接覆盖，它的形态只被渲染链路的端到端测试
间接验过。本轮 qa 的图注绑定检查（FR-M5-07 的"图与图注不分离"）第一次**按运行取图**
去读产物，于是暴露出一个长期隐形的结构缺陷：``w:drawing`` 被直接挂在 ``w:p`` 下，
而不是包在 ``w:r`` 里。Word 与 LibreOffice 都宽容到照样显示，所以它一直没被发现——
但它是**非法 OOXML**（``CT_P`` 的内容模型里没有 ``w:drawing``），且会让任何"按运行
取图"的代码看不到这张图。

故这里的第一条断言是**结构合法性**，而不是"图能显示"。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import pytest
from lxml import etree

from docfidelity.ooxml import (
    DOCUMENT_PART,
    DocxPackage,
    find_all,
    find_first,
    place_picture,
    qn,
    read_extents,
    resize_pictures,
    serialize,
)
from docfidelity.ooxml.pictures import IMAGE_REL_TYPE, PICTURE_GRAPHIC_URI
from tests.fixtures.docx_builder import png_bytes

pytestmark = pytest.mark.unit

_CONTENT_TYPES = (
    b'<?xml version="1.0" encoding="UTF-8"?>'
    b'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    b'<Default Extension="xml" ContentType="application/xml"/>'
    b'<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    b'<Default Extension="png" ContentType="image/png"/>'
    b'<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-'
    b'officedocument.wordprocessingml.document.main+xml"/>'
    b"</Types>"
)

_EMPTY_RELS = (
    b'<?xml version="1.0" encoding="UTF-8"?>'
    b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"/>'
)

#: 造一张图不便宜，且同一张图在所有用例里都够用。
_PIXELS = (240, 160)


@lru_cache(maxsize=1)
def _image() -> bytes:
    return png_bytes(*_PIXELS)


@pytest.fixture()
def package() -> DocxPackage:
    """一个只含三个空段落的可写包（结构从零构造，不受模板夹具形态影响）。"""
    document = etree.Element(qn("w:document"))
    body = etree.SubElement(document, qn("w:body"))
    for _ in range(3):
        etree.SubElement(body, qn("w:p"))
    return DocxPackage.from_parts(
        {
            "[Content_Types].xml": _CONTENT_TYPES,
            "_rels/.rels": _EMPTY_RELS,
            "word/_rels/document.xml.rels": _EMPTY_RELS,
            DOCUMENT_PART: serialize(document),
        },
        read_only=False,
    )


def _paragraphs(pkg: DocxPackage) -> list:
    body = find_first(pkg.part(DOCUMENT_PART).read_tree, "w:body")
    assert body is not None
    return find_all(body, "w:p")


def _place(pkg: DocxPackage, paragraph, **overrides: object):  # type: ignore[no-untyped-def]
    kwargs: dict[str, object] = {
        "filename": "probe.png",
        "avail_w_emu": 6_000_000,
        "avail_h_emu": 9_000_000,
    }
    kwargs.update(overrides)
    return place_picture(pkg, paragraph, _image(), **kwargs)  # type: ignore[arg-type]


# ------------------------------------------------------------------ 结构合法性
def test_drawing_is_wrapped_in_a_run(package: DocxPackage) -> None:
    """``w:drawing`` 必须是 ``w:p/w:r/w:drawing``：``CT_P`` 只允许运行承载内容。"""
    paragraph = _paragraphs(package)[0]
    _place(package, paragraph)

    assert find_first(paragraph, "w:drawing") is None, "w:drawing 不得是 w:p 的直接子元素"
    runs_with_drawing = [
        run for run in find_all(paragraph, "w:r") if find_first(run, "w:drawing") is not None
    ]
    assert len(runs_with_drawing) == 1


def test_pictures_are_reachable_through_runs(package: DocxPackage) -> None:
    """ "按运行取图"必须取得到——这正是那条非法结构挡住的口径。"""
    paragraph = _paragraphs(package)[0]
    _place(package, paragraph)

    assert [
        run
        for run in find_all(paragraph, "w:r")
        if any(find_first(run, f"w:{name}") is not None for name in ("drawing", "pict"))
    ]


def test_drawing_carries_the_picture_graphic_uri(package: DocxPackage) -> None:
    paragraph = _paragraphs(package)[0]
    _place(package, paragraph)

    graphic_data = find_first(
        find_first(find_first(find_first(paragraph, "w:r"), "w:drawing"), "wp:inline"),
        "a:graphic",
    )
    assert graphic_data is not None
    assert graphic_data.find(qn("a:graphicData")).get("uri") == PICTURE_GRAPHIC_URI


# ------------------------------------------------------------------ 只缩不放
def test_oversized_image_is_shrunk(package: DocxPackage) -> None:
    extent = _place(package, _paragraphs(package)[0], avail_w_emu=1_000_000)
    assert extent.cx <= 1_000_000
    assert extent.cy > 0
    assert extent.anchor is False


def test_smaller_image_is_never_grown(package: DocxPackage) -> None:
    extent = _place(
        package,
        _paragraphs(package)[0],
        avail_w_emu=40_000_000,
        avail_h_emu=40_000_000,
        cx_emu=500_000,
        cy_emu=400_000,
    )
    assert (extent.cx, extent.cy) == (500_000, 400_000)


# ------------------------------------------------------------------ 部件与关系
def test_image_part_and_relationship_are_created(package: DocxPackage) -> None:
    _place(package, _paragraphs(package)[0])

    media = sorted(name for name in package.part_names("all") if name.startswith("word/media/"))
    assert media, "图片必须作为一个部件落进包里"
    assert package.content_type_of(media[0]) == "image/png"
    assert any(rel.type == IMAGE_REL_TYPE for rel in package.relationships(DOCUMENT_PART))


def test_two_placements_do_not_collide_on_media_path(package: DocxPackage) -> None:
    paragraphs = _paragraphs(package)
    _place(package, paragraphs[0])
    _place(package, paragraphs[1])

    media = sorted(name for name in package.part_names("all") if name.startswith("word/media/"))
    assert len(media) == 2, "同名图片两次放置不得互相覆盖"


def test_drawing_ids_are_distinct(package: DocxPackage) -> None:
    """``wp:docPr/@id`` 重复会让 Word 报"文档包含无法读取的内容"。"""
    paragraphs = _paragraphs(package)
    _place(package, paragraphs[0])
    _place(package, paragraphs[1])

    ids = [node.get("id") for node in package.part(DOCUMENT_PART).read_tree.iter(qn("wp:docPr"))]
    assert len(ids) == len(set(ids)) == 2


# ------------------------------------------------------------------ 读取与缩放
def test_read_extents_finds_the_placed_picture(package: DocxPackage) -> None:
    placed = _place(package, _paragraphs(package)[0])

    extents = read_extents(package)
    assert len(extents) == 1
    assert (extents[0].cx, extents[0].cy) == (placed.cx, placed.cy)
    assert "inline" in extents[0].xpath


def test_resize_leaves_an_in_range_picture_alone(package: DocxPackage) -> None:
    _place(package, _paragraphs(package)[0])
    report = resize_pictures(package, avail_w_emu=40_000_000, avail_h_emu=40_000_000)
    assert (report.resized, report.unchanged) == (0, 1)
    assert report.min_scale == 1.0


def test_resize_shrinks_a_too_wide_picture(package: DocxPackage) -> None:
    _place(package, _paragraphs(package)[0])
    report = resize_pictures(package, avail_w_emu=1_000_000, avail_h_emu=9_000_000)
    assert report.resized == 1
    assert read_extents(package)[0].cx <= 1_000_000


def test_source_is_none_for_in_memory_packages(package: DocxPackage) -> None:
    assert package.source is None
    assert isinstance(Path("probe.docx").name, str)
