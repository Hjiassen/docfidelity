"""几何纯函数的单测（DD-4.4）。

这一组测试的价值在于穷举边界：图片越界的判定只有一处实现，一旦它错了，
"只缩不放"与"越界必报"两个承诺会同时失效，而且线上表现为"图看起来没超版心、
Word 里却溢出"。
"""

from __future__ import annotations

import pytest

from docfidelity.contracts.errors import EngineError
from docfidelity.contracts.template import Margins, PageSetupContract, PageSize
from docfidelity.ooxml.geometry import (
    EMU_PER_TWIP,
    MIN_COLUMN_TWIPS,
    emu_to_twips,
    fit_picture,
    is_overflow,
    normalize_table_width,
    pixels_to_emu,
    twips_to_emu,
    usable_height,
    usable_width,
    usable_width_emu,
)

pytestmark = pytest.mark.unit


def _page(**overrides: object) -> PageSetupContract:
    defaults: dict[str, object] = {
        "page_size": PageSize(width_twips=11906, height_twips=16838),
        "margins": Margins(top_twips=1440, bottom_twips=1440, left_twips=1800, right_twips=1800),
    }
    defaults.update(overrides)
    return PageSetupContract(**defaults)  # type: ignore[arg-type]


class TestPageGeometry:
    def test_usable_width_subtracts_both_margins(self) -> None:
        assert usable_width(_page()) == 11906 - 1800 - 1800

    def test_usable_height_uses_other_axis(self) -> None:
        assert usable_height(_page()) == 16838 - 1440 - 1440

    def test_landscape_swaps_axes(self) -> None:
        portrait = _page()
        landscape = _page(
            page_size=PageSize(width_twips=11906, height_twips=16838, orientation="landscape")
        )
        assert usable_width(landscape) == 16838 - 3600
        assert usable_width(landscape) != usable_width(portrait)

    def test_emu_conversion_matches_twip_factor(self) -> None:
        page = _page()
        assert usable_width_emu(page) == usable_width(page) * EMU_PER_TWIP
        assert emu_to_twips(twips_to_emu(12345)) == 12345

    def test_out_of_range_section_is_rejected_not_silently_fallback(self) -> None:
        """越界下标必须报错：静默回退到第 0 节会让"第 3 节按第 1 节版心判定"。"""
        from docfidelity.contracts.template import SectionSpec

        page = _page(
            sections=[
                SectionSpec(
                    index=0,
                    page_size=PageSize(width_twips=11906, height_twips=16838),
                    margins=Margins(
                        top_twips=1440, bottom_twips=1440, left_twips=1800, right_twips=1800
                    ),
                )
            ]
        )
        assert usable_width(page, 0) == 8306
        with pytest.raises(EngineError):
            usable_width(page, 3)


class TestFitPicture:
    def test_shrinks_to_available_width_keeping_aspect(self) -> None:
        cx, cy, scale = fit_picture(9_000_000, 3_000_000, 5_943_600, 8_000_000)
        assert cx == 5_943_600
        assert scale == pytest.approx(5_943_600 / 9_000_000)
        assert cy == int(3_000_000 * scale)

    def test_never_grows_by_default(self) -> None:
        """1.4 原则：小图不得被拉伸。"""
        cx, cy, scale = fit_picture(1000, 500, 100_000, 100_000)
        assert (cx, cy) == (1000, 500)
        assert scale == 1.0

    def test_grows_only_when_explicitly_allowed(self) -> None:
        cx, cy, scale = fit_picture(1000, 500, 4000, 4000, allow_grow=True)
        assert scale == pytest.approx(4.0)
        assert (cx, cy) == (4000, 2000)

    def test_height_constraint_wins_when_more_binding(self) -> None:
        cx, cy, scale = fit_picture(1000, 1000, 10_000, 500)
        assert scale == pytest.approx(0.5)
        assert (cx, cy) == (500, 500)

    @pytest.mark.parametrize(
        ("cx", "cy", "avail_w", "avail_h"),
        [(0, 100, 100, 100), (100, 0, 100, 100), (100, 100, 0, 100), (100, 100, 100, 0)],
    )
    def test_degenerate_input_raises(self, cx: int, cy: int, avail_w: int, avail_h: int) -> None:
        with pytest.raises(EngineError):
            fit_picture(cx, cy, avail_w, avail_h)

    def test_zero_area_is_not_produced(self) -> None:
        """极小缩放不得把尺寸算成 0——0 宽图片在 Word 里是"图片消失"。"""
        cx, cy, _ = fit_picture(10_000_000, 10_000_000, 1, 1)
        assert cx >= 1 and cy >= 1


class TestNormalizeTableWidth:
    def test_sum_is_exactly_available_width(self) -> None:
        result = normalize_table_width([1000, 2000, 3000], 5997)
        assert sum(result) == 5997

    def test_proportional_ratio_is_preserved(self) -> None:
        result = normalize_table_width([1000, 3000], 4000)
        assert result == [1000, 3000]

    def test_remainder_goes_to_largest_fraction_deterministically(self) -> None:
        first = normalize_table_width([1, 1, 1], 1000)
        second = normalize_table_width([1, 1, 1], 1000)
        assert first == second
        assert sum(first) == 1000
        assert max(first) - min(first) <= 1

    def test_zero_width_template_splits_evenly(self) -> None:
        result = normalize_table_width([0, 0, 0], 1000)
        assert sum(result) == 1000

    def test_empty_grid_returns_empty(self) -> None:
        assert normalize_table_width([], 1000) == []

    def test_negative_width_is_rejected(self) -> None:
        with pytest.raises(EngineError):
            normalize_table_width([100, -1], 1000)

    def test_too_narrow_page_is_rejected(self) -> None:
        with pytest.raises(EngineError):
            normalize_table_width([1, 1, 1, 1], MIN_COLUMN_TWIPS * 4 - 1)

    def test_narrow_column_is_widened_from_the_widest(self) -> None:
        """极窄列必须被抬到可读下限，否则该列内容会逐字折行。"""
        result = normalize_table_width([100_000, 1, 1], MIN_COLUMN_TWIPS * 4)
        assert sum(result) == MIN_COLUMN_TWIPS * 4
        assert min(result) >= MIN_COLUMN_TWIPS


class TestOverflow:
    def test_overflow_uses_extent_only(self) -> None:
        assert is_overflow(6_000_000, 1_000_000, 5_943_600, 8_000_000)
        assert not is_overflow(5_943_600, 1_000_000, 5_943_600, 8_000_000)

    def test_boundary_is_inclusive(self) -> None:
        """恰好等于版心不算越界——差一 EMU 就报会让门禁天天红。"""
        assert not is_overflow(5_943_600, 8_000_000, 5_943_600, 8_000_000)

    def test_pixels_to_emu_uses_dpi(self) -> None:
        assert pixels_to_emu(96, 96) == 914_400
        assert pixels_to_emu(960, 96) == 9_144_000

    def test_pixels_to_emu_rejects_non_positive_dpi(self) -> None:
        with pytest.raises(EngineError):
            pixels_to_emu(100, 0)
