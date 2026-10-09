"""几何计算：版心推导、等比缩放、列宽归一（DD-4.4）。

本模块是**纯函数**模块：不读文件、不碰 XML、不依赖 ``lxml``。
输入是契约里的数值，输出是数值。这样越界判定才能在单测里被穷举。

单位纪律
--------
``PageSetupContract`` 用 **twips**（1/20 磅），``wp:extent`` 与 DrawingML 用 **EMU**。
两者混用是"图片看起来没超版心、Word 里却溢出"的根因，因此本模块同时提供两组
单位换算，并要求调用方在函数名上显式声明单位（``*_twips`` / ``*_emu`` / ``*_px``）。
"""

from __future__ import annotations

from typing import Final

from docfidelity.contracts.errors import EngineError, ErrorCode
from docfidelity.contracts.template import Margins, PageSetupContract, PageSize

#: 1 twip = 1/20 磅；1 磅 = 12700 EMU。
EMU_PER_TWIP: Final = 635
EMU_PER_INCH: Final = 914400

#: 表格最小列宽（twips）：约 0.2 厘米。低于它列内容必然全部折行。
MIN_COLUMN_TWIPS: Final = 113


def twips_to_emu(twips: int) -> int:
    """twips -> EMU。"""
    return twips * EMU_PER_TWIP


def emu_to_twips(emu: int) -> int:
    """EMU -> twips（向零取整，供诊断与阈值比较使用）。"""
    return emu // EMU_PER_TWIP


def pixels_to_emu(pixels: int, dpi: int) -> int:
    """像素 + DPI -> EMU（``FigureData`` 只给像素与 DPI，换算在渲染层做）。"""
    if dpi <= 0:
        raise EngineError(
            ErrorCode.SCHEMA_INVALID,
            f"DPI 必须为正数：{dpi}",
            where={"dpi": dpi},
            remediation=["检查文档档案中的图片 DPI 设置"],
        )
    return round(pixels * EMU_PER_INCH / dpi)


def _section_at(page: PageSetupContract, section_index: int) -> tuple[PageSize, Margins]:
    """取第 ``section_index`` 节的 (page_size, margins)。

    ``sections`` 为空时回落到顶层 ``page_size`` / ``margins``；``sections`` 非空但
    下标越界属于调用方的定位错误，直接抛错而不是静默取第 0 节——静默回退会让
    "第 3 节的图片按第 1 节版心判定"，越界漏判。
    """
    if not page.sections:
        return page.page_size, page.margins
    if not 0 <= section_index < len(page.sections):
        raise EngineError(
            ErrorCode.SCHEMA_INVALID,
            f"分节下标越界：{section_index}（共 {len(page.sections)} 节）",
            where={"section_index": section_index, "sections": len(page.sections)},
            remediation=["确认模板契约中的分节数与实际文档一致"],
        )
    section = page.sections[section_index]
    return section.page_size, section.margins


def usable_width(page: PageSetupContract, section_index: int = 0) -> int:
    """版心宽（twips）= 纸张正文方向宽度 - 左右页边距。"""
    size, margins = _section_at(page, section_index)
    return size.text_orientation_width - margins.left_twips - margins.right_twips


def usable_height(page: PageSetupContract, section_index: int = 0) -> int:
    """版心高（twips）= 纸张另一方向长度 - 上下页边距。"""
    size, margins = _section_at(page, section_index)
    height = size.width_twips if size.orientation == "landscape" else size.height_twips
    return height - margins.top_twips - margins.bottom_twips


def usable_width_emu(page: PageSetupContract, section_index: int = 0) -> int:
    """版心宽（EMU）。图片越界判定用这个，不要拿 twips 直接和 ``wp:extent`` 比。"""
    return twips_to_emu(usable_width(page, section_index))


def usable_height_emu(page: PageSetupContract, section_index: int = 0) -> int:
    """版心高（EMU）。"""
    return twips_to_emu(usable_height(page, section_index))


