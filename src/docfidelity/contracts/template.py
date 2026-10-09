"""模板包契约（DD-3.2（1））。

模板包是**解析后的**模板承诺。生成期只查本契约，绝不重新打开模板原件
（决策二：模板解析前置）。因此本文件的字段直接决定"生成期能问到什么"：
契约里没有的东西，渲染期拿不到，也不允许回头去解析原件补。

各模型与模板包 ``contract/`` 目录下的文件一一对应。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, ClassVar, Literal

from pydantic import Field

from .base import FormatModel

StyleType = Literal["paragraph", "character", "table", "numbering", "linked"]
"""样式类型，对应 ``w:style/@w:type``。"""

HeaderFooterRole = Literal["default", "first", "even"]
"""页眉页脚引用角色，对应 ``w:headerReference/@w:type``。"""

TblStylePrType = Literal[
    "firstRow",
    "lastRow",
    "firstCol",
    "lastCol",
    "band1Vert",
    "band1Horz",
    "band2Vert",
    "band2Horz",
    "neCell",
    "nwCell",
    "seCell",
    "swCell",
]
"""表格条件格式类型，对应 ``w:tblStylePr/@w:type``。"""

BlockKind = Literal["cover", "revision", "toc", "header", "table"]
"""可复用块的类别（DD-3.2（1）BlockIndex）。"""


class BlockLocator(FormatModel):
    """块 / 占位符在 OOXML 包内的定位。

    ``xpath`` 必须在同一部件的同一快照下稳定；涉及易变标识（``_Toc`` 书签 ID、
    部件关系 ID）时，定位必须落在归一化之后的形式上（DD-4.7）。
    """

    part: str = Field(description="部件路径，如 word/document.xml")
    xpath: str = Field(description="精确到节点的 XPath")
    index: int | None = Field(default=None, description="同 XPath 命中多项时的序号（0 起）")


class TableStylePr(FormatModel):
    """表格条件格式（``w:tblStylePr``）。

    **不得省略**：``firstRow`` / ``lastRow`` / ``band1Horz`` 等条件格式直接决定
    表头与隔行底纹是否保真。跨文档克隆表格样式时若丢掉本字段，产物会在
    Word 中"看起来样式没变、实际表头掉色"（1.2 第 8 条教训）。
    """

    type: TblStylePrType
    bold: bool | None = None
    italic: bool | None = None
    shading_fill: str | None = Field(default=None, description="底纹填充，如 'D9E2F3'")
    font_color: str | None = Field(default=None, description="字体颜色 RGB，如 '1F3864'")
    raw_xml: str | None = Field(
        default=None,
        description="未建模属性的原始 XML 片段。**保留不丢**，回写时原样还原。",
    )


class StyleContract(FormatModel):
    """单个样式的承诺。对应 ``w:styles`` 中一个 ``w:style``。"""

    format_family: ClassVar[str] = "template"

    name: str = Field(description="样式显示名，对应 w:name")
    style_id: str = Field(
        description="样式 ID，对应 w:styleId；文档中被 w:pStyle / w:tblStyle 引用"
    )
    type: StyleType
    based_on: str | None = Field(default=None, description="w:basedOn")
    next: str | None = Field(default=None, description="w:next（仅段落样式）")
    link: str | None = Field(default=None, description="w:link（段落样式与字符样式的互链）")
    font_latin: str | None = None
    font_east_asian: str | None = Field(default=None, description="东亚字体，缺省请勿用 latin 顶替")
    size_pt: float | None = None
    bold: bool | None = None
    italic: bool | None = None
    line_spacing: float | None = Field(default=None, description="倍数行距")
    space_before_pt: float | None = None
    space_after_pt: float | None = None
    indent_left_twips: int | None = None
    indent_first_line_twips: int | None = Field(
        default=None, description="首行缩进；负值表示悬挂缩进"
    )
    num_id: int | None = Field(default=None, description="该样式绑定的编号实例")
    tbl_style_pr: list[TableStylePr] = Field(default_factory=list)
    raw_xml: str | None = Field(default=None, description="未建模属性的原始 XML 片段")


class PageSize(FormatModel):
    """纸张尺寸（twips）。"""

    width_twips: int
    height_twips: int
    orientation: Literal["portrait", "landscape"] = "portrait"

    @property
    def text_orientation_width(self) -> int:
        """版心推导使用的宽度（横向时与高度互换）。"""
        return self.height_twips if self.orientation == "landscape" else self.width_twips


class Margins(FormatModel):
    """页边距（twips）。"""

    top_twips: int
    bottom_twips: int
    left_twips: int
    right_twips: int
    header_twips: int | None = None
    footer_twips: int | None = None
    gutter_twips: int | None = None


class SectionSpec(FormatModel):
    """分节差异。仅记录该节相对前一节**不同**的部分，未列出的项继承前一节。"""

    index: int = Field(description="节序号，0 起")
    page_size: PageSize
    margins: Margins
    start_type: Literal["continuous", "nextPage", "evenPage", "oddPage"] | None = None
    header_ref: str | None = Field(default=None, description="页眉部件关系 ID（归一化后）")
    footer_ref: str | None = Field(default=None, description="页脚部件关系 ID（归一化后）")
    header_footer_roles: list[HeaderFooterRole] = Field(default_factory=list)
    diff_from_previous: list[str] = Field(
        default_factory=list, description="相对前一节发生变化的字段名，便于界面呈现"
    )


class PageSetupContract(FormatModel):
    """版心契约。渲染期推导可用宽高只需要本模型 + 图片 ``wp:extent``。"""

    format_family: ClassVar[str] = "template"

    page_size: PageSize
    margins: Margins
    sections: list[SectionSpec] = Field(default_factory=list)

    @property
    def content_width_twips(self) -> int:
        """正文可用宽度（twips）。图片与表格越界判定的分母。"""
        return (
            self.page_size.text_orientation_width
            - self.margins.left_twips
            - self.margins.right_twips
        )


class NumberLevel(FormatModel):
    """编号层级，对应 ``w:lvl``。"""

    ilvl: int
    num_fmt: str = Field(description="编号格式，如 decimal / chineseCounting / lowerLetter")
    lvl_text: str = Field(description="编号模板文本，如 '%1.%2'")
    start: int = 1
    indent_left_twips: int | None = None
    indent_hanging_twips: int | None = None
    font_east_asian: str | None = None


class AbstractNum(FormatModel):
    """抽象编号定义，对应 ``w:abstractNum``。"""

    abstract_num_id: int
    levels: list[NumberLevel] = Field(default_factory=list)


class NumInstance(FormatModel):
    """编号实例，对应 ``w:num``；把 ``numId`` 映射到 ``abstractNumId``。"""

    num_id: int
    abstract_num_id: int


class HeadingBinding(FormatModel):
    """标题样式到编号的绑定：``styleId`` -> ``numId`` + ``ilvl``。"""

    style_id: str
    num_id: int
    ilvl: int


class NumberingContract(FormatModel):
    """编号体系契约。章节注入后必须按本契约重算编号（DD-9.1 步骤顺序）。"""

    format_family: ClassVar[str] = "template"

    abstract_num: list[AbstractNum] = Field(default_factory=list)
    num: list[NumInstance] = Field(default_factory=list)
    heading_bindings: list[HeadingBinding] = Field(default_factory=list)

    def num_id_for_style(self, style_id: str) -> HeadingBinding | None:
        """查询某标题样式绑定的编号实例；未绑定返回 ``None``。"""
        for binding in self.heading_bindings:
            if binding.style_id == style_id:
                return binding
        return None


class UsedStyle(FormatModel):
    """被使用过的样式及其出现次数。"""

    style_id: str
    name: str | None = None
    count: int = Field(default=0, ge=0)


class UsedStylesContract(FormatModel):
    """活跃 / 死样式的区分依据（FR-M2-13）。

    用于回答两个问题：① 裁剪模板冗余时哪些样式**不能删**；
    ② 生成期新注入内容可以安全引用哪些样式。
    """

    format_family: ClassVar[str] = "template"

    p_style: list[UsedStyle] = Field(default_factory=list)
    tbl_style: list[UsedStyle] = Field(default_factory=list)

    def is_used(self, style_id: str) -> bool:
        return any(item.style_id == style_id for item in (*self.p_style, *self.tbl_style))


class SlotSpec(FormatModel):
    """章节骨架上的一个可填充槽位。"""

    slot_id: str
    required: bool = True
    unit_types: list[str] = Field(
        default_factory=list,
        description="允许填入的内容单元类型；取值见 contracts.ir.UnitType，此处保持开放以避免环依赖",
    )
    locator: str = Field(description="槽位在骨架中的语义定位，如 'chapter[2].section[1]'")
    description: str | None = None


class ChapterNode(FormatModel):
    """章节骨架节点。层级由 ``level`` 表达，允许非连续层级的模板（如只有 1/3 级）。"""

    level: int = Field(ge=1)
    title: str
    style_id: str | None = Field(default=None, description="该级标题使用的段落样式 ID")
    slots: list[SlotSpec] = Field(default_factory=list)
    children: list[ChapterNode] = Field(default_factory=list)


class OutlineSkeleton(FormatModel):
    """章节骨架契约。装配层据此决定"哪些章节必须有、可以挂什么内容"。"""

    format_family: ClassVar[str] = "template"

    chapter_tree: list[ChapterNode] = Field(default_factory=list)
    allowed_block_types: list[BlockKind] = Field(default_factory=list)
    slots: list[SlotSpec] = Field(default_factory=list, description="扁平索引，便于按 slot_id 直查")


class BlockEntry(FormatModel):
    """模板中一个可复用块的索引条目。"""

    block_id: str
    kind: BlockKind
    locator: BlockLocator
    structure: dict[str, Any] = Field(
        default_factory=dict, description="块的结构摘要（行数 / 列数 / 占位符数量等）"
    )
    cross_template_ref: bool = Field(
        default=False, description="该块是否引用了跨模板资源（如外部图片、链接）"
    )


class BlockIndex(FormatModel):
    """块索引。修订记录表、目录、固定表单等按原样复用，不重新构造。"""

    format_family: ClassVar[str] = "template"

    blocks: list[BlockEntry] = Field(default_factory=list)

    def by_kind(self, kind: BlockKind) -> list[BlockEntry]:
        return [block for block in self.blocks if block.kind == kind]


class PlaceholderEntry(FormatModel):
    """模板中的占位符 / 示例值。入库时识别，生成期必须被替换或显式清除。"""

    text: str
    locator: BlockLocator
    proposed_rule_id: str | None = Field(
        default=None, description="建议由哪条门禁规则校验其是否被残留"
    )


class PlaceholderList(FormatModel):
    """占位符清单。"""

    format_family: ClassVar[str] = "template"

    entries: list[PlaceholderEntry] = Field(default_factory=list)


class TemplateFingerprint(FormatModel):
    """模板指纹：各分区独立摘要，供分区级 diff（DD-7.4）。

    计算前必须归一化易变标识（``_Toc`` 书签 ID、部件关系 ID），
    否则同一模板每次解析都会得到不同指纹（DD-4.7）。
    """

    format_family: ClassVar[str] = "template"

    digest: str = Field(description="整体摘要：由其自身分区摘要合成，与分区顺序无关")
    sections: dict[str, str] = Field(
        default_factory=dict,
        description="分区摘要：分区名 → 摘要，供分区级 diff；分区集合由 registry 定义",
    )


class TemplateManifest(FormatModel):
    """模板包清单。模板包 ``contract/manifest.json`` 的内容。"""

    format_family: ClassVar[str] = "template"

    template_id: str = Field(description="模板编号，来自模板封面 / 页眉的文件编号")
    version: str = Field(description="模板版本")
    doc_type: str | None = Field(default=None, description="模板面向的文档类型（来自档案，可为空）")
    source_hash: str = Field(description="模板原件摘要")
    package_hash: str = Field(
        description="包内容摘要，覆盖 contract/ + blocks/ + placeholders.json，**不含** fingerprint.json（避免自指）"
    )
    created_at: datetime
    source_filename: str | None = None
    notes: str | None = None

    @property
    def ref(self) -> str:
        """``template_id@version`` 形式的引用键。"""
        return f"{self.template_id}@{self.version}"


class TemplatePackage(FormatModel):
    """模板包聚合视图。

    与模板包目录的对应关系::

        <package>/
        ├── contract/manifest.json      -> manifest
        ├── contract/styles.json        -> styles
        ├── contract/page_setup.json    -> page_setup
        ├── contract/numbering.json     -> numbering
        ├── contract/used_styles.json   -> used_styles
        ├── contract/outline.json       -> outline
        ├── contract/blocks.json        -> blocks
        ├── placeholders.json           -> placeholders
        ├── fingerprint.json            -> fingerprint
        ├── overrides.json              -> TemplateOverrides（人工确认留痕，不在本视图内）
        └── blocks/                     -> 块原件（图片、嵌入对象等）
    """

    format_family: ClassVar[str] = "template"

    manifest: TemplateManifest
    styles: list[StyleContract] = Field(default_factory=list)
    page_setup: PageSetupContract
    numbering: NumberingContract = Field(default_factory=NumberingContract)
    used_styles: UsedStylesContract = Field(default_factory=UsedStylesContract)
    outline: OutlineSkeleton = Field(default_factory=OutlineSkeleton)
    blocks: BlockIndex = Field(default_factory=BlockIndex)
    placeholders: PlaceholderList = Field(default_factory=PlaceholderList)
    fingerprint: TemplateFingerprint | None = None

    def style_by_id(self, style_id: str) -> StyleContract | None:
        for style in self.styles:
            if style.style_id == style_id:
                return style
        return None


ReviewAction = Literal["accept", "specify"]
"""待确认项的两条出路：**采纳解析结果**（``accept``）或**由人指定取值**（``specify``）。

