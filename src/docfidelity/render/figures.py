"""图片自适应与图注绑定（DD-9.5 / FR-M4-07 / FR-M5-07 / FR-M3-08）。

三条纪律
--------
1. **越界的唯一判据是 ``wp:extent`` 的 ``cx/cy``**（FR-M5-07 / 1.2 第 6 条）。
   禁止用页数、渲染像素或 PDF 目检反推——那条路依赖渲染器，同一份文档在 Word
   与 LibreOffice 下会给出两个结论。判据的实现只有 ``ooxml.geometry.is_overflow``
   一处。
2. **只缩不放**。小图放大是"样式保真"的直接破坏：模板里一像素对一像素的截图
   放大后糊成一片。故所有缩放路径都走 ``fit_picture``（``allow_grow`` 默认 False）。
3. **尺寸只从 IR 拿像素 + DPI，换算在渲染层做**（``FigureData`` 契约）。IR 里出现
   twips 或 EMU 就是样式痕迹泄漏。

文档级缩放默认关闭 —— 一个不显然但重要的取舍
--------------------------------------------
模板前置版块（封面）里的图片**可能有意超出版心宽度**：封面图常按纸张宽度排，
缩到版心会留出两侧白边，整张封面当场变形。把"只缩不放"无差别地套到整个包，
会把封面图裁掉。故文档级缩放做成显式开关（``FigurePolicy.shrink_document``），
默认关闭；**注入的图片无论开关如何都会被缩到版心内**（``place_picture`` 内部
就调 ``fit_picture``）。与此同时，越界检查对全包生效并把发现报成 FR-M5-07。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from docfidelity.contracts.ir import ContentUnit, FigureData
from docfidelity.ooxml import (
    Element,
    Extent,
    ResizeReport,
    bind_caption,
    fit_picture,
    is_overflow,
    pixels_to_emu,
    place_picture,
    read_extents,
    resize_pictures,
    set_paragraph_text,
    usable_height_emu,
    usable_width_emu,
)

from .context import (
    FIGURE_OVERFLOW_RULE_ID,
    FIGURE_RESIZE_RULE_ID,
    TEMPLATE_FIDELITY_RULE_ID,
    RenderContext,
)

__all__ = [
    "CONTENT_COMPLETE_RULE_ID",
    "FIGURE_UNIT_TYPES",
    "FigurePlacement",
    "FigurePolicy",
    "assert_pictures_inside",
    "figure_data_of",
    "fit_document_pictures",
    "picture_overflow",
    "place_figure",
    "place_pending_figures",
]

#: 图片缺失（事实源里登记了 ``image_ref``，但工作区里取不到字节）属内容不完整。
CONTENT_COMPLETE_RULE_ID: Final = "FR-M5-09"

#: 图片单元的类型取值（``ContentUnit.type``）。
FIGURE_UNIT_TYPES: Final[tuple[str, ...]] = ("figure", "screenshot")


@dataclass(frozen=True)
class FigurePolicy:
    """图片策略。

    ``caption_style_id`` 为空时不猜样式：图注段落回落到**正文段落原型**的样式，
    并记一条 minor。硬编码一个"题注"样式名（中文模板与英文模板叫法不同）会让
    同一份代码在不同模板上给出不同结果，而两边都"看起来正常"。
    """

    caption_style_id: str | None = None
    shrink_document: bool = False
    max_height_ratio: float = 1.0
    alt_text: bool = True


@dataclass(frozen=True)
class FigurePlacement:
    """一张注入完成的图：图片段落 + 图注段落（顺序即插入顺序）。"""

    picture: Element
    caption: Element | None
    extent: Extent

    @property
    def elements(self) -> list[Element]:
        return [self.picture] if self.caption is None else [self.picture, self.caption]


def figure_data_of(unit: ContentUnit) -> FigureData:
    """把单元的 ``fields`` 读成 :class:`FigureData`（显式逐字段读，同表格的理由）。"""
    fields = unit.fields
    width = fields.get("width_px")
    height = fields.get("height_px")
    dpi = fields.get("dpi")
    return FigureData(
        image_ref=str(fields.get("image_ref") or ""),
        caption=fields.get("caption") if isinstance(fields.get("caption"), str) else None,
        caption_slot=(
            fields.get("caption_slot")
            if isinstance(fields.get("caption_slot"), int)
            and not isinstance(fields.get("caption_slot"), bool)
            else None
        ),
        width_px=width if isinstance(width, int) else None,
        height_px=height if isinstance(height, int) else None,
        dpi=dpi if isinstance(dpi, int) and dpi > 0 else 96,
    )


# ------------------------------------------------------------------ 放置
def place_figure(
    ctx: RenderContext,
    unit: ContentUnit,
    *,
    policy: FigurePolicy,
    paragraph: Element | None = None,
    alt_text: str | None = None,
) -> FigurePlacement | None:
    """放一张图并绑定图注；取不到图片字节时返回 ``None`` 并记 blocker。

    ``paragraph`` 由调用方给（DD-9.1 第 6 步已经把它排在正确位置上），为空时
    就地造一个。分开的原因：**位置属于结构，字节与缩放属于图片步骤**——注入步骤
    因此不必依赖资源加载器，也就不必知道工作区长什么样。承载段落的文本无论
    由谁提供都会被清空（图与文字同段共存是"看不见"的缺陷，见 ``_picture_paragraph``）。

    ``image_ref`` 是**工作区内的逻辑名**，由 ``ingest`` 落盘并登记。渲染层不认识
    工作区布局，故字节来源由调用方以 ``asset_loader`` 注入——渲染层不碰文件系统
    路径拼接，越界与命名规则于是都只有一处实现。
    """
    data = figure_data_of(unit)
    if not data.image_ref:
        ctx.note_once(
            CONTENT_COMPLETE_RULE_ID,
            "blocker",
            "figure-without-image-ref",
            "图片单元没有 image_ref，无法定位图片；该单元已被跳过",
            unit=unit,
            suggestion="检查事实源的图片接入配置，确认图片已被落盘并登记",
        )
        return None

    image = _load_asset(ctx, data.image_ref)
    if image is None:
        return None

    target = _picture_paragraph(ctx, unit, paragraph)
    if target is None:
        return None

    avail_w = usable_width_emu(ctx.template.page_setup)
    avail_h = int(usable_height_emu(ctx.template.page_setup) * max(0.0, policy.max_height_ratio))

    box = _declared_box(data)
    if box is not None:
        _, _, scale = fit_picture(box[0], box[1], avail_w, avail_h)
        if scale < 1.0:
            ctx.note(
                FIGURE_RESIZE_RULE_ID,
                "minor",
                f"图片按版心等比缩放（比例 {scale:.3f}），只缩不放",
                unit=unit,
                part=ctx.document_part,
                evidence=f"image_ref={data.image_ref}, scale={scale:.3f}",
                suggestion="确认图片原始尺寸是否过大；确需大图请改用横向分节",
            )

    extent = place_picture(
        ctx.pkg,
        target,
        image,
        filename=_filename_of(data.image_ref),
        avail_w_emu=avail_w,
        avail_h_emu=avail_h,
        owner=ctx.document_part,
        cx_emu=box[0] if box else None,
        cy_emu=box[1] if box else None,
        alt_text=(alt_text or data.caption or None) if policy.alt_text else None,
    )
    if is_overflow(extent.cx, extent.cy, avail_w, avail_h):
        ctx.note(
            FIGURE_OVERFLOW_RULE_ID,
            "blocker",
            "图片尺寸超出可用区域（判据：wp:extent 的 cx/cy）",
            unit=unit,
            part=ctx.document_part,
            evidence=f"cx={extent.cx}, cy={extent.cy}, avail=({avail_w}, {avail_h})",
            suggestion="检查版心契约与图片声明尺寸；不要用页数或 PDF 目检反推尺寸",
        )

    caption = _bind_caption(ctx, unit, target, data, policy=policy)
    return FigurePlacement(picture=target, caption=caption, extent=extent)


def place_pending_figures(ctx: RenderContext, *, policy: FigurePolicy) -> list[FigurePlacement]:
    """DD-9.1 第 8 步：把注入阶段登记好的待放图片逐张落位。

    只处理注入区内的图片；模板受保护区里的图片**不动**——那些图的尺寸是模板
    版式的一部分（封面图常按纸张宽排），按版心缩会当场把封面改形。
    """
    placements: list[FigurePlacement] = []
    for unit, paragraph in ctx.pending_figures:
        placement = place_figure(ctx, unit, policy=policy, paragraph=paragraph)
        if placement is not None:
            placements.append(placement)
    return placements


def _load_asset(ctx: RenderContext, image_ref: str) -> bytes | None:
    if ctx.asset_loader is None:
        ctx.note_once(
            CONTENT_COMPLETE_RULE_ID,
            "blocker",
            "no-asset-loader",
            "渲染未提供图片资源加载器，所有图片单元都会被跳过",
            evidence=f"image_ref={image_ref}",
            suggestion="在调用渲染时传入 asset_loader（image_ref -> 原始字节）",
        )
        return None
    try:
        return ctx.asset_loader(image_ref)
    except Exception as error:
        ctx.note_once(
            CONTENT_COMPLETE_RULE_ID,
            "blocker",
            f"asset-missing:{image_ref}",
            f"图片资源读取失败：{error}",
            evidence=f"image_ref={image_ref}",
            suggestion="确认图片仍在工作区内且未被保留策略清理",
        )
        return None


def _picture_paragraph(
    ctx: RenderContext, unit: ContentUnit, paragraph: Element | None
) -> Element | None:
    """图片承载段落：优先用调用方给的（注入步骤已排好位置），否则复用正文原型。

    无论哪一种都**清空文本**：承载段落里必须只有图形。不清空的后果是原型段落
    自带的示例/占位文字与图片同段共存——屏幕上图片盖住文字，评审时几乎看不出来，
    但导 PDF 或改字号就会看到一行字从图里钻出来。
    """
    target = paragraph if paragraph is not None else ctx.prototypes.body_paragraph()
    if target is None:
        ctx.note_once(
            TEMPLATE_FIDELITY_RULE_ID,
            "major",
            "no-paragraph-prototype",
            "模板里取不到正文段落原型，无法为图片单元提供承载段落；该单元已被跳过",
            unit=unit,
            suggestion="在模板的示例内容区保留至少一段正文，作为段落原型",
        )
        return None
    set_paragraph_text(target, "")
    return target


def _bind_caption(
    ctx: RenderContext,
    unit: ContentUnit,
    paragraph: Element,
    data: FigureData,
    *,
    policy: FigurePolicy,
) -> Element | None:
    """绑定图注。文本来自 IR（编号已由装配层填好），这里只负责落位与样式。

    位置固定在**图片段落之后**：``bind_caption`` 给图片段落挂 ``keepNext`` 并把
    图注放在紧邻的下一段——这就是"图与图注不分离"在 Word 里唯一可靠的手段。
    """
    caption_text = (data.caption or "").strip()
    if not caption_text:
        return None
    style_id = ctx.resolve_caption_style(policy.caption_style_id)
    caption = ctx.prototypes.body_paragraph()
    if caption is None:
        ctx.note_once(
            TEMPLATE_FIDELITY_RULE_ID,
            "major",
            "no-caption-prototype",
            "模板里取不到正文段落原型，无法为图注提供承载段落；该图注已被跳过",
            unit=unit,
            suggestion="在模板示例内容区保留正文段落原型",
        )
        return None
    # 紧邻段落可能是下一张图片或正文；先插入本图专属图注再交给绑定原语。
    paragraph.addnext(caption)
    return bind_caption(ctx.pkg, paragraph, caption_text, style_id=style_id)


def _declared_box(data: FigureData) -> tuple[int, int] | None:
    """IR 声明尺寸 -> EMU；未声明返回 ``None``（交给 ``place_picture`` 读图片头）。"""
    if data.width_px is None or data.height_px is None:
        return None
    return (
        pixels_to_emu(data.width_px, data.dpi),
        pixels_to_emu(data.height_px, data.dpi),
    )


def _filename_of(image_ref: str) -> str:
    """逻辑名 -> 落盘文件名。取末段，避免逻辑名里的目录结构影响 media 命名。"""
    tail = image_ref.replace("\\", "/").rpartition("/")[2]
    return tail or "image.png"


# ------------------------------------------------------------------ 全包检查
def fit_document_pictures(ctx: RenderContext) -> ResizeReport:
    """文档级"只缩不放"（**默认不由 ``render`` 调用**，理由见模块文档）。

    走 ``ooxml.pictures.resize_pictures``：它同时同步 ``wp:extent`` 与 DrawingML
    的 ``a:ext``，不同步的后果是"程序算的越界判定与 Word 的渲染结果不一致"，
    且只有真正打开文档时才发现。
    """
    return resize_pictures(
        ctx.pkg,
        avail_w_emu=usable_width_emu(ctx.template.page_setup),
        avail_h_emu=usable_height_emu(ctx.template.page_setup),
    )


def assert_pictures_inside(ctx: RenderContext, *, avail_w: int, avail_h: int) -> list[Extent]:
    """返回越界的图片（空列表表示全部在版心内），并为每一张记一条 blocker。

    只报不改：越界判定与缩放是两件事。混在一起会让"检查"顺带改了产物，
    而门禁报告里的数字从此再也对不上产物。
    """
    offenders: list[Extent] = []
    for extent in read_extents(ctx.pkg):
        if not is_overflow(extent.cx, extent.cy, avail_w, avail_h):
            continue
        offenders.append(extent)
        ctx.note_once(
            FIGURE_OVERFLOW_RULE_ID,
            "blocker",
            f"image-overflow:{extent.part}:{extent.xpath}",
            "图片尺寸超出可用区域（判据：wp:extent 的 cx/cy）",
            part=extent.part,
            evidence=f"xpath={extent.xpath}, cx={extent.cx}, cy={extent.cy}",
            suggestion="缩小该图，或把它所在分节改为横向",
        )
    return offenders


def picture_overflow(ctx: RenderContext) -> list[Extent]:
    """全包图片越界检查（版心取模板契约）。"""
    return assert_pictures_inside(
        ctx,
        avail_w=usable_width_emu(ctx.template.page_setup),
        avail_h=usable_height_emu(ctx.template.page_setup),
    )
