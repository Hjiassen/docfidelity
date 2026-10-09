"""渲染层（``render``）—— 内容 IR + 模板包 → ``.docx``。

本层的全部职责是 DD-9.1 的 13 步编排。它对外的唯一入口是 :func:`render`；
其余符号导出是为了让每一步**可单独调用**（扩展点原则 1 / 验收 25）与可单独测试。

两条不可越界的纪律
------------------
* ``ooxml`` 是唯一写 OOXML 的地方（4.5 硬原则 3）。本层不直接操作 ``lxml``
  以外的任何 XML 细节，也不自行拼装决定版式的元素。
* 本层**只依赖契约**（``contracts.template`` / ``contracts.profile`` /
  ``contracts.ir`` / ``contracts.config``）与 ``ooxml``；不 import ``registry``
  （生成期若还能回查模板库，"只查契约"这条纪律就形同虚设）与 ``compose``
  （IR 是两者之间唯一的接口）。由 ``.importlinter`` 强制。
"""

from .blocks import (
    REGION_KINDS,
    PlaceholderReport,
    block_reference_of,
    block_text_of,
    clear_protected_placeholders,
    copy_block,
    locate_regions,
    placeholder_residue,
    placeholder_residue_in_package,
)
from .context import (
    CELL_LAYOUT_RULE_ID,
    DEFERRED_STEPS,
    FIGURE_OVERFLOW_RULE_ID,
    FIGURE_RESIZE_RULE_ID,
    PAGINATION_RULE_ID,
    PLACEHOLDER_RULE_ID,
    REMOVAL_RULE_ID,
    ROW_TEMPLATE_RULE_ID,
    STEP_ORDER,
    STYLE_FALLBACK_RULE_ID,
    TABLE_HEADER_RULE_ID,
    TABLE_WIDTH_RULE_ID,
    TEMPLATE_FIDELITY_RULE_ID,
    PrototypeIndex,
    RegionEntry,
    RenderContext,
    StepResult,
)
from .figures import (
    CONTENT_COMPLETE_RULE_ID,
    FigurePlacement,
    FigurePolicy,
    assert_pictures_inside,
    figure_data_of,
    fit_document_pictures,
    picture_overflow,
    place_figure,
    place_pending_figures,
)
from .inject import InjectionPolicy, InjectionReport, inject_chapters, inject_unit
from .pagination import (
    PaginationReport,
    apply_pagination,
    break_sites,
    count_page_breaks,
    validate_pagination,
    whitelist_targets,
)
from .pipeline import RenderPolicy, RenderResult, open_working_copy, render
from .properties import (
    PropertiesPolicy,
    PropertiesReport,
    apply_properties,
    output_filename,
    sanitize_filename,
)
from .removal import RemovalReport, RemovalRule, apply_removals
from .tables import (
    TablePolicy,
    TableReport,
    build_table,
    normalize_injected_tables,
    normalize_table,
    table_data_of,
)
from .toc import (
    REFRESH_RULE_ID,
    TOC_RULE_ID,
    RefreshReport,
    TocPolicy,
    TocReport,
    ensure_toc_bookmarks,
    heading_paragraphs,
    heading_units,
    office_available,
    outline_from_ir,
    rebuild_toc_entries,
    refresh_stage,
    toc_blocks,
)

#: 渲染策略扩展点名称（DD-9.8）。Beta 之前只登记"默认策略"这一个取值。
DEFAULT_STRATEGY: str = "default"

__all__ = [
    "DEFAULT_STRATEGY",
    # 出口
    "RenderPolicy",
    "RenderResult",
    "open_working_copy",
    "render",
    # 上下文与步骤编排
    "DEFERRED_STEPS",
    "STEP_ORDER",
    "PrototypeIndex",
    "RegionEntry",
    "RenderContext",
    "StepResult",
    # 版块
    "REGION_KINDS",
    "PlaceholderReport",
    "block_reference_of",
    "block_text_of",
    "clear_protected_placeholders",
    "copy_block",
    "locate_regions",
    "placeholder_residue",
    "placeholder_residue_in_package",
    # 裁剪
    "RemovalReport",
    "RemovalRule",
    "apply_removals",
    # 注入
    "InjectionPolicy",
    "InjectionReport",
    "inject_chapters",
    "inject_unit",
    # 表格
    "TablePolicy",
    "TableReport",
    "build_table",
    "normalize_injected_tables",
    "normalize_table",
    "table_data_of",
    # 图片
    "CONTENT_COMPLETE_RULE_ID",
    "FigurePlacement",
    "FigurePolicy",
    "assert_pictures_inside",
    "figure_data_of",
    "fit_document_pictures",
    "picture_overflow",
    "place_figure",
    "place_pending_figures",
    # 分页
    "PaginationReport",
    "apply_pagination",
    "break_sites",
    "count_page_breaks",
    "validate_pagination",
    "whitelist_targets",
    # 目录与域刷新
    "RefreshReport",
    "TocPolicy",
    "TocReport",
    "ensure_toc_bookmarks",
    "heading_paragraphs",
    "heading_units",
    "office_available",
    "outline_from_ir",
    "rebuild_toc_entries",
    "refresh_stage",
    "toc_blocks",
    # 属性
    "PropertiesPolicy",
    "PropertiesReport",
    "apply_properties",
    "output_filename",
    "sanitize_filename",
    # 规则编号
    "CELL_LAYOUT_RULE_ID",
    "FIGURE_OVERFLOW_RULE_ID",
    "FIGURE_RESIZE_RULE_ID",
    "PAGINATION_RULE_ID",
    "PLACEHOLDER_RULE_ID",
    "REFRESH_RULE_ID",
    "REMOVAL_RULE_ID",
    "ROW_TEMPLATE_RULE_ID",
    "STYLE_FALLBACK_RULE_ID",
    "TABLE_HEADER_RULE_ID",
    "TABLE_WIDTH_RULE_ID",
    "TEMPLATE_FIDELITY_RULE_ID",
    "TOC_RULE_ID",
]