def fit_picture(
    cx: int, cy: int, avail_w: int, avail_h: int, *, allow_grow: bool = False
) -> tuple[int, int, float]:
    """等比缩放图片至可用区域，返回 ``(cx', cy', scale)``。

    **只缩不放**（1.4 原则）：``allow_grow=False`` 时 ``scale`` 上限为 1.0，
    小图不会被拉伸——拉伸是"样式保真"的直接破坏，模板里的小图放大后
    一像素对一像素的截图会糊成一片。``allow_grow=True`` 只用于少数
    "必须铺满版心"的封面场景，且必须由档案显式声明。

    入参单位必须一致（EMU 或 twips），本函数不做换算。
    """
    if cx <= 0 or cy <= 0:
        raise EngineError(
            ErrorCode.SCHEMA_INVALID,
            f"图片原始尺寸非法：cx={cx}, cy={cy}",
            where={"cx": cx, "cy": cy},
            remediation=["检查 wp:extent 是否被外部工具写坏", "重新插入该图片"],
        )
    if avail_w <= 0 or avail_h <= 0:
        raise EngineError(
            ErrorCode.SCHEMA_INVALID,
            f"可用区域非法：avail_w={avail_w}, avail_h={avail_h}",
            where={"avail_w": avail_w, "avail_h": avail_h},
            remediation=["检查模板版心契约（页边距不得吃满纸宽）"],
        )

    scale = min(avail_w / cx, avail_h / cy)
    if not allow_grow:
        scale = min(scale, 1.0)
    new_cx = max(1, int(cx * scale))
    new_cy = max(1, int(cy * scale))
    return new_cx, new_cy, scale


def normalize_table_width(grid: list[int], avail_w: int) -> list[int]:
    """列宽按版心等比归一，保证 ``sum(result) == avail_w``。

    余数补偿不可省：等比后逐列向下取整会让总宽比版心窄几 twips，Word 会把这
    几 twips 加到最后一列上，于是"程序算的列宽"与"Word 渲染的列宽"永远对不上，
    任何基于列宽的比对都失去意义。
    """
    if not grid:
        return []
    if any(width < 0 for width in grid):
        raise EngineError(
            ErrorCode.SCHEMA_INVALID,
            f"列宽不得为负：{grid}",
            where={"grid": grid},
            remediation=["检查模板表格原型中的 tblGrid"],
        )
    if avail_w < MIN_COLUMN_TWIPS * len(grid):
        raise EngineError(
            ErrorCode.SCHEMA_INVALID,
            f"版心宽 {avail_w} twips 容不下 {len(grid)} 列（每列至少 {MIN_COLUMN_TWIPS} twips）",
            where={"avail_w": avail_w, "columns": len(grid)},
            remediation=["减少列数", "把该表改为横向分节", "调小页边距"],
        )

    total = sum(grid)
    if total <= 0:
        # 模板未声明列宽：按版心等分 + 余数补偿
        base = avail_w // len(grid)
        result = [base] * len(grid)
        for index in range(avail_w - base * len(grid)):
            result[index] += 1
        return result

    scaled = [width * avail_w / total for width in grid]
    result = [int(value) for value in scaled]
    remainder = avail_w - sum(result)
    # 余数给"被截掉最多"的列；并列时按下标升序，保证结果确定
    order = sorted(range(len(grid)), key=lambda i: (-(scaled[i] - result[i]), i))
    for offset in range(remainder):
        result[order[offset]] += 1

    # 兜底：极小列可能低于可读下限，从最宽列借
    for index, width in enumerate(result):
        if width >= MIN_COLUMN_TWIPS:
            continue
        deficit = MIN_COLUMN_TWIPS - width
        donor = max(range(len(result)), key=lambda i: result[i])
        if donor == index or result[donor] - deficit < MIN_COLUMN_TWIPS:
            continue
        result[donor] -= deficit
        result[index] = MIN_COLUMN_TWIPS
    return result


def is_overflow(cx: int, cy: int, avail_w: int, avail_h: int) -> bool:
    """图片是否越界。

    **唯一判据是 ``wp:extent`` 的 cx/cy**（FR-M5-07 / 1.2 第 6 条）：
    禁止用页数、渲染像素或 PDF 目检反推。此函数是这个判据的唯一实现。
    """
    return cx > avail_w or cy > avail_h


__all__ = [
    "EMU_PER_INCH",
    "EMU_PER_TWIP",
    "MIN_COLUMN_TWIPS",
    "emu_to_twips",
    "fit_picture",
    "is_overflow",
    "normalize_table_width",
    "pixels_to_emu",
    "twips_to_emu",
    "usable_height",
    "usable_height_emu",
    "usable_width",
    "usable_width_emu",
]