只有两种，且都不允许"跳过"——P05 第 4 步的判据是"没有留痕"，没有留痕就是没办结
（与门禁裁决同一口径）。"回头再说"的表达方式是**不提交**，而不是提交一个空动作。
"""


class ReviewOverride(FormatModel):
    """一条待确认项的处理留痕（P05 第 4 步）。

    与 :class:`~docfidelity.contracts.qa.Adjudication` 是**两个概念**，刻意不复用：
    那个是**门禁判定**的人工放行（``accept`` / ``configure`` / ``waive``，其中 ``waive``
    会改变交付结论）；本条是**入库解析**的低置信项确认，它不改变任何交付结论——
    入库报告没有"结论"可言，只有"这件模板能不能登记"。

    两者混成一种的代价很具体：模板解析的一个低置信项会出现在 ``/inbox`` 的待裁决
    列表里，而那个列表的语义是"这些文档**还不能交付**"。
    """

    format_family: ClassVar[str] = "template"

    item_id: str = Field(description="待确认项标识，形如 <import_id>@<序号>")
    rule_id: str = Field(description="产出该项的规则名（如 REG-OUTLINE）")
    action: ReviewAction
    value: str | None = Field(default=None, description="``specify`` 时由人给出的取值")
    operator: str = Field(description="责任人；必填（NFR-15）")
    note: str | None = Field(default=None, description="处理意见")
    decided_at: datetime
    evidence: str | None = Field(default=None, description="判定依据快照，随件留存")


class TemplateOverrides(FormatModel):
    """模板包内的 ``overrides.json``（DD-3.2 / 需求 P05 第 4 步「结果写入 overrides 配置」）。

    放在包里而不是库里：生成期按 ``DocumentEntry.overrides_ref`` 取它，
    而那个引用指向的是**模板包内**的文件。库侧另存一份必然漂移。

    **不参与** ``package_hash``（与 ``manifest.json`` 同处理）：包哈希覆盖的是
    "这份模板解析出了什么"，而本文件记录的是"人就解析结果做了哪些确认"。
    把它算进摘要的话，"同一份模板 + 同一个决定"会因为确认时间不同而算出两个哈希，
    版本比对会把一次人工确认显示成一次模板变更。
    """

    format_family: ClassVar[str] = "template"

    template_ref: str = Field(description="``template_id@version``")
    entries: list[ReviewOverride] = Field(default_factory=list)

    def decision_of(self, item_id: str) -> ReviewOverride | None:
        """某条待确认项**最近一次**的处理（后者胜出）。"""
        found: ReviewOverride | None = None
        for entry in self.entries:
            if entry.item_id == item_id:
                found = entry
        return found

    def decided_ids(self) -> set[str]:
        return {entry.item_id for entry in self.entries}


# ChapterNode 自引用需要显式重建（pydantic v2 在字符串注解下的常规做法）
ChapterNode.model_rebuild()


__all__ = [
    "AbstractNum",
    "BlockEntry",
    "BlockIndex",
    "BlockKind",
    "BlockLocator",
    "ChapterNode",
    "HeaderFooterRole",
    "HeadingBinding",
    "Margins",
    "NumInstance",
    "NumberLevel",
    "NumberingContract",
    "OutlineSkeleton",
    "PageSetupContract",
    "PageSize",
    "PlaceholderEntry",
    "PlaceholderList",
    "ReviewAction",
    "ReviewOverride",
    "SectionSpec",
    "SlotSpec",
    "StyleContract",
    "StyleType",
    "TableStylePr",
    "TblStylePrType",
    "TemplateFingerprint",
    "TemplateManifest",
    "TemplateOverrides",
    "TemplatePackage",
    "UsedStyle",
    "UsedStylesContract",
]
