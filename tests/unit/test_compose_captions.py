"""图注 / 表注编号（DD-8.3 / FR-M3-08）。"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import pytest

from docfidelity.compose.captions import assign, parse_numbered_caption
from docfidelity.contracts.ir import ContentIR, ContentUnit

pytestmark = pytest.mark.unit


def _ir(*units: ContentUnit) -> ContentIR:
    return ContentIR(
        project_id="demo",
        doc_key="detail",
        profile_id="p",
        template_ref="TPL@1.0",
        units=list(units),
    )


def _chapter(index: int, title: str, children: Sequence[ContentUnit] = ()) -> ContentUnit:
    return ContentUnit(
        unit_id=f"chapter#{index}",
        type="heading",
        level=1,
        text=title,
        children=list(children),
    )


def _figure(unit_id: str, caption: str | None, slot: Any) -> ContentUnit:
    return ContentUnit(
        unit_id=unit_id,
        type="figure",
        fields={"image_ref": "img", "caption": caption, "caption_slot": slot},
    )


def _table(unit_id: str, caption: str | None, slot: Any) -> ContentUnit:
    return ContentUnit(
        unit_id=unit_id,
        type="table",
        fields={"columns": ["a"], "rows": [["1"]], "caption": caption, "caption_slot": slot},
    )


def _caption(ir: ContentIR, unit_id: str) -> str:
    unit = ir.find(unit_id)
    assert unit is not None
    return str(unit.fields.get("caption"))


def test_numbers_are_chapter_scoped_and_gapless() -> None:
    ir = _ir(
        _chapter(
            0, "总述", [_figure("f1", "甲", 0), _figure("f2", "乙", 0), _table("t1", "丙", 0)]
        ),
        _chapter(1, "细节", [_figure("f3", "丁", 0)]),
    )

    assign(ir)

    assert _caption(ir, "f1") == "图 1-1 甲"
    assert _caption(ir, "f2") == "图 1-2 乙"
    assert _caption(ir, "t1") == "表 1-1 丙"
    assert _caption(ir, "f3") == "图 2-1 丁"


def test_screenshot_shares_the_figure_series() -> None:
    """``screenshot`` 与 ``figure`` 共用「图」序号——两个序号列会在正文引用里打架。"""
    screenshot = ContentUnit(
        unit_id="s1",
        type="screenshot",
        fields={"image_ref": "shot", "caption": "界面", "caption_slot": 0},
    )
    ir = _ir(_chapter(0, "界面", [_figure("f1", "甲", 0), screenshot]))

    assign(ir)

    assert _caption(ir, "f1") == "图 1-1 甲"
    assert _caption(ir, "s1") == "图 1-2 界面"


def test_units_without_caption_slot_are_not_numbered_and_do_not_consume_a_number() -> None:
    ir = _ir(_chapter(0, "章", [_figure("f1", "无编号", None), _figure("f2", "有编号", 0)]))

    assign(ir)

    assert _caption(ir, "f1") == "无编号"
    assert _caption(ir, "f2") == "图 1-1 有编号"


def test_numbering_works_without_a_name() -> None:
    ir = _ir(_chapter(0, "章", [_figure("f1", None, 0)]))

    assign(ir)

    assert _caption(ir, "f1") == "图 1-1"


def test_assign_is_idempotent() -> None:
    ir = _ir(_chapter(0, "章", [_figure("f1", "甲", 0), _figure("f2", "乙", 0)]))

    assign(ir)
    first = [_caption(ir, "f1"), _caption(ir, "f2")]
    assign(ir)

    assert first == ["图 1-1 甲", "图 1-2 乙"]
    assert [_caption(ir, "f1"), _caption(ir, "f2")] == first


def test_content_outside_any_chapter_gets_chapter_zero() -> None:
    """``X=0`` 是非法成果，但它**可见**；悄悄从 1 起算会让"内容落在所有章节之外"隐形。"""
    ir = _ir(_figure("f1", "游离", 0))

    assign(ir)

    assert _caption(ir, "f1") == "图 0-1 游离"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("图 3-1 登录流程", ("图", 3, 1)),
        ("表 12-7", ("表", 12, 7)),
        ("图3-2 紧凑写法", ("图", 3, 2)),
        ("图表说明", None),
        ("图 一-1", None),
        ("", None),
    ],
)
def test_parse_numbered_caption(text: str, expected: tuple[str, int, int] | None) -> None:
    assert parse_numbered_caption(text) == expected
