"""渲染上下文：渲染期唯一的状态载体与原型索引（DD-9.1 / DD-9.2）。

``render`` 是整条链路上**唯一真正改写 OOXML 字节**的地方。它吃两样东西：内容 IR
（``compose`` 的出口）与**模板包契约**（``registry`` 的出口），再加一份模板原件
作为深拷贝的基底。模板知识一律来自 ``contracts.template``，渲染期绝不允许
"回头再解析一遍模板"（决策二）。

与 DD-9.1 / 9.2 的一处签名偏离
------------------------------
DD 的伪代码里步骤函数接收 ``contract: ContractQuery``。``ContractQuery`` 住在
``registry``，而 ``.importlinter`` 把 ``render`` 与 ``registry`` 排在同一独立层
（互相禁引）——这条分层是对的：渲染期若能回查模板库，"生成期只查契约"这条纪律
就形同虚设。故这里改用 :class:`~docfidelity.contracts.template.TemplatePackage`
（契约，不是仓库）作为模板知识入口。

原型表**每次取用都必须深拷贝**
------------------------------
原型是模板里的**活元素**，``Element`` 一旦被 ``insert`` 就从原位置移走了。
若 :class:`PrototypeIndex` 直接返回缓存的那个元素，第二次注入会把第一次注入的
内容搬走——产物"少了一章"，而没有任何一层门禁会喊。故取用一律经 ``deep_copy``。
``PrototypeIndex`` 内只存游离副本，外部只能通过访问器拿到新的副本。

内容区（content region）
------------------------
模板 = 前置版块（封面 / 修订记录 / 目录 / 页眉）+ **示例内容区** + 尾节属性。
示例内容区是模板作者写的"样子"，它必须被裁掉并换成注入内容——不裁就会产出
"模板示例 + 注入内容"双份。判据只有一条：**第一个用骨架标题样式排版的段落**
到 **body 直属 ``w:sectPr`` 之前**。故本模块把它算出来供裁剪与注入共用，
两处各算一遍必然漂移。
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from docfidelity.contracts.ir import ContentIR, ContentUnit
from docfidelity.contracts.profile import DocTypeProfile
from docfidelity.contracts.qa import Finding, Location, RuleSeverity
from docfidelity.contracts.template import (
    BlockEntry,
    NumberingContract,
    StyleContract,
    TemplatePackage,
)
from docfidelity.ooxml import (
    DOCUMENT_PART,
    DocxPackage,
    Element,
    Part,
    attr,
    clone_style_if_missing,
    deep_copy,
    find_all,
    find_first,
    level_style_map,
    localname,
    used_styles_in_package,
    xpath,
)

from .order import (
    BULLET_FORMAT,
    is_paragraph,
    is_table,
)

__all__ = [
    "CELL_LAYOUT_RULE_ID",
    "DEFERRED_STEPS",
    "FIGURE_OVERFLOW_RULE_ID",
    "FIGURE_RESIZE_RULE_ID",
    "PAGINATION_RULE_ID",
    "PLACEHOLDER_RULE_ID",
    "PlaceholderSite",
    "PrototypeIndex",
    "REMOVAL_RULE_ID",
    "ROW_TEMPLATE_RULE_ID",
    "RegionEntry",
    "RenderContext",
    "STEP_ORDER",
    "STYLE_FALLBACK_RULE_ID",
    "TABLE_HEADER_RULE_ID",
    "TABLE_WIDTH_RULE_ID",
    "TEMPLATE_FIDELITY_RULE_ID",
    "body_children",
    "body_of",
    "content_region",
    "harvest_prototypes",
    "heading_style_id",
    "paragraph_style_id",
    "trailing_section",
]

# -- 规则编号（DD-10 / 附录 C；渲染期只报自己这几条） ---------------------
REMOVAL_RULE_ID: Final = "FR-M2-10"
PLACEHOLDER_RULE_ID: Final = "FR-M2-11"
STYLE_FALLBACK_RULE_ID: Final = "FR-M2-13"
FIGURE_RESIZE_RULE_ID: Final = "FR-M4-07"
CELL_LAYOUT_RULE_ID: Final = "FR-M4-08"
TEMPLATE_FIDELITY_RULE_ID: Final = "FR-M5-02"
TABLE_HEADER_RULE_ID: Final = "FR-M5-03"
PAGINATION_RULE_ID: Final = "FR-M5-04"
TABLE_WIDTH_RULE_ID: Final = "FR-M5-06"
FIGURE_OVERFLOW_RULE_ID: Final = "FR-M5-07"
ROW_TEMPLATE_RULE_ID: Final = "FR-M6-05"

#: 渲染步骤顺序（DD-9.1 的 1~12，顺序即语义）。
#:
#: **``refresh`` 排在末位是一个有意的偏离**：DD-9.1 把它列为第 11 步、保存列为第 13 步，
#: 但域刷新走 Word COM，只能对**磁盘上的文件**操作，内存里的 lxml 树它看不见。故刷新的
#: *执行*必须在保存之后；DD-11.3 本身也把 ``refresh`` 建模成紧跟 render 的独立阶段
#: （``render → out.docx``、``refresh → out.refreshed.docx``）。理由详见 ``render.toc``。
STEP_ORDER: Final[tuple[str, ...]] = (
    "blocks",
    "removal",
    "styles",
    "numbering",
    "inject",
    "tables",
    "figures",
    "pagination",
    "toc",
    "properties",
    "refresh",
)

#: DD-9.1 的 13 步已全部落地。保留这个常量是为了让"还缺哪几步"依旧**代码可见**
#: （``RenderResult.deferred_steps`` 会把它带出来），而不是靠人去比对 DD 的编号。
DEFERRED_STEPS: Final[tuple[str, ...]] = ()


# 元素层的书写约定（局部名 / 子元素规范序 / 开关取值）住在 ``order``，此处
# 只是转出以便调用方就近取用：``from .context import TBL_PR_ORDER`` 与
# ``from .order import TBL_PR_ORDER`` 等价，少一次"该从哪 import"的判断。
# --------------------------------------------------------------------- 工具
def body_of(pkg: DocxPackage, part: str = DOCUMENT_PART) -> Element:
    """取正文部件的 ``w:body``；缺失即抛 ``E_PACKAGE_INCOMPLETE``。"""
    from docfidelity.contracts.errors import EngineError, ErrorCode

    body = find_first(pkg.part(part).tree, "w:body")
    if body is None:
        raise EngineError(
            ErrorCode.PACKAGE_INCOMPLETE,
            f"正文部件缺少 w:body：{part}",
            where={"part": part},
            remediation=["重新导出模板原件", "确认上传的是 .docx 而非 .doc"],
        )
    return body


def body_children(body: Element) -> list[Element]:
    """``w:body`` 的全部直接子元素（按文档序）。"""
    return list(body)


def trailing_section(body: Element) -> Element | None:
    """``w:body`` 直属的 ``w:sectPr``（最后一节的节属性）。

    ``w:sectPr`` 在 body 直属时表示**最后一节**；出现在 ``w:pPr`` 里时表示
    "前一个节在这里结束"。两者语义相反，故这里只看直属的那一个。
    """
    last = body[-1] if len(body) else None
    if last is not None and localname(last) == "sectPr":
        return last
    for child in body:
        if localname(child) == "sectPr":
            return child
    return None


def paragraph_style_id(paragraph: Element) -> str | None:
    """段落引用的样式 ID（``w:pPr/w:pStyle/@w:val``）；无样式返回 ``None``。"""
    style = find_first(find_first(paragraph, "w:pPr"), "w:pStyle")
    return attr(style, "w:val")


def heading_style_id(chain: Sequence[Element]) -> str | None:
    """按"由近及远"的顺序取第一个命中的段落样式（插槽用）。

    单独抽出来是因为"原型段落属于哪一级"这件事在注入与裁剪里各判一次，
    两处判法不同会让"被当标题裁掉的段落"与"被当正文注入的段落"错位。
    """
    for element in chain:
        if element is not None and is_paragraph(element):
            return paragraph_style_id(element)
    return None


# ------------------------------------------------------------------ 内容区
def content_region(body: Element, level_styles: Mapping[int, str]) -> tuple[int, int]:
    """内容区在 ``body_children`` 里的半开下标区间 ``[start, end)``。

    ``start`` = 第一个用骨架标题样式排版的段落；``end`` = 直属 ``w:sectPr`` 的下标
    （没有则取子元素个数）。骨架标题一个都没有时返回一个**空区间**，位置落在
    尾节属性之前——这是"模板里没有示例内容"的正常形态，不是错误。
    """
    children = body_children(body)
    styles = set(level_styles.values())
    start = len(children)
    for index, child in enumerate(children):
        if not is_paragraph(child):
            continue
        if paragraph_style_id(child) in styles:
            start = index
            break
    end = len(children)
    for index in range(start, len(children)):
        if localname(children[index]) == "sectPr":
            end = index
            break
    return start, end


# ------------------------------------------------------------------ 原型表
@dataclass(frozen=True)
class PrototypeIndex:
    """模板原型表。字段值是**游离副本**，取用一律再深拷贝一份（见模块文档）。"""

    headings: Mapping[int, Element] = field(default_factory=dict)
    body: Element | None = None
    bullets: Element | None = None
    numbered: Element | None = None
    table: Element | None = None

    def heading(self, level: int) -> Element | None:
        """该层级的标题原型（新副本）。"""
        return _clone(self.headings.get(level))

    def heading_levels(self) -> list[int]:
        return sorted(self.headings)

    def nearest_heading_level(self, level: int) -> int | None:
        """最接近 ``level`` 的**已有**层级（优先向下取更浅的层级）。

        回退方向朝**浅**：把二级标题渲染成一级样式只是层次略粗；渲染成三级样式
        会让正文看起来"塌进去"，评审时更难发现。
        """
        levels = self.heading_levels()
        if not levels:
            return None
        shallower = [item for item in levels if item <= level]
        return shallower[-1] if shallower else levels[0]

    def heading_for(self, level: int) -> tuple[Element | None, bool]:
        """``(标题原型副本, 是否命中该层级)``；未命中时回落到最接近的层级。"""
        if level in self.headings:
            return _clone(self.headings[level]), True
        nearest = self.nearest_heading_level(level)
        if nearest is None:
            return None, False
        return _clone(self.headings[nearest]), False

    def body_paragraph(self) -> Element | None:
        """正文段落原型（新副本）。"""
        return _clone(self.body)

    def bullet_paragraph(self) -> Element | None:
        """项目符号段落原型（新副本）；未识别到时回落到正文原型。

        回落判据必须是 ``is not None``，不能写成 ``_clone(self.bullets) or _clone(self.body)``：
        lxml 元素的真值 = "有没有子元素"，一个**没有内容**的列表段（完全合法的形态）
        会被判成"没有原型"而悄悄回落到正文——符号没了，报告全绿。
        """
        return _clone(self.bullets) if self.bullets is not None else _clone(self.body)

    def numbered_paragraph(self) -> Element | None:
        """有序编号段落原型（新副本）；未识别到时回落到正文原型（回落判据同项目符号）。"""
        return _clone(self.numbered) if self.numbered is not None else _clone(self.body)

    def table_clone(self) -> Element | None:
        """表格原型（新副本）。"""
        return _clone(self.table)


def _clone(element: Element | None) -> Element | None:
    return None if element is None else deep_copy(element)


def harvest_prototypes(
    body: Element,
    level_styles: Mapping[int, str],
    numbering: NumberingContract | None = None,
    region: tuple[int, int] | None = None,
) -> PrototypeIndex:
    """从正文里采集原型：各级标题 / 正文 / 项目符号 / 有序编号 / 表格。

    **复用优先于新建**（1.4 原则）：原型只从模板自己的元素里取，绝不拼装
    ``w:pPr``。模板里没有对应原型时留空，由调用方回退并记 Finding——
    "自己造一个段落"会让样式保真当场失效。

    采集窗口先取**示例内容区**，取不到再回落到整篇
    --------------------------------------------
    原型的语义是"本模板的正文长这样"，而**示例内容区**（``content_region``）正是
    模板作者演示这件事的地方。整篇地取第一个候选则会把**前置版块**当正文：封面上的
    标题段（大字号居中）会成为正文原型，修订记录表会成为表格原型——于是注入的每一段
    正文都套上封面样式、每一张注入表都继承修订表的列数。产物看上去"有内容"，只有
    人眼能看出样式串了。

    回落不是保险起见而是必要的：``content_region`` 在"模板里没有骨架标题"时返回
    空区间（那是正常形态，见其文档），此时只能按整篇取；内容区缺某一类原型（比如
    内容区没有列表段、但前置版块有）也照样按整篇补。故这里是"内容区优先、整篇兜底"，
    不是"只认内容区"。
    """
    children = body_children(body)
    window = children if region is None else children[region[0] : region[1]]
    primary = _harvest_from(window, level_styles, numbering)
    if len(window) == len(children):
        return primary
    fallback = _harvest_from(children, level_styles, numbering)
    return PrototypeIndex(
        headings={**fallback.headings, **primary.headings},
        body=primary.body if primary.body is not None else fallback.body,
        bullets=primary.bullets if primary.bullets is not None else fallback.bullets,
        numbered=primary.numbered if primary.numbered is not None else fallback.numbered,
        table=primary.table if primary.table is not None else fallback.table,
    )


def _harvest_from(
    children: Sequence[Element],
    level_styles: Mapping[int, str],
    numbering: NumberingContract | None,
) -> PrototypeIndex:
    """给定一串**同级**元素，按"第一个合适者"采集各类原型。"""
    heading_styles = set(level_styles.values())
    bullets_ids = _bullet_num_ids(numbering)
    headings: dict[int, Element] = {}
    body_paragraph: Element | None = None
    bullet_paragraph: Element | None = None
    numbered_paragraph: Element | None = None
    table: Element | None = None

    for child in children:
        if is_table(child):
            if table is None:
                table = child
            continue
        if not is_paragraph(child):
            continue
        style_id = paragraph_style_id(child)
        for level, style in level_styles.items():
            if style_id == style and level not in headings:
                headings[level] = child
        num_id = _num_id_of(child)
        if num_id is not None:
            if num_id in bullets_ids and bullet_paragraph is None:
                bullet_paragraph = child
            elif num_id not in bullets_ids and numbered_paragraph is None:
                numbered_paragraph = child
            continue
        if style_id in heading_styles:
            continue
        if body_paragraph is None:
            body_paragraph = child

    return PrototypeIndex(
        headings=headings,
        body=body_paragraph,
        bullets=bullet_paragraph,
        numbered=numbered_paragraph,
        table=table,
    )


def _num_id_of(paragraph: Element) -> str | None:
    """段落直接引用的编号实例 ID（``w:pPr/w:numPr/w:numId/@w:val``）。"""
    return attr(
        find_first(find_first(find_first(paragraph, "w:pPr"), "w:numPr"), "w:numId"), "w:val"
    )


def _bullet_num_ids(numbering: NumberingContract | None) -> set[str]:
    """``numId`` 集合：其抽象定义在 ``ilvl=0`` 上的格式是项目符号。

    用编号契约定"符号 vs 有序"这条判据，而不是猜样式名（``ListParagraph``、
    ``列表段落``、``List`` 三种写法都见过）。判据只有一处实现。
    """
    if numbering is None:
        return set()
    abstract_format: dict[int, str] = {}
    for definition in numbering.abstract_num:
        for level in definition.levels:
            if level.ilvl == 0:
                abstract_format[definition.abstract_num_id] = level.num_fmt
                break
    return {
        str(instance.num_id)
        for instance in numbering.num
        if abstract_format.get(instance.abstract_num_id) == BULLET_FORMAT
    }


# ------------------------------------------------------------------ 步骤结果
@dataclass
class StepResult:
    """单步产出（DD-9.1：``findings`` + ``checkpoint_outputs``）。

    ``checkpoint_outputs`` 是**可 JSON 化**的步骤摘要，供 TASK-P0-13 的
    checkpoint 落库——续跑时要能判断"这一步的产物还算不算数"。故这里只放
    计数与标识，不放元素（元素不可序列化，也不该跨进程传）。
    """

    step: str
    findings: list[Finding] = field(default_factory=list)
    checkpoint_outputs: dict[str, Any] = field(default_factory=dict)
    touched_parts: list[str] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        counts = {"blocker": 0, "major": 0, "minor": 0}
        for item in self.findings:
            counts[item.severity] = counts.get(item.severity, 0) + 1
        return counts


@dataclass(frozen=True)
class RegionEntry:
    """一个已登记的受保护区（模板前置版块）。

    ``elements`` 是一个**元组**而不是单个元素：大多数版块是一个节点（修订记录表、
    目录控件、页眉），但**封面是一个区间**——"首个分节符之前的连续段落"只能由一串
    段落表达。把它退化成"区间里的某一个段落"，占位符清除步骤就会判封面里的占位符
    "不在受保护区内"而跳过（而残留护栏会照报，于是每份产物都挂着一条 FR-M2-11）。
    """

    block_id: str
    kind: str
    part: str
    elements: tuple[Element, ...]

    @property
    def element(self) -> Element:
        """区域的**首个**元素（单节点版块就是它自己）。

        分页白名单只需要一个落点（"这一块要从新页开始"），取首个即可。
        """
        return self.elements[0]


@dataclass(frozen=True)
class PlaceholderSite:
    """一个模板占位符在**任何改动之前**的落点。

    为什么要"改动之前"就定死落点：定位符是位置型的（``(//w:p)[19]``），裁剪与注入
    一发生，同一个表达式指向的就是**另一个**段落。清除与残留检查若各自重新解析，
    两者看的根本不是同一批对象——"清除没生效"和"残留检查报了个别的段"会同时出现，
    而数字看起来都正常。故落点只算一次，两个步骤共用。
    """

    text: str
    part: str
    #: 落点所在部件的根（``w:document`` / ``w:hdr`` / ``w:ftr``）；部件不存在时为 ``None``。
    root: Element | None
    #: 落点元素；``None`` = 定位符在原件里没命中（重新入库才能修）。
    element: Element | None
    #: 是否落在受保护区内（清除步骤只处理受保护区里的占位符）。
    protected: bool

    @property
    def located(self) -> bool:
        return self.element is not None

    def still_in_document(self) -> bool:
        """元素是否仍**挂在部件树上**（而不是随被裁掉的区间成了游离子树）。

        不能只判 ``getparent() is not None``：占位段落若在被裁掉的**表格单元格**里，
        它的父节点（那个单元格）还跟着它走，而整棵子树已经从部件上摘下来了。
        故必须一路走到最顶层，看它是不是部件的根。漏掉这一条的后果是把"已经被裁掉"
        当成"残留"报出来——每份产物都挂着一条 FR-M2-11 的成因之一就在这里。
        """
        element, root = self.element, self.root
        if element is None or root is None:
            return False
        top = element
        for ancestor in element.iterancestors():
            top = ancestor
        return top is root


# ------------------------------------------------------------------- 上下文
class RenderContext:
    """渲染期的共享状态：包 / 契约 / 派生索引 / 判定汇聚。

    构造时会采集原型与内容区——它们被裁剪、注入、分页三步共用，
    各自算一遍必然漂移（"裁掉的区间"与"插入的位置"错位一格就会少一章）。
    """

    #: 资源加载器签名：``image_ref -> 原始字节``。
    AssetLoader = Callable[[str], bytes]

    def __init__(
        self,
        *,
        pkg: DocxPackage,
        template: TemplatePackage,
        profile: DocTypeProfile,
        ir: ContentIR,
        document_part: str = DOCUMENT_PART,
        asset_loader: AssetLoader | None = None,
        protected_blocks: Sequence[BlockEntry] = (),
    ) -> None:
        self.pkg = pkg
        self.template = template
        self.profile = profile
        self.ir = ir
        self.document_part = document_part
        self.asset_loader = asset_loader
        self.protected_blocks: list[BlockEntry] = list(protected_blocks)

        self.body = body_of(pkg, document_part)
        self.level_styles: dict[int, str] = level_style_map(template.outline)
        self.region: tuple[int, int] = content_region(self.body, self.level_styles)
        self.prototypes = harvest_prototypes(
            self.body, self.level_styles, template.numbering, region=self.region
        )
        self.findings: list[Finding] = []
        #: 已登记的受保护区：``block_id -> 区域条目``。
        self.regions: dict[str, RegionEntry] = {}
        #: 占位符落点（**改动之前**算一次；清除与残留检查共用同一批对象）。
        self.placeholder_sites: list[PlaceholderSite] = []
        #: 注入期登记的章节标题段落（分页白名单与"每章另起一页"的落点）。
        self.chapter_headings: list[Element] = []
        #: 注入期遇到的**显式声明**的强制分页点（DD-9.3"各文档声明的块"）。
        self.declared_breaks: list[Element] = []
        #: 注入期登记的全部新元素（按插入顺序）——裁剪与分页都要按它排除"我们自己的"。
        self.injected: list[Element] = []
        #: 注入期登记的表：``(unit_id, 表元素)``，供"表格规范化"步骤逐张处理。
        self.injected_tables: list[tuple[str, Element]] = []
        #: 注入期登记待放图片：``(单元, 承载段落)``，供"图片自适应"步骤逐张处理。
        #: 段落由注入步骤给（位置属于结构），字节与缩放属于图片步骤——两件事分开，
        #: 注入步骤就不必依赖资源加载器。
        self.pending_figures: list[tuple[ContentUnit, Element]] = []
        self._noted: set[tuple[str, str]] = set()

    @property
    def chapter_level(self) -> int:
        """ "章"的层级：模板骨架里最浅的那一级（有的模板只有 2/3 级）。"""
        return min(self.level_styles) if self.level_styles else 1

    @property
    def heading_style_ids(self) -> set[str]:
        return set(self.level_styles.values())

    def profile_allows(self, unit_type: str) -> bool:
        """体例档案是否声明允许该单元类型。

        渲染层**不按文档类型分支**（DD-9.2：结构来自 IR），档案在这里的唯一用途是
        给"渲染层不认识这个单元类型"这条判定**定级**：

        * 档案允许它 → 渲染层本该支持，这是引擎能力缺口，按 major 报；
        * 档案不允许它 → IR 出现它本身就是上游问题，按 minor 报。

        档案未声明任何类型（空清单）时按"引擎能力缺口"处理——把"没声明"当成
        "不允许"会让真正的渲染缺口被降级成一条无关痛痒的提示。
        """
        allowed = set(self.profile.genre.allowed_unit_types)
        return not allowed or unit_type in allowed

    def content_bounds(self) -> tuple[int, int]:
        """**当前**内容区间的半开下标 ``[start, end)``（``end`` = 尾节属性之前）。

        与 :attr:`region` 的差别必须记住：``region`` 是**模板原件**里的示例内容区，
        构造时算一次就固定，供裁剪与注入定位使用；注入之后内容区间会变长，
        任何"按当前正文再算一遍"的判断都必须用本方法。用错 ``region`` 的症状是
        "注入出来的内容整片落在判断区间之外"，表现为某些标志时有时无，像随机。
        """
        children = body_children(self.body)
        start = self.region[0]
        end = len(children)
        for index in range(start, len(children)):
            if localname(children[index]) == "sectPr":
                end = index
                break
        return start, end

    def resolve_caption_style(self, declared: str | None) -> str | None:
        """题注样式：显式声明优先；未声明时回落到正文段落样式并记一条 minor。

        **不猜**样式名。中文模板叫"题注"、英文模板叫"Caption"、还有很多模板根本
        没建过题注样式——按名字猜会在这三种情况下给出三种结果，而每一种看起来都
        "正常"。回落到正文样式至少是自洽的，而且有 Finding 说明发生过回退。
        """
        if declared is not None:
            return declared
        prototype = self.prototypes.body_paragraph()
        style_id = paragraph_style_id(prototype) if prototype is not None else None
        self.note_once(
            STYLE_FALLBACK_RULE_ID,
            "minor",
            "caption-style-fallback",
            "未声明题注样式，题注段落已回落到正文段落样式",
            evidence=f"style_id={style_id!r}",
            suggestion="在档案或渲染调用中显式声明题注样式 ID",
        )
        return style_id

    def region_entries(self, kinds: Sequence[str]) -> list[RegionEntry]:
        """按类别取受保护区（``kind`` 取值见 ``contracts.template.BlockKind``）。"""
        wanted = set(kinds)
        return [
            entry
            for entry in sorted(self.regions.values(), key=lambda item: item.block_id)
            if entry.kind in wanted
        ]

    def injected_paths(self) -> set[str]:
        """注入元素的 XPath 集合（判定"这个元素是不是我们放进去的"）。"""
        return {path for path in (_path_of(item) for item in self.injected) if path}

    # -------------------------------------------------------------- 便利访问
    @property
    def part(self) -> Part:
        """正文部件（可变树）。"""
        return self.pkg.part(self.document_part)

    def find_style(self, style_id: str) -> StyleContract | None:
        """按 ID 查模板样式契约。"""
        return self.template.style_by_id(style_id)

    def style_for_level(self, level: int) -> str | None:
        """该层级应使用的标题样式 ID。"""
        return self.level_styles.get(level)

    def nearest_style_for_level(self, level: int) -> str | None:
        """最接近 ``level`` 的可用标题样式（优先向下取更浅的层级）。

        回退方向朝**浅**：把二级标题渲染成一级样式只是层次略粗，渲染成三级样式
        会让正文看起来"塌进去"，评审时更难发现。回退必须记 Finding（DD-9.2 第 2 条）。
        """
        if not self.level_styles:
            return None
        candidates = sorted(self.level_styles)
        shallower = [item for item in candidates if item <= level]
        if shallower:
            return self.level_styles[shallower[-1]]
        return self.level_styles[candidates[0]]

    def ensure_style(self, style_id: str) -> tuple[str | None, bool]:
        """确保目标包的 ``styles.xml`` 里有该样式；缺失时按契约克隆（复用优先）。

        返回 ``(实际可用的 style_id, 是否由克隆产生)``。契约里也没有该样式时返回
        ``(None, False)``——调用方必须回退并记 Finding。**不抛错**：样式缺失是
        可控降级（回退到原型自带的样式），不是引擎缺陷，抛错会让一次可交付的构建失败。

        一律走 ``read_tree`` 判定：先前用 ``tree`` 探查"有没有 w:style"，
        那一次探查就把 ``styles.xml`` 永久置脏，于是"未触碰部件字节全等"这条承诺
        在最不需要它的地方失效——症状是"某个部件的字节莫名变了"。
        """
        if style_id in _style_element_ids(self.pkg):
            return style_id, False
        contract = self.find_style(style_id)
        if contract is None:
            return None, False
        return clone_style_if_missing(self.pkg, contract).style_id, True

    def active_style_ids(self) -> set[str]:
        """模板里真正被使用过的样式 ID（FR-M2-13）。"""
        used = used_styles_in_package(self.pkg)
        return {item.style_id for item in (*used.p_style, *used.tbl_style)}

    def usable_width_twips(self) -> int:
        """版心宽（twips）。表格列宽归一的唯一分母。"""
        from docfidelity.ooxml import usable_width

        return usable_width(self.template.page_setup)

    # -------------------------------------------------------------- 判定汇聚
    def note(
        self,
        rule_id: str,
        severity: RuleSeverity,
        message: str,
        *,
        unit: ContentUnit | None = None,
        part: str | None = None,
        element: Element | None = None,
        evidence: str | None = None,
        suggestion: str | None = None,
    ) -> Finding:
        """登记一条判定（``Location`` 自动带上 ``unit_id`` / 部件 / XPath）。"""
        finding = Finding(
            rule_id=rule_id,
            severity=severity,
            message=message,
            location=Location(
                unit_id=unit.unit_id if unit is not None else None,
                part=part,
                xpath=_path_of(element) if element is not None else None,
            ),
            evidence=evidence,
            suggestion=suggestion,
        )
        self.findings.append(finding)
        return finding

    def note_once(
        self,
        rule_id: str,
        severity: RuleSeverity,
        key: str,
        message: str,
        *,
        unit: ContentUnit | None = None,
        part: str | None = None,
        element: Element | None = None,
        evidence: str | None = None,
        suggestion: str | None = None,
    ) -> Finding | None:
        """同类判定只登记一次。

        "每一级标题的原型都缺"会一次刷出几十条一模一样的告警，把真正独有的一条
        埋掉。``key`` 由调用方给，语义是"这条算不算同一件事"。
        """
        marker = (rule_id, key)
        if marker in self._noted:
            return None
        self._noted.add(marker)
        return self.note(
            rule_id,
            severity,
            message,
            unit=unit,
            part=part,
            element=element,
            evidence=evidence,
            suggestion=suggestion,
        )

    def drain_findings(self, step: str) -> StepResult:
        """把当前累积的判定收进一个 :class:`StepResult` 并清空。"""
        findings = list(self.findings)
        self.findings.clear()
        return StepResult(step=step, findings=findings)


def _style_element_ids(pkg: DocxPackage) -> set[str]:
    """目标包 ``styles.xml`` 里已定义的样式 ID。"""
    root = pkg.part("word/styles.xml").read_tree
    return {
        value
        for value in (attr(element, "w:styleId") for element in find_all(root, "w:style"))
        if value
    }


def _path_of(element: Element) -> str | None:
    """元素的绝对 XPath（诊断用；lxml 代理对象不可做键，故只取路径字符串）。"""
    try:
        return element.getroottree().getpath(element)
    except AttributeError:  # 游离元素（无 root tree）不给定位，总比抛错好
        return None


def iter_region_elements(body: Element, region: tuple[int, int]) -> Iterator[Element]:
    """内容区内的元素（供裁剪统计与迁移使用）。"""
    start, end = region
    children = body_children(body)
    yield from children[start:end]


def xpath_elements(root: Element, expression: str) -> list[Element]:
    """带命名空间的 XPath（薄封装，集中在此处以免各处写法不一致）。"""
    return xpath(root, expression)


__all__.extend(["iter_region_elements", "xpath_elements"])
