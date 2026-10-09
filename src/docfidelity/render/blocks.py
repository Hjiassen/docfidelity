"""版块定位、占位符清理与块复用（DD-9.1 第 2 步 / FR-M2-11 / FR-M6-05）。

DD-9.1 第 2 步写的是"版块替换：cover / revision / toc / header（按 block_id，可跨模板）"。
但**替换成什么**在 P0 阶段没有数据来源：封面上的项目名、文件编号、编制单位来自
项目级元数据，而 ``ProjectConfig``（已冻结）只有一个 ``project_id``。硬造一份
"项目元数据"契约会凭空多出一层需要维护的东西，而本轮的目标是"docx 可产出"。

故这里把第 2 步实现成它能确实做到的三件事，一件也不假装：

1. **版块定位**：按 ``BlockLocator`` 把 cover / revision / toc / header 落到元素上，
   登记为**受保护区**。下游的裁剪与分页都要它——"不能把封面当冗余裁掉"、
   "修订记录页与目录页前要强制分页"都依赖这份登记，而且**只能有一份**。
2. **占位符清理**：``PlaceholderList`` 里落在受保护区内的占位文本必须被显式清除
   （模板包的契约就说"生成期必须被替换或显式清除"）。落在内容区的占位符不必单独
   处理——它们随内容区一起被裁掉。
3. **块复用**：``block_ref`` 类型的 IR 单元按 ``block_id`` 取模板块原件深拷贝复用
   （DD-9.1"可跨模板"靠的就是这条：定位直接落在 ``BlockLocator.part`` 上）。

"替换"这一半（用项目元数据填封面）留给引入项目元数据契约的那一轮；本模块把
**定位与清除**做扎实，是那一轮的前置件而不是替代品。

定位一律走 ``read_tree``
------------------------
``Part.tree`` 一访问就置脏，而 `save` 时置脏部件由元素树重新序列化。定位一个
封面块并不改动它，若顺手用 ``tree`` 就会让"未触碰部件字节全等"这条承诺失效——
而检查这条承诺的断言在另一个模块里，症状会表现为"某个部件的字节莫名变了"。
故本模块只对**真正改过的部件**调 ``mark_dirty()``。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Final

from docfidelity.contracts.ir import ContentUnit
from docfidelity.contracts.template import BlockEntry, BlockLocator, PlaceholderEntry
from docfidelity.ooxml import (
    DocxPackage,
    Element,
    deep_copy,
    find_first,
    iter_elements,
    localname,
    paragraph_text,
    qn,
    set_text,
    xpath,
)

from .context import (
    PLACEHOLDER_RULE_ID,
    TEMPLATE_FIDELITY_RULE_ID,
    PlaceholderSite,
    RegionEntry,
    RenderContext,
)

__all__ = [
    "REGION_KINDS",
    "PlaceholderReport",
    "block_reference_of",
    "block_text_of",
    "clear_protected_placeholders",
    "copy_block",
    "locate_placeholders",
    "locate_regions",
    "placeholder_residue",
    "placeholder_residue_in_package",
]

#: 需要登记为受保护区的版块类别（``contracts.template.BlockKind`` 的子集）。
#: ``table`` 不在其中：表类块是"可复用的表格原型"，不是"不能动的版块"。
REGION_KINDS: Final[tuple[str, ...]] = ("cover", "revision", "toc", "header")

#: **前导区间型**版的块类别：语义是"从正文开头起的一段连续段落"，定位符只能给出
#: 它的**末段**（区间没有可定位的起点边界，只有终点）。
#: ``registry`` 侧的对应约定见 ``registry.parser.blocks._cover_blocks``——两侧是同一
#: 条接缝，改一侧必须同时改另一侧。
_LEADING_KINDS: Final[tuple[str, ...]] = ("cover",)

#: 段落元素的局部名（前导区间的游走要判它）。
_PARAGRAPH_LOCAL: Final = "p"

#: 文本节点标签（占位符替换只改文本，不碰结构）。
_TEXT_TAG: Final = qn("w:t")


@dataclass
class PlaceholderReport:
    """占位符清理结果。"""

    protected_total: int = 0
    cleared: int = 0
    #: 被清掉的占位段原文（留痕，供审计回答"这一处原来是什么"）。
    cleared_texts: list[str] = field(default_factory=list)
    unmatched: list[str] = field(default_factory=list)
    touched_parts: list[str] = field(default_factory=list)

    def as_payload(self) -> dict[str, object]:
        return {
            "protected_total": self.protected_total,
            "cleared": self.cleared,
            "cleared_texts": list(self.cleared_texts),
            "unmatched": list(self.unmatched),
            "touched_parts": list(self.touched_parts),
        }


# ------------------------------------------------------------------ 一、定位
def locate_regions(ctx: RenderContext) -> list[RegionEntry]:
    """按块索引定位受保护区并登记。

    定位失败**不抛错**：模板被外部工具改过之后块索引会失准，那是模板的问题，
    不该让整次构建当场失败。降级成一条 major 判定并继续——其余版块仍然可用。

    区域可以是一个节点，也可以是一段区间
    ------------------------------------
    封面的语义是"首个分节符之前的连续段落"，它是一个**区间**。故定位符给的是区间
    的末段，这里由它向前游走连续的段落同胞得到整段（见 :func:`_run_ending_at`）。
    把区间退化成"某一个段落"的后果很具体：封面里的占位段落落在区间内却不等于
    那个被定位的段落，清除步骤判它"不在受保护区内"而跳过，而残留护栏照报。
    """
    entries: list[RegionEntry] = []
    for block in sorted(ctx.protected_blocks, key=lambda item: item.block_id):
        if block.kind not in REGION_KINDS:
            continue
        part = ctx.pkg.find_part(block.locator.part)
        if part is None:
            _note_unlocatable(ctx, block.block_id, block.kind, f"部件不存在：{block.locator.part}")
            continue
        hits = xpath(part.read_tree, block.locator.xpath)
        index = block.locator.index or 0
        if not hits or index >= len(hits):
            _note_unlocatable(
                ctx,
                block.block_id,
                block.kind,
                f"XPath 未命中或下标越界：{block.locator.xpath}[{index}]（命中 {len(hits)} 项）",
            )
            continue
        entry = RegionEntry(
            block_id=block.block_id,
            kind=block.kind,
            part=part.path,
            elements=_region_elements(block, hits[index]),
        )
        ctx.regions[block.block_id] = entry
        entries.append(entry)
    return entries


def _region_elements(block: BlockEntry, located: Element) -> tuple[Element, ...]:
    """区域覆盖的元素：前导区间型块展开成一段，其余就是定位到的那个节点。"""
    if block.kind in _LEADING_KINDS:
        return _run_ending_at(located)
    return (located,)


def _run_ending_at(element: Element) -> tuple[Element, ...]:
    """从元素向前游走**连续的段落同胞**（含自身），按文档序返回。

    只走同胞、只认段落：封面区间由一串平级段落组成，遇到非段落（内容控件、表格、
    分节符属性）即到界。这里不含任何"哪里是封面"的判断——那条判断只在
    ``registry`` 里有一处实现，本函数只把定位符给出的终点向前铺开。
    """
    run = [element]
    previous = element.getprevious()
    while previous is not None and localname(previous) == _PARAGRAPH_LOCAL:
        run.append(previous)
        previous = previous.getprevious()
    return tuple(reversed(run))


def _note_unlocatable(ctx: RenderContext, block_id: str, kind: str, reason: str) -> None:
    ctx.note_once(
        TEMPLATE_FIDELITY_RULE_ID,
        "major",
        f"region-unlocatable:{block_id}",
        f"模板版块 {block_id}（{kind}）定位失败：{reason}",
        evidence=reason,
        suggestion="重新入库该模板以刷新块索引，或在界面确认该版块已被删除",
    )


# ------------------------------------------------------------ 二、占位符清理
def locate_placeholders(ctx: RenderContext, entries: list[RegionEntry]) -> list[PlaceholderSite]:
    """把模板包的占位符清单落到元素上，并缓存进 ``ctx``。

    **必须在任何改动之前调用，且整轮只算一次。** 定位符是位置型的
    （``(//w:p)[19]``），裁剪与注入一发生，同一个表达式指向的就是另一个段落。
    清除与残留检查若各自重新解析，两者看的根本不是同一批对象——"清除没生效"
    与"残留检查报了个别的段"会同时出现，而数字看起来都正常。故落点算一次，
    两个步骤共用（同一口径只有一处实现）。
    """
    if ctx.placeholder_sites:
        return ctx.placeholder_sites
    sites: list[PlaceholderSite] = []
    for placeholder in ctx.template.placeholders.entries:
        located = _locate_placeholder(ctx, placeholder.locator)
        if located is None:
            sites.append(
                PlaceholderSite(
                    text=placeholder.text,
                    part=placeholder.locator.part,
                    root=None,
                    element=None,
                    protected=False,
                )
            )
            continue
        owner, root, element = located
        sites.append(
            PlaceholderSite(
                text=placeholder.text,
                part=owner,
                root=root,
                element=element,
                protected=_inside_regions(element, entries),
            )
        )
    ctx.placeholder_sites = sites
    return sites


def clear_protected_placeholders(
    ctx: RenderContext, entries: list[RegionEntry]
) -> PlaceholderReport:
    """清除受保护区内的占位文本（FR-M2-11）。

    契约的语义是**「段」不是「片段」**
    ---------------------------------
    ``registry`` 登记占位符时给出的是 ``locator = (//w:p)[n]`` 加该段的
    **整段可见文本**（且做了空白折叠）。契约里*没有*记下"命中片段"是哪几个字。
    于是"按片段做子串替换"这条实现路径有个致命形态：原件里是全角空格或连续空格时，
    折叠后的 ``entry.text`` 在原件文本里**根本搜不到**，替换静默变成空操作，
    而清单上的数字看起来完全正常。（本模块的第一版正是这样，直到用真实夹具
    跑通才发现页眉占位原封不动。）

    故本模块的判据只落在**定位**上：定位到的段落就是这个占位符所在段落，
    清除它的文本即可——这正是 DD-7.2 给的两条出路里的"**显式清除**"。
    只清 ``w:t`` 的文本、不删运行 / 域 / 书签：占位段落往往正是版式的一部分
    （页眉里 "XX 项目说明书　此处填写项目编号" 这种），删掉运行会连带改掉版式。

    "在受保护区内"这条判据要靠**区间**而不是单点
    -------------------------------------------
    封面是"首个分节符之前的连续段落"，是一个区间。若 ``locate_regions`` 把封面
    退化成某一个段落，封面里的占位段落就判不进去而**被静默跳过**——产物里留着
    "此处填写编制单位"，而清单上说这一处已处理。故这里的判据是"落在区域的**任一**
    元素上或其后代"（见 :meth:`RegionEntry.elements`）。

    清掉了什么必须留痕
    ------------------
    逐段被清掉的原文进 :attr:`PlaceholderReport.cleared_texts`（可 JSON 化，
    落 checkpoint）。"替换"那一半（用项目元数据填封面 / 页眉）要等项目元数据契约，
    在此之前这里是**清除**；把清掉的原文记下来，才能让后续审计回答"这一处原来是
    什么"，而不是只看到一个数字。
    """
    report = PlaceholderReport(protected_total=len(entries))
    sites = locate_placeholders(ctx, entries)
    if not entries:
        return report

    touched: set[str] = set()
    unmatched: list[str] = []
    cleared_texts: list[str] = []
    for site in sites:
        element = site.element
        if element is None:
            unmatched.append(site.text)
            continue
        if not site.protected:
            # 内容区里的占位符随内容区一起被裁掉——对它们做清除是白费功夫，
            # 还会让"占位残留数"这个门禁数字失去意义。
            continue
        if _erase_paragraph_text(element):
            cleared_texts.append(site.text)
            touched.add(site.part)

    for part_path in sorted(touched):
        ctx.pkg.part(part_path).mark_dirty()
    report.cleared = len(cleared_texts)
    report.cleared_texts = sorted(cleared_texts)
    report.unmatched = sorted(unmatched)
    report.touched_parts = sorted(touched)
    if unmatched:
        ctx.note_once(
            PLACEHOLDER_RULE_ID,
            "minor",
            "placeholder-unlocatable",
            f"模板包声明的 {len(unmatched)} 个占位符在原件里定位不到，无法确认是否残留",
            evidence=", ".join(sorted(unmatched)[:5]),
            suggestion="重新入库该模板以刷新占位符清单",
        )
    return report


def _locate_placeholder(
    ctx: RenderContext, locator: BlockLocator
) -> tuple[str, Element, Element] | None:
    """按占位符的 ``BlockLocator`` 定位 ``(部件路径, 部件根, 元素)``。

    连**部件根**一起返回：残留护栏要用它判"这个段落还挂在部件树上吗"
    （见 :meth:`PlaceholderSite.still_in_document`）。``Part.read_tree`` 是按需解析且
    **带缓存**的，所以同一个部件反复取到的是同一棵树，元素同一性可直接比较。
    """
    part = ctx.pkg.find_part(locator.part)
    if part is None:
        return None
    root = part.read_tree
    hits = xpath(root, locator.xpath)
    index = locator.index or 0
    if not hits or index >= len(hits):
        return None
    return part.path, root, hits[index]


def _inside_regions(element: Element, entries: list[RegionEntry]) -> bool:
    """元素是否落在某个受保护区内（自身或祖先命中即算）。

    区域可能有**多个**元素（封面是区间，见 :class:`RegionEntry`），故这里铺平了比。
    """
    region_elements = [item for entry in entries for item in entry.elements]
    if any(element == region for region in region_elements):
        return True
    return any(
        any(ancestor == region for region in region_elements)
        for ancestor in element.iterancestors()
    )


def _erase_paragraph_text(element: Element) -> bool:
    """把元素内所有 ``w:t`` 的文本清空，返回是否真的清掉了东西。

    只清文本节点：``w:pPr``（版式）、``w:r``（运行属性）、域字符、书签一律保留。
    清空而不是删除文本节点：删节点会让段落结构变化，进而影响行高与页眉排版。
    """
    changed = False
    for node in element.iter(_TEXT_TAG):
        if node.text:
            set_text(node, "")
            changed = True
    return changed


# -------------------------------------------------------------- 三、块复用
def copy_block(ctx: RenderContext, block_id: str) -> Element | None:
    """按 ``block_id`` 取模板块原件并深拷贝（复用优先，不重新构造）。

    "可跨模板"体现在定位直接落在 ``BlockLocator.part`` 上：块原件在哪个部件里
    就在哪个部件里取，渲染层不需要知道它就是 ``word/document.xml``。
    """
    entry = _block_entry(ctx, block_id)
    if entry is None:
        ctx.note_once(
            TEMPLATE_FIDELITY_RULE_ID,
            "major",
            f"block-missing:{block_id}",
            f"模板契约里没有块 {block_id}，无法复用该版块",
            evidence=f"block_id={block_id}",
            suggestion="确认档案引用的 block_id 与模板包中的块索引一致",
        )
        return None
    part = ctx.pkg.find_part(entry.locator.part)
    if part is None:
        ctx.note_once(
            TEMPLATE_FIDELITY_RULE_ID,
            "major",
            f"block-part-missing:{block_id}",
            f"块 {block_id} 所在部件不在包内：{entry.locator.part}",
            evidence=f"part={entry.locator.part}",
            suggestion="重新入库该模板以刷新块索引",
        )
        return None
    hits = xpath(part.read_tree, entry.locator.xpath)
    index = entry.locator.index or 0
    if not hits or index >= len(hits):
        ctx.note_once(
            TEMPLATE_FIDELITY_RULE_ID,
            "major",
            f"block-unlocatable:{block_id}",
            f"块 {block_id} 在原件里定位不到：{entry.locator.xpath}[{index}]",
            evidence=f"hits={len(hits)}",
            suggestion="重新入库该模板以刷新块索引",
        )
        return None
    return deep_copy(hits[index])


def _block_entry(ctx: RenderContext, block_id: str) -> BlockEntry | None:
    wanted = block_id.strip()
    if not wanted:
        return None
    for entry in ctx.template.blocks.blocks:
        if entry.block_id == wanted:
            return entry
    return None


def block_reference_of(unit: ContentUnit) -> str:
    """``block_ref`` 单元引用的块 ID（``fields['block_id']`` 或直接给在 ``text`` 上）。"""
    value = unit.fields.get("block_id")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return (unit.text or "").strip()


def block_text_of(element: Element) -> str:
    """块元素的可见文本摘要（诊断与测试断言用）。"""
    paragraph = find_first(element, "w:p")
    if paragraph is not None:
        return paragraph_text(paragraph)
    return "".join(node.text or "" for node in element.iter(_TEXT_TAG))


def placeholder_residue(ctx: RenderContext) -> list[str]:
    """产物里**仍有文本**的占位段原文（应为空）。

    这是写盘前的护栏（FR-M5-09「占位残留数」）。判据刻意**只落在定位上、不做文本
    匹配**：契约登记的是一整段（见 ``clear_protected_placeholders`` 的说明），
    而"再按文本搜一遍"会与清除逻辑共用同一个错误前提——两边同时失效，表现是
    "清除没生效、残留检查也报 0"。这里改为"定位到那个段落，看它是否还是非空"。

    两条附加条件缺一不可
    --------------------
    ① **用改动之前算好的落点**（``ctx.placeholder_sites``），不重新解析定位符。
       位置型定位符在裁剪与注入之后会指向别的段落——重新解析等于换了一批判据对象，
       报出来的"残留"和清除过的根本不是同一处。
    ② **元素必须仍挂在文档树里**。内容区里的占位段随内容区一起被裁掉，元素成了游离
       子树：文本还在内存里，产物里却已经没有这一段。漏掉这一条就会把"已裁掉"
       当成"残留"报出来（这正是此前每份产物都误报一条 FR-M2-11 的另一半成因）。

    复核的范围是**全部**占位符，不只受保护区
    ----------------------------------------
    早先的实现把范围收窄到"受保护区"，理由是"内容区的占位符应随内容区被裁掉，
    真残留是另一条判定"。那个理由让护栏看不见一处**确实印在产物里**的编辑痕迹，
    是净损失。条件②直接判"它还在不在产物里"，既更简单也更严：内容区没裁干净同样是
    残留，照样要报。
    """
    residue: list[str] = []
    for site in locate_placeholders(ctx, ctx.region_entries(REGION_KINDS)):
        element = site.element
        if element is None or not site.still_in_document():
            continue
        if paragraph_text(element).strip():
            residue.append(site.text)
    return sorted(set(residue))


def placeholder_residue_in_package(
    pkg: DocxPackage, entries: Sequence[PlaceholderEntry]
) -> list[str]:
    """**只看产物**的占位残留判据：登记的占位原文还印在产物里吗（应为空）。

    与 :func:`placeholder_residue` 的分工
    -------------------------------------
    ``placeholder_residue`` 读的是**渲染上下文**，判据落在"定位到的那个段落是否还
    非空"上（写盘前的护栏，落点由注入前的 ``ctx.placeholder_sites`` 冻结）。它不适
    用于**产物侧**复查：门禁只有产物，重建上下文后位置型定位符（``(//w:p)[n]``）
    会指向**别**的段落，于是判出来的是"某段落非空"——那些段落本来就是正文内容，
    报告上于是出现"产物里仍有 3 处占位残留"，而产物里一个占位字都没有。

    产物侧唯一**可行**的判据是文本探针
    ----------------------------------
    判据不能落在定位上：位置在产物里已经不可复原（"改动前的落点"只存在于渲染进程
    的内存里）。剩下的唯一问法是"这段占位原文在产物里还找得到吗"——它对"清除静默
    没生效"同样有效（没清掉就是找得到），却不会与清除逻辑共用错误前提，因为两侧的
    归一化口径一致（登记时 ``placeholders._collapse`` 与这里都是空白折叠）。

    按**登记时所在的部件**扫描，不扫全包：占位符的 ``locator.part`` 就是它当初所在
    部件，扫全包会把"同一个词恰好出现在正文"也算成残留。
    """
    residue: list[str] = []
    for entry in entries:
        target = _collapse(entry.text)
        if not target:
            continue
        part_name = entry.locator.part
        part = pkg.find_part(part_name)
        if part is None:
            continue
        for paragraph in iter_elements(part.read_tree, "w:p"):
            if target in _collapse(paragraph_text(paragraph)):
                residue.append(entry.text)
                break
    return sorted(set(residue))


def _collapse(text: str) -> str:
    """空白折叠。与入库登记（``registry.parser.placeholders._collapse``）同一口径。"""
    return " ".join(text.split())
