"""渲染流水线（DD-9.1）—— 步骤编排、写盘护栏与产物。

DD-9.1 的 13 步已全部落地：第 1~9 步做结构（版块 / 裁剪 / 样式 / 编号 / 注入 / 表格 /
图片 / 分页），第 10 步重建目录，第 11 步刷新域，第 12~13 步写属性与保存。
:data:`context.DEFERRED_STEPS` 现在为空，但**保留着**——它让"还缺哪几步"始终是
代码可见的，而不是靠人去比对 DD 的编号。

一处对 DD-9.1 顺序的偏离：**域刷新的执行排在保存之后**
-----------------------------------------------------
DD-9.1 把"域刷新"列为第 11 步、保存列为第 13 步。但刷新走 Word COM，只能对磁盘上的
文件操作——内存里的 lxml 树它看不见。故 :func:`render` 先保存（第 13 步），再执行
刷新；``context.STEP_ORDER`` 里 refresh 因此排在末位。DD-11.3 本身也把 refresh 建模成
紧跟 render 的独立阶段，所以这一步不是妥协，而是把那个阶段的逻辑收进了本包。

刷新真发生了就**必须重算 package_hash**：Word 改的是磁盘上的字节，内存里的包已经
不是产物了。不重算等于给产物贴一个属于"刷新前版本"的指纹——版本 diff 与 golden
比对都会据此得出错误结论。

步骤函数签名的偏离
------------------
DD 写的是 ``(doc, ctx) -> StepResult``。本轮每个步骤函数改为"收 :class:`RenderContext`、
返回自己的报告对象"，由 :func:`render` 统一收进 :class:`StepResult`。理由：
"步骤产物长什么样"只有该步骤自己清楚（表格报告 / 分页报告 / 属性报告各不相同），
为统一签名把它们都塞进一个泛型 dict，会让类型检查在这里失效——而 ``render``
正是 ``mypy --strict`` 覆盖的模块。**"每一步都可单独调用"这条验收不受影响**：
每个步骤函数都是公开的、可独立调用的。

写盘护栏（DD-10.7「输出护栏」）
--------------------------------
DD 要求在**第 13 步之前**拦一次：样式引用不完整、占位残留都属"产物已坏但看不出来"。
命中护栏 **blocker** 时不写盘——生成一份"能打开但样式静默回退"的文档，比构建失败更糟：
它会被当成成品交出去。

**严重度与"是否拦写盘"是两件事**
--------------------------------
``Finding.severity == "blocker"`` 是**规则**层面的判定（DD-10.2 的规则表；由 ``qa``
决定阻断与否），而"这一步该不该落盘"是渲染层护栏的判断。二者刻意不划等号：

* 护栏这一组（样式引用悬挂、占位残留）→ **不写盘**。这些都是"产物本身已坏"。
* 内容不完整的 blocker（图片取不到字节、单元类型没人认得）→ **照报、照样写盘**。
  硬规则 9 要求"模型 / 资源不可用时构建不得失败"：少一张图的文档仍然是有用的
  中间产物，交给人去补比当场失败更符合"低置信不静默采用"的处理方式。

把"blocker 即不写盘"当成通则，会让"模板里有一张图没接上"升级成整条链路失败；
反过来把护栏也降级成"照写不误"，就会交付一份样式静默回退的文档。

一处刻意不做的检查：**全包图片越界**
------------------------------------
模板前置版块（封面）的图片**可能有意超出版心宽度**（按纸张宽度排的封面图），
把它们判成"越界 blocker"会让每一份带满幅封面的模板都构建失败。故越界判定
只施加在**注入的图片**上（第 8 步内部完成）；模板自带图片的越界由 ``qa``
在渲染目检（FR-M5-07 / FR-M5-12）中以"区分区"的口径处理。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from docfidelity.contracts.config import PaginationPolicy
from docfidelity.contracts.errors import EngineError
from docfidelity.contracts.ir import ContentIR
from docfidelity.contracts.profile import DocTypeProfile
from docfidelity.contracts.qa import Finding
from docfidelity.contracts.template import TemplatePackage
from docfidelity.ooxml import (
    DOCUMENT_PART,
    NUMBERING_PART,
    STYLES_PART,
    DocxPackage,
    align_style_definitions,
    assert_no_text_numbering,
    heading_num_bindings,
    normalized_digest,
    unify_numbering,
    validate_style_refs,
)

from .blocks import clear_protected_placeholders, locate_regions, placeholder_residue
from .context import (
    DEFERRED_STEPS,
    PLACEHOLDER_RULE_ID,
    STEP_ORDER,
    TEMPLATE_FIDELITY_RULE_ID,
    RenderContext,
    StepResult,
)
from .figures import FigurePolicy, place_pending_figures
from .inject import InjectionPolicy, inject_chapters
from .pagination import apply_pagination
from .properties import PropertiesPolicy, apply_properties, output_filename
from .removal import RemovalRule, apply_removals
from .tables import TablePolicy, normalize_injected_tables
from .toc import TocPolicy, rebuild_toc_entries, refresh_stage

__all__ = [
    "DEFERRED_STEPS",
    "STEP_ORDER",
    "RenderPolicy",
    "RenderResult",
    "open_working_copy",
    "render",
]


@dataclass(frozen=True)
class RenderPolicy:
    """一次渲染的全部策略。

    ``caption_style_id`` 是**唯一的**题注样式声明入口：设了就同时作用于表注与图注，
    避免"表格的题注对齐了、图片的没对齐"这种只在某一类内容上出现的偏差。

    ``pagination.keep_table_header_row`` 与 ``table.repeat_header_row`` 是**同一个开关**：
    ``render`` 用前者派生后者（DD-9.4 的"跨页重复表头"）。两处各设一次，会让
    "关掉重复表头"这个操作只有一处生效，而那种"开关没反应"的缺陷最难定位。
    """

    caption_style_id: str | None = None
    removal_rules: tuple[RemovalRule, ...] = ()
    injection: InjectionPolicy = field(default_factory=InjectionPolicy)
    table: TablePolicy = field(default_factory=TablePolicy)
    figure: FigurePolicy = field(default_factory=FigurePolicy)
    pagination: PaginationPolicy = field(default_factory=PaginationPolicy)
    toc: TocPolicy = field(default_factory=TocPolicy)
    properties: PropertiesPolicy = field(default_factory=PropertiesPolicy)

    def resolved_injection(self) -> InjectionPolicy:
        if self.caption_style_id is None:
            return self.injection
        return replace(self.injection, caption_style_id=self.caption_style_id)

    def resolved_figure(self) -> FigurePolicy:
        if self.caption_style_id is None:
            return self.figure
        return replace(self.figure, caption_style_id=self.caption_style_id)

    def resolved_table(self) -> TablePolicy:
        return replace(self.table, repeat_header_row=self.pagination.keep_table_header_row)


@dataclass
class RenderResult:
    """渲染产物与全过程判定（DD-9.1 的 ``StepResult`` 列表 + 产物信息）。"""

    filename: str
    #: 本次渲染所依据的档案与模板引用——续跑时用来确认"还是同一套输入"。
    profile_id: str = ""
    template_ref: str = ""
    steps: list[StepResult] = field(default_factory=list)
    artifact_path: Path | None = None
    package_hash: str | None = None
    #: 本轮未实现的步骤（代码可见，见模块文档）。
    deferred_steps: tuple[str, ...] = DEFERRED_STEPS
    #: 写盘护栏命中的 blocker；非空表示**没有写盘**。
    guardrail_blockers: list[Finding] = field(default_factory=list)

    # ------------------------------------------------------------ 判定汇总
    def findings(self) -> list[Finding]:
        """全部步骤的判定（按步骤顺序）。"""
        return [item for step in self.steps for item in step.findings]

    def blockers(self) -> list[Finding]:
        return [item for item in self.findings() if item.severity == "blocker"]

    @property
    def written(self) -> bool:
        """产物是否已落盘。"""
        return self.artifact_path is not None

    def counts(self) -> dict[str, int]:
        counts = {"blocker": 0, "major": 0, "minor": 0}
        for item in [*self.findings(), *self.guardrail_blockers]:
            counts[item.severity] = counts.get(item.severity, 0) + 1
        return counts

    def checkpoint_payload(self) -> dict[str, Any]:
        """可 JSON 化的过程摘要（TASK-P0-13 的 checkpoint 落库用）。"""
        return {
            "filename": self.filename,
            "profile_id": self.profile_id,
            "template_ref": self.template_ref,
            "artifact": str(self.artifact_path) if self.artifact_path else None,
            "package_hash": self.package_hash,
            "written": self.written,
            "deferred_steps": list(self.deferred_steps),
            "counts": self.counts(),
            "steps": [
                {"step": step.step, "counts": step.counts(), "outputs": step.checkpoint_outputs}
                for step in self.steps
            ],
        }


def render(
    *,
    pkg: DocxPackage,
    template: TemplatePackage,
    profile: DocTypeProfile,
    ir: ContentIR,
    out_path: str | Path,
    policy: RenderPolicy | None = None,
    document_part: str = DOCUMENT_PART,
    asset_loader: RenderContext.AssetLoader | None = None,
    office: bool | None = None,
    office_reason: str | None = None,
) -> RenderResult:
    """把内容 IR 渲染成 ``.docx`` 并落盘。

    ``pkg`` 必须是**可写的工作副本**（:func:`open_working_copy`）。模板原件不会被
    写回——第 1 步"深拷贝"在本项目里是**结构性成立**的：``DocxPackage.open`` 一次性
    把全部部件读进内存，``save(dest)`` 只往 ``dest`` 写，原件自始至终只被读。
    故"深拷贝"不需要一次额外的整包复制，也就不会有"复制漏了某个部件"的风险。

    ``office`` 显式声明"本机是否有可用的 Word（COM）"：``None`` 时自动探测。让调用方
    能传入不只是为了测试——一次构建一个文档族时，探测一次就够，不必每份文档各探一遍。
    ``office=False`` 的成因写进 ``office_reason``（确定性模式关掉它时给文案），
    否则报告会把"配置关掉的"报成"本机没装"——两者的排查方向完全不同。
    """
    settings = policy or RenderPolicy()
    ctx = RenderContext(
        pkg=pkg,
        template=template,
        profile=profile,
        ir=ir,
        document_part=document_part,
        asset_loader=asset_loader,
        protected_blocks=template.blocks.blocks,
    )
    injection_policy = settings.resolved_injection()
    figure_policy = settings.resolved_figure()
    table_policy = settings.resolved_table()

    result = RenderResult(
        filename=output_filename(ir, policy=settings.properties),
        profile_id=profile.profile_id,
        template_ref=ir.template_ref,
    )

    # 1+2 版块：定位受保护区 → 清占位符（替换那一半待项目元数据契约落地）。
    regions = locate_regions(ctx)
    placeholders = clear_protected_placeholders(ctx, regions)
    result.steps.append(
        _step(
            ctx,
            "blocks",
            {
                "regions": len(regions),
                "region_ids": sorted(ctx.regions),
                **placeholders.as_payload(),
            },
            touched=placeholders.touched_parts,
        )
    )

    # 3 冗余裁剪：按规则整块移除模板冗余（标题 + 段落 + 表格）。
    removal = apply_removals(ctx, settings.removal_rules)
    result.steps.append(
        _step(
            ctx,
            "removal",
            removal.as_payload(),
            touched=[document_part] if removal.removed_elements else [],
        )
    )

    # 4 样式对齐：只同步定义内容，保留 styleId（正文引用一律不动）。
    align = align_style_definitions(pkg, template.styles)
    result.steps.append(
        _step(
            ctx,
            "styles",
            {
                "updated": len(align.updated),
                "unchanged": len(align.unchanged),
                "missing_in_target": sorted(align.missing_in_target),
            },
            touched=[STYLES_PART] if align.updated else [],
        )
    )

    # 5 编号统一 + 标题编号绑定：合并等价定义、压缩实例，并检出双重编号。
    unify = unify_numbering(pkg, template.numbering)
    ctx.findings.extend(assert_no_text_numbering(ir, template.outline, heading_num_bindings(pkg)))
    result.steps.append(
        _step(
            ctx,
            "numbering",
            {
                "merged_definitions": unify.merged_definitions,
                "repointed_instances": unify.repointed_instances,
                "removed_instances": len(unify.removed_instances),
                "warnings": list(unify.warnings),
            },
            touched=[NUMBERING_PART] if unify.merged_definitions else [],
        )
    )

    # 6 章节注入：裁掉模板示例内容区，按 IR 重建（只做结构）。
    injection = inject_chapters(ctx, policy=injection_policy)
    result.steps.append(_step(ctx, "inject", injection.as_payload(), touched=[document_part]))

    # 7 表格规范化：列宽归一 / 表头 / 跨页重复 / 单元格版式固定。
    tables = normalize_injected_tables(ctx, policy=table_policy)
    result.steps.append(
        _step(
            ctx,
            "tables",
            {"tables": [item.as_payload() for item in tables]},
            touched=[document_part] if tables else [],
        )
    )

    # 8 图片自适应：只缩不放 + 图注绑定（只处理注入区内的图片）。
    figures = place_pending_figures(ctx, policy=figure_policy)
    result.steps.append(
        _step(
            ctx,
            "figures",
            {
                "placed": len(figures),
                "captions": sum(1 for item in figures if item.caption is not None),
            },
            touched=[document_part] if figures else [],
        )
    )

    # 9 分页策略：清除残留 → 按白名单重建 → 落实成组开关 → 校验。
    pagination = apply_pagination(ctx, settings.pagination)
    result.steps.append(
        _step(
            ctx,
            "pagination",
            pagination.as_payload(),
            touched=[document_part] if pagination.applied_breaks else [],
        )
    )

    # 10 目录重建：按 IR 标题重写模板目录内容控件（条目文本 / 锚点 / 层级样式）。
    toc = rebuild_toc_entries(ctx, policy=settings.toc)
    result.steps.append(_step(ctx, "toc", toc.as_payload(), touched=toc.parts_touched))

    # 12 属性与命名。
    properties = apply_properties(ctx, policy=settings.properties)
    result.filename = properties.filename
    result.steps.append(
        _step(ctx, "properties", properties.as_payload(), touched=properties.touched_parts)
    )

    # 写盘护栏：产物已坏但看不出来的那些情形，在这里拦一次。
    result.guardrail_blockers = _guardrails(ctx)
    if result.guardrail_blockers:
        # 没写盘就没有可刷新的文件。刷新步照常登记（步骤序列不因写盘失败而缺项），
        # 但如实记 ``no-artifact``——此时说"目录需刷新"是句空话，也不该产生判定。
        result.steps.append(
            _step(
                ctx,
                "refresh",
                refresh_stage(
                    ctx,
                    toc_report=toc,
                    artifact=None,
                    office=office,
                    office_reason=office_reason,
                ).as_payload(),
            )
        )
        return result

    # 13 保存（原子写）+ 记录 package_hash。
    result.package_hash = normalized_digest(pkg)
    result.artifact_path = pkg.save(out_path)

    # 11 域刷新——执行在保存之后（理由见模块文档）；刷新成功则产物摘要必须重算。
    refresh = refresh_stage(
        ctx,
        toc_report=toc,
        artifact=result.artifact_path,
        office=office,
        office_reason=office_reason,
    )
    result.steps.append(_step(ctx, "refresh", refresh.as_payload()))
    if refresh.refreshed:
        result.package_hash = _rehash(result.artifact_path)
    return result


def open_working_copy(template_path: str | Path) -> DocxPackage:
    """打开模板的**可写工作副本**。

    ``read_only=False`` 只影响"能否 save"，不影响源文件：包内容在打开时已全部
    读进内存，``save`` 写的是另一个路径。故"原件永不修改"由结构保证，而不是
    靠调用方自觉。
    """
    return DocxPackage.open(template_path, read_only=False)


# ------------------------------------------------------------------- 内部
def _step(
    ctx: RenderContext,
    name: str,
    outputs: dict[str, Any],
    *,
    touched: list[str] | None = None,
) -> StepResult:
    """把当前累积的判定收进一个 ``StepResult``，并附上该步骤的产物摘要。"""
    result = ctx.drain_findings(name)
    result.checkpoint_outputs = dict(outputs)
    result.touched_parts = list(touched or ())
    return result


def _guardrails(ctx: RenderContext) -> list[Finding]:
    """写盘前护栏（DD-10.7）。命中 blocker 即不写盘，见模块文档。"""
    blockers: list[Finding] = []

    missing = validate_style_refs(ctx.pkg, ctx.template.styles)
    if missing:
        blockers.append(
            ctx.note(
                TEMPLATE_FIDELITY_RULE_ID,
                "blocker",
                f"产物引用了 {len(missing)} 个未定义的样式 ID，Word 会静默回退这些样式",
                part=ctx.document_part,
                evidence=", ".join(missing[:10]),
                suggestion="重新入库模板以补齐样式定义，或修正档案里引用的样式 ID",
            )
        )

    residue = placeholder_residue(ctx)
    if residue:
        ctx.note(
            PLACEHOLDER_RULE_ID,
            "major",
            f"正文里仍有 {len(residue)} 处模板占位文本残留在最终产物中",
            part=ctx.document_part,
            evidence=", ".join(residue[:5]),
            suggestion="确认这些占位所在版块已被登记为受保护区并完成清理",
        )
    return blockers


def _rehash(artifact: Path) -> str | None:
    """刷新成功后从磁盘重算产物摘要。

    Word 改的是磁盘上的字节，内存里的包已经不是产物了。不重算就等于给产物贴一个
    属于"刷新前版本"的指纹——版本 diff 与 golden 比对都会据此得出错误结论。
    重读失败时返回 ``None``：算不出摘要，比给出一个错的摘要诚实。
    """
    try:
        with DocxPackage.open(artifact, read_only=True) as reopened:
            return normalized_digest(reopened)
    except EngineError:
        return None
