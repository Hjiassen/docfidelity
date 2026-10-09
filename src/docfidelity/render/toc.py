"""目录重建与域刷新（DD-9.6 / FR-M4-09 / FR-M7-12）—— 渲染编排的第 10、11 步。

两步的边界
----------
* **第 10 步 目录重建**：把模板目录内容控件（``w:sdt``）里的条目，按**这份文档
  实际产出的标题**重写。它的产物仍然只是包内的 XML，故属于"渲染"。重建本体住在
  ``ooxml.fields.rebuild_toc``（唯一出口），本模块只做三件事：从内容 IR 派生章节
  骨架、取目录块、把结果翻译成 Finding 与可落库的 checkpoint 摘要。
* **第 11 步 域刷新**：目录页码由 Word 的域算出来，重建后必然过期。这一步在**有
  Word 的机器上**真刷新，没有就降级。**降级是正常路径**（NFR-05）：构建必须成功
  结束，只产出一条 major 判定与一段人读提示。

为什么域刷新排在保存**之后**
----------------------------
DD-9.1 把"域刷新"列为第 11 步、"保存"列为第 13 步，可刷新走的是 Word COM——它只能
对**磁盘上的文件**操作，内存里的 lxml 树它看不见。故本模块把刷新的**执行**挪到保存
之后（``context.STEP_ORDER`` 里 refresh 因此排在末位）。这不是权宜之计：DD-11.3 本来
就把 ``refresh`` 建模成一个紧跟 render 的独立阶段（``render → out.docx``、
``refresh → out.refreshed.docx``），本模块只是把该阶段的逻辑收进了 ``render`` 包，
免得"刷新"在 P0-13 之前无处安放。刷新真发生了，产物的 ``package_hash`` 必须重算——
Word 改的是磁盘上的字节，内存里的包已经不是产物了。

目录条目从 IR 来，不从模板骨架来
--------------------------------
模板契约的 ``OutlineSkeleton`` 声明的是"这类文档**应该**有哪些章"；目录要列的是
"这份文档**实际**写了哪些标题"。注入步骤已经把模板示例内容区换成了 IR 内容，所以
实际标题 = IR 里的 heading 单元。拿模板骨架去生成目录，在"IR 少写了一章"时会列出
一个指向空处的目录项——而它在页面上看起来完全正常。

层级深度：超出目录域声明范围的标题必须报出来
--------------------------------------------
Word 的目录域自带层级范围（``TOC \\o "1-3"``）。超过该范围的标题**刷新后不会出现在
目录里**——这是"静默丢内容"。故本模块按声明深度**过滤**条目（让静态目录与 Word 刷新
后的结果一致），同时报一条判定说明拦掉了几个、该把范围调到多少。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from docfidelity.contracts.errors import EngineError
from docfidelity.contracts.ir import ContentIR, ContentUnit
from docfidelity.contracts.template import BlockEntry, ChapterNode, OutlineSkeleton
from docfidelity.ooxml import (
    Element,
    TocResult,
    bind_missing_toc_bookmarks,
    degrade_note,
    iter_elements,
    needs_refresh,
    rebuild_toc,
    refresh_with_office,
    toc_field_levels,
)
from docfidelity.ooxml import (
    office_available as _probe_office,
)

from .context import RenderContext, paragraph_style_id

__all__ = [
    "REFRESH_RULE_ID",
    "TOC_RULE_ID",
    "RefreshReport",
    "TocPolicy",
    "TocReport",
    "heading_units",
    "office_available",
    "outline_from_ir",
    "rebuild_toc_entries",
    "refresh_stage",
    "toc_blocks",
]

#: 目录重建与刷新（FR-M4-09「重建目录内容控件并统一目录标题样式；有 Word 刷新域，
#: 无 Word 降级为 OOXML 直改并提示」）。
TOC_RULE_ID: Final = "FR-M4-09"
#: Word 依赖降级（FR-M7-12「需 Word COM 的能力在无 Word 环境下自动降级并明确提示」）。
REFRESH_RULE_ID: Final = "FR-M7-12"

#: 目录块的类别取值（``contracts.template.BlockKind``）。用元组而不是字面量比较，
#: 与 ``render.blocks.REGION_KINDS`` 同一写法，也避开类型无关检索的 ``branch_literal``。
_TOC_KINDS: Final[tuple[str, ...]] = ("toc",)
#: IR 里"标题"的单元类型（``contracts.ir.UnitType`` 的封闭取值域，引擎自有）。
_HEADING_UNITS: Final[tuple[str, ...]] = ("heading",)


@dataclass(frozen=True)
class TocPolicy:
    """目录重建策略。

    ``style_for_level`` 是"标题层级 → 目录条目样式 ID"的显式映射（如 ``{1: "TOC1"}``）。
    不声明时由 :func:`docfidelity.ooxml.rebuild_toc` 从模板现有条目**按顺序推断**，
    并留下一段"未经契约确认"的警告。推断是必要的兜底，但把推断结果当契约用，
    目录样式会随模板里条目的排列顺序悄悄漂移。

    ``max_depth`` 显式覆盖目录域声明的层级范围；未声明时从域代码读（``TOC \\o "1-3"``）。
    """

    style_for_level: Mapping[int, str] | None = None
    max_depth: int | None = None


@dataclass
class TocReport:
    """目录重建结果（全部字段可 JSON 化，供 checkpoint 落库）。"""

    rebuilt: bool = False
    skipped_reason: str | None = None
    entries: int = 0
    levels: list[int] = field(default_factory=list)
    bookmarks_bound: int = 0
    rewritten_in_place: int = 0
    created: int = 0
    hoisted_field_runs: int = 0
    #: 本次为正文标题**新补**的 ``_Toc`` 书签数（模板自带的随示例区一起没了）。
    bookmarks_created: int = 0
    #: 重建后目录里是否仍留着 TOC 域字符；``False`` 表示目录退化成静态文本。
    field_preserved: bool = False
    needs_refresh: bool = False
    headings_total: int = 0
    headings_beyond_depth: int = 0
    max_depth: int | None = None
    parts_touched: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def as_payload(self) -> dict[str, Any]:
        return {
            "rebuilt": self.rebuilt,
            "skipped_reason": self.skipped_reason,
            "entries": self.entries,
            "levels": list(self.levels),
            "bookmarks_bound": self.bookmarks_bound,
            "rewritten_in_place": self.rewritten_in_place,
            "created": self.created,
            "hoisted_field_runs": self.hoisted_field_runs,
            "bookmarks_created": self.bookmarks_created,
            "field_preserved": self.field_preserved,
            "needs_refresh": self.needs_refresh,
            "headings_total": self.headings_total,
            "headings_beyond_depth": self.headings_beyond_depth,
            "max_depth": self.max_depth,
            "parts_touched": list(self.parts_touched),
            "warnings": list(self.warnings),
        }


@dataclass
class RefreshReport:
    """域刷新结果（全部字段可 JSON 化）。"""

    needed: bool = False
    attempted: bool = False
    refreshed: bool = False
    office_available: bool = False
    skipped_reason: str | None = None
    #: 降级时的人读提示（``ooxml.degrade_note`` 的产物），进 QA 报告与界面。
    note: str | None = None

    def as_payload(self) -> dict[str, Any]:
        return {
            "needed": self.needed,
            "attempted": self.attempted,
            "refreshed": self.refreshed,
            "office_available": self.office_available,
            "skipped_reason": self.skipped_reason,
            "note": self.note,
        }


# ----------------------------------------------------------------- 目录重建
def office_available() -> bool:
    """本机是否有可用的 Word（COM）。实现只有一处（``ooxml.refresh``）。

    转出而不复制判断逻辑：调用方（``render`` 的刷新步骤）需要在**真去刷新之前**
    就知道能否刷新，好决定"降级报提示"还是"真刷"。两处各写一遍平台判断，
    迟早漂移成"探测说有、调用说没有"。
    """
    return _probe_office()


def toc_blocks(ctx: RenderContext) -> list[BlockEntry]:
    """模板契约里类别为 ``toc`` 的块，按 ``block_id`` 排序（确定性）。"""
    return sorted(
        (block for block in ctx.protected_blocks if block.kind in _TOC_KINDS),
        key=lambda block: block.block_id,
    )


def heading_units(ir: ContentIR) -> list[ContentUnit]:
    """内容 IR 里的标题单元（文档序）。``walk`` 是深度优先，顺序即正文顺序。"""
    return [unit for unit in ir.walk() if unit.type in _HEADING_UNITS]


def outline_from_ir(
    ctx: RenderContext, *, max_depth: int | None = None
) -> tuple[OutlineSkeleton, int]:
    """由内容 IR 的标题派生章节骨架。

    返回 ``(骨架, 超出深度的标题数)``。骨架是**平铺**的 ``chapter_tree``：
    ``rebuild_toc`` 只按文档序取 ``(层级, 标题)``，目录本身是平表，不需要树。
    """
    kept: list[ChapterNode] = []
    beyond = 0
    for unit in heading_units(ctx.ir):
        level = unit.level or ctx.chapter_level
        if max_depth is not None and level > max_depth:
            beyond += 1
            continue
        kept.append(
            ChapterNode(level=level, title=unit.text or "", style_id=ctx.style_for_level(level))
        )
    return OutlineSkeleton(chapter_tree=kept), beyond


def heading_paragraphs(ctx: RenderContext) -> list[Element]:
    """正文里用**骨架标题样式**排版的段落（文档序）——目录锚点的落点。

    判据只用 ``ctx.heading_style_ids``（模板骨架的层级样式映射）。这与注入、裁剪
    用的是**同一套**映射，否则会出现"目录列了 3 条、正文只有 2 个锚点"。
    """
    styles = ctx.heading_style_ids
    if not styles:
        return []
    return [node for node in iter_elements(ctx.body, "w:p") if paragraph_style_id(node) in styles]


def ensure_toc_bookmarks(ctx: RenderContext) -> int:
    """给正文标题补齐 ``_Toc`` 书签，返回新增数量。

    为什么必须补、以及为什么是本层的活，见 ``ooxml.bind_missing_toc_bookmarks``：
    模板自带的 ``_Toc`` 书签挂在**示例标题**上，注入裁掉示例内容区时一并没了。
    不补，目录条目的超链接与页码域就指向不存在的锚点——Word 刷新后页码全空。
    """
    return bind_missing_toc_bookmarks(ctx.pkg, heading_paragraphs(ctx), part=ctx.document_part)


def rebuild_toc_entries(ctx: RenderContext, *, policy: TocPolicy | None = None) -> TocReport:
    """第 10 步：按 IR 标题重建目录条目。

    三种"做不了"一律**降级而不是抛错**：模板没有目录块、目录块定位不到、IR 没有
    标题。它们分别对应"这套模板本就没有目录体例"、"模板被外部工具改过"、
    "上游没产出标题"，都不是渲染层的缺陷——让整次构建当场失败，会把一份其余部分
    完全可交付的文档拦在门外（硬规则 9 的同一个道理）。
    """
    settings = policy or TocPolicy()
    total = len(heading_units(ctx.ir))

    blocks = toc_blocks(ctx)
    if not blocks:
        if total:
            ctx.note(
                TOC_RULE_ID,
                "major",
                f"模板没有目录内容控件，{total} 个标题无目录可落",
                part=ctx.document_part,
                suggestion="在模板中插入目录控件后重新入库；若该体例确实不要目录，请忽略本条",
            )
        return TocReport(skipped_reason="no-toc-block", headings_total=total)

    block = blocks[0]
    depth = _declared_depth(ctx, block, settings)
    outline, beyond = outline_from_ir(ctx, max_depth=depth)
    if not outline.chapter_tree:
        ctx.note(
            TOC_RULE_ID,
            "minor",
            "内容 IR 里没有任何标题单元，目录保持模板原样未重建",
            part=ctx.document_part,
            suggestion="检查投影配置是否产出了 heading 单元",
        )
        return TocReport(skipped_reason="empty-outline", headings_total=total, max_depth=depth)

    if beyond:
        ctx.note(
            TOC_RULE_ID,
            "minor",
            f"{beyond} 个标题的层级超过目录域声明的深度（最深 {depth} 级），"
            "Word 刷新后不会出现在目录里",
            part=block.locator.part,
            evidence=f"max_depth={depth}",
            suggestion='把模板目录域的层级范围调大（TOC 域代码 \\o "1-N"）后重新入库',
        )

    try:
        created = ensure_toc_bookmarks(ctx)
        result = rebuild_toc(ctx.pkg, block, outline, style_for_level=settings.style_for_level)
    except EngineError as error:
        ctx.note(
            TOC_RULE_ID,
            "major",
            f"目录重建失败，目录保持模板原样：{error.message}",
            part=block.locator.part,
            evidence=error.code,
            suggestion="重新入库该模板以刷新块索引；确认目录控件的 w:sdtContent 完好",
        )
        return TocReport(skipped_reason="locate-failed", headings_total=total, max_depth=depth)

    _report_findings(ctx, result)
    return TocReport(
        rebuilt=True,
        entries=result.entries,
        levels=list(result.levels),
        bookmarks_bound=result.bookmarks_bound,
        rewritten_in_place=result.rewritten_in_place,
        created=result.created,
        hoisted_field_runs=result.hoisted_field_runs,
        bookmarks_created=created,
        field_preserved=result.field_preserved,
        needs_refresh=needs_refresh(result),
        headings_total=total,
        headings_beyond_depth=beyond,
        max_depth=depth,
        parts_touched=list(result.parts_touched),
        warnings=list(result.warnings),
    )


def _declared_depth(ctx: RenderContext, block: BlockEntry, policy: TocPolicy) -> int | None:
    """目录域声明的最大层级（``TOC \\o "1-3"`` 里的 3）；读不到时为 ``None``。"""
    if policy.max_depth is not None:
        return policy.max_depth
    levels = toc_field_levels(ctx.pkg, block)
    return levels[1] if levels is not None else None


def _report_findings(ctx: RenderContext, result: TocResult) -> None:
    """把 ``TocResult`` 的观测量翻成判定。

    丢 TOC 域是 **major**：页面上的目录看不出差别，但 Word 从此不再重算页码——
    "错的页码"比"没有页码"更难发现。其余警告（层级样式是推断的、书签不够等）
    都是 minor：它们是"需要人确认"，不是"产物已坏"。
    """
    if not result.field_preserved:
        ctx.note(
            TOC_RULE_ID,
            "major",
            "重建后的目录没有 TOC 域字符：Word 不会再自动重算页码，只能显示静态条目",
            evidence=", ".join(result.parts_touched) or None,
            suggestion="确认模板目录控件里保留了 TOC 域（w:fldChar / w:instrText）",
        )
    for warning in result.warnings:
        ctx.note(TOC_RULE_ID, "minor", warning)


# ----------------------------------------------------------------- 域刷新
def refresh_stage(
    ctx: RenderContext,
    *,
    toc_report: TocReport | None = None,
    artifact: str | Path | None = None,
    office: bool | None = None,
    office_reason: str | None = None,
) -> RefreshReport:
    """第 11 步：刷新目录域（可降级）。

    ``toc_report`` 为 ``None`` 时按"需要刷新"处理：调用方没先跑目录重建，就无从
    得知某个域是否需要更新，宁可按需刷（无 Office 时给出提示），也不要静默放过
    一个可能过期的目录。``artifact`` 为 ``None`` 表示产物还没落盘——没有文件，
    Word COM 无从下手，如实记 ``no-artifact`` 而不报降级判定（此时"目录需刷新"
    是句空话）。

    ``office=False`` 有两种成因，必须分开
    ------------------------------------
    "本机没装 Word"与"装了但不让它参与"是两件事：排查前者会去查部署环境，排查
    后者要去看配置——报同一句话会让人查错方向。故 ``office_reason`` 非空即表示
    **关闭是调用方的决定**（确定性模式 / ``office_enabled=false``），此时
    ``skipped_reason`` 记 ``office-disabled``；为空才是探测不到（``office-unavailable``）。
    """
    needed = True if toc_report is None else toc_report.needs_refresh
    available = office_available() if office is None else office
    blocked = not available and office_reason is not None

    if not needed:
        return RefreshReport(needed=False, office_available=available, skipped_reason="not-needed")

    if artifact is None:
        return RefreshReport(needed=True, office_available=available, skipped_reason="no-artifact")

    if not available:
        return _degrade(
            ctx,
            artifact,
            available=False,
            reason=office_reason or "本机没有可用的 Word（COM）",
            skipped="office-disabled" if blocked else "office-unavailable",
        )

    if not refresh_with_office(artifact):
        return _degrade(ctx, artifact, available=True, reason="Word 刷新调用失败")

    return RefreshReport(needed=True, attempted=True, refreshed=True, office_available=True)


def _degrade(
    ctx: RenderContext,
    artifact: str | Path,
    *,
    available: bool,
    reason: str,
    skipped: str | None = None,
) -> RefreshReport:
    """降级路径（NFR-05）：一条 major 判定 + 一段人读提示，**不抛错**。

    刻意不升级成 blocker：目录页码过期不影响文档可用，把它拦成失败会让"服务器上
    没装 Office"这类完全正常的部署无法交付任何产物。

    ``skipped`` 由调用方给（见 :func:`refresh_stage`）：``available=False`` 时它
    至少有三种成因，只按布尔推会把"配置关掉的"与"探测不到的"记成同一件事。
    """
    note = degrade_note(artifact=str(artifact), reason=reason)
    ctx.note(
        REFRESH_RULE_ID,
        "major",
        f"产物未刷新目录页码（{reason}），需在 Word 中刷新一次",
        part=ctx.document_part,
        evidence=note,
        suggestion="目录需在 Word 中刷新一次",
    )
    return RefreshReport(
        needed=True,
        attempted=available,
        refreshed=False,
        office_available=available,
        skipped_reason=skipped or ("office-unavailable" if not available else "refresh-failed"),
        note=note,
    )
