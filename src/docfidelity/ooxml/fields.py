"""域与内容控件：目录重建（DD-4.6）。

目录重建的原则
--------------
**不做"按契约重写 XML"，而是在原目录条目上做最小改动**：取模板里已有的目录条目
作原型逐条改写，只动三处——标题文本、书签锚点、``PAGEREF`` 域代码里的书签名。
条目样式、制表位、点前导符、缩进全部原样保留，因为它们是模板体例的一部分，
一旦由程序重建（例如自己拼 ``<w:tabs><w:tab w:leader="dot"/>``）就必然走样。

**域字符必须保住**。目录块里有两层域：每个条目自带的 ``PAGEREF`` 域，以及包裹
整块的 ``TOC`` 域（``begin`` 在第一条、``end`` 在最后一条）。所以：

1. **优先原地改写**：条目数与骨架条数相同时，一条都不删、一条都不建，只改文本。
2. **多余条目先"搬域"再删**：删之前把待删段落里的 ``fldChar`` / ``instrText``
   运行搬到新的末条上，保住 ``TOC`` 域的配对；否则"目录"会退化成静态文本——
   页面上看不出差别，但 Word 再也不会重算页码。
3. **条目不够才新建**，并用 :func:`_push_outer_field_end` 把 ``TOC`` 域的 ``end``
   挪到新的末条上。不挪的话域只覆盖到原型那几条，新增条目全落在域外——页面看着
   齐全，**用户在 Word 里更新目录却会得到一份重复的目录**。

骨架为空时**不清空目录**：清空会连域一起毁掉，宁可留着模板目录并报警告。

"装上 Word 就算完成"这件事不在本模块
------------------------------------
域结果回读与 Word COM 调用在 :mod:`docfidelity.ooxml.refresh`。分家的理由有两条：
刷新要在**产物的临时副本**上起 Word 进程（与"只改结构"是两种副作用），以及
NFR-09 的单文件行数上限——两者混在一个模块里，任何一边加几段注释就会越线。
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field

from lxml import etree

from docfidelity.contracts.errors import EngineError, ErrorCode
from docfidelity.contracts.template import BlockEntry, ChapterNode, OutlineSkeleton

from .doc import (
    Element,
    attr,
    deep_copy,
    find_first,
    localname,
    qn,
    set_text,
    xpath,
)
from .package import DOCUMENT_PART, DocxPackage

_SETTINGS_PART = "word/settings.xml"
_TOC_FIELD_RE = re.compile(r"^\s*TOC\b")
_BOOKMARK_NAME_RE = re.compile(r"_Toc\d+")

#: Word 的域刷新需要本地 Office；这是 P0 的可降级路径（风险项 #3）。
#:
#: 注意：这里**没有超时**。原先有一个 ``_OFFICE_TIMEOUT_SECONDS = 120``，但它从未被
#: 任何代码读过——它唯一的作用是让"超时"看起来已经被处理了。一次不返回的 COM 调用
#: （Word 弹了模态框之类）会把这一步一直挂住。要真做超时只有两条路：另起进程（等于
#: 把这段重写一遍）或开线程后放弃它（被放弃的线程仍可能反写产物，破坏 NFR-02 的
#: 确定性，比不做更糟）。故如实记在注释里，不摆一个假的旋钮。

#: 域机制的三个标记元素。``w:t`` 里出现 PAGEREF 字样不算域。
_FIELD_TAGS = frozenset({"fldChar", "instrText"})

#: ``w:fldChar/@w:fldCharType`` 的三个取值。``separate`` 不影响嵌套深度，不参与计数。
#:
#: 与 :func:`field_marker` 一起公开：域结果回读在 ``ooxml.refresh``，它需要按同一套
#: 取值判域边界——两处各写一份字符串字面量，迟早漂移成"一边认 ``separate``、另一边不认"。
FIELD_BEGIN = "begin"
FIELD_END = "end"
FIELD_SEPARATE = "separate"

#: 书签名的前缀（``_Toc00000001`` 这种）——目录项的锚点按**名字**引用。
_TOC_NAME_PREFIX = "_Toc"


@dataclass
class TocResult:
    """目录重建结果。

    ``needs_refresh`` 默认 ``True``：目录页码由域计算，重建后必然过期，
    只有 :func:`refresh_with_office` 成功才配得上 ``False``。
    """

    entries: int = 0
    levels: list[int] = field(default_factory=list)
    bookmarks_bound: int = 0
    rewritten_in_place: int = 0
    created: int = 0
    #: 被搬移的外层 TOC 域运行数——**两个方向都算**：条目变少时从待删段落里"救出来"
    #: （:func:`_hoist_field_runs`），变多时把 ``end`` 挪到新的末条（:func:`_push_outer_field_end`）。
    #: 二者都是"为了让域夹住整块"。计数为 0 而条目数变了，就是域被落下了一处。
    hoisted_field_runs: int = 0
    field_preserved: bool = False
    needs_refresh: bool = True
    parts_touched: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


# ------------------------------------------------------------------ 目录重建
def rebuild_toc(
    pkg: DocxPackage,
    toc_block: BlockEntry,
    outline: OutlineSkeleton,
    *,
    style_for_level: Mapping[int, str] | None = None,
) -> TocResult:
    """按章节骨架重建目录条目（FR-M4-09）。

    ``style_for_level`` 是"大纲层级 -> 目录条目样式 ID"的映射。不传时会尝试按
    模板中现有条目的样式出现顺序推断，并在 ``warnings`` 里如实说明——推断结果
    未经人工确认，不能当作契约。
    """
    result = TocResult()
    part = pkg.part(toc_block.locator.part)
    content = find_first(_locate(part.tree, toc_block), "w:sdtContent")
    if content is None:
        raise EngineError(
            ErrorCode.PACKAGE_INCOMPLETE,
            "目录内容控件缺少 w:sdtContent，无法重建目录",
            where={"part": part.path, "xpath": toc_block.locator.xpath},
            remediation=["在 Word 中重新插入目录", "或把该块从模板块索引中移除"],
        )

    paragraphs = [child for child in content if localname(child) == "p"]
    result.field_preserved = any(_has_field_runs(item) for item in paragraphs)
    if not result.field_preserved:
        result.warnings.append(
            "模板目录中没有 TOC 域字符：重建后 Word 无法自动重算页码，只能显示静态条目"
        )
    if not paragraphs:
        result.warnings.append("模板目录中没有任何条目，已跳过重建")
        result.needs_refresh = False
        return result

    entries = list(_flatten_outline(outline))
    if not entries:
        result.warnings.append("章节骨架为空，为保住 TOC 域未清空目录，请确认骨架配置")
        result.needs_refresh = False
        return result

    prototype_by_style = _prototype_by_style(paragraphs)
    level_style = _resolve_level_styles(
        sorted({level for level, _ in entries}), prototype_by_style, style_for_level, result
    )
    anchors = toc_anchor_names(pkg)
    if len(anchors) < len(entries):
        result.warnings.append(
            f"文档中只有 {len(anchors)} 个目录书签，少于 {len(entries)} 个目录项："
            "页码域刷新后会指向错误位置，需在渲染阶段补齐书签"
        )

    reused = min(len(entries), len(paragraphs))
    last_kept = paragraphs[reused - 1]
    for index in range(reused):
        level, title = entries[index]
        anchor = anchors[index] if index < len(anchors) else None
        _rewrite_entry(last_kept if index == reused - 1 else paragraphs[index], title, anchor)
        result.entries += 1
        result.rewritten_in_place += 1
        result.bookmarks_bound += int(anchor is not None)
        _track_level(result, level)

    surplus = paragraphs[reused:]
    if surplus:
        # 先搬域再删段：待删段落里挂着 TOC 域的 end（PAGEREF 域则随段落一起删）。
        result.hoisted_field_runs = _hoist_field_runs(paragraphs[:reused], surplus, last_kept)
        for item in surplus:
            content.remove(item)
        if _has_field_runs(last_kept) and not result.field_preserved:
            result.field_preserved = True

    if len(entries) > reused:
        insert_at = _child_index(content, last_kept) + 1
        appended: list[Element] = []
        for level, title in entries[reused:]:
            prototype = prototype_by_style.get(level_style.get(level) or "", paragraphs[0])
            anchor = anchors[result.entries] if result.entries < len(anchors) else None
            entry = _build_entry(prototype, title, anchor)
            content.insert(insert_at, entry)
            insert_at += 1
            appended.append(entry)
            result.entries += 1
            result.created += 1
            result.bookmarks_bound += int(anchor is not None)
            _track_level(result, level)
        if appended:
            # 新条目插在 last_kept 之后，TOC 域的 end 却还留在 last_kept 里——
            # 不挪过去，新增的每一条都落在域外（见 _push_outer_field_end）。
            result.hoisted_field_runs += _push_outer_field_end(
                paragraphs[: reused - 1], last_kept, appended[-1]
            )

    part.mark_dirty()
    result.parts_touched.append(part.path)
    _request_field_update(pkg, result)
    return result


def needs_refresh(toc_result: TocResult) -> bool:
    """目录是否需要 Word 刷新域。"""
    return toc_result.needs_refresh


def _track_level(result: TocResult, level: int) -> None:
    if level not in result.levels:
        result.levels.append(level)


def _child_index(parent: Element, child: Element) -> int:
    for index, item in enumerate(parent):
        if item is child:
            return index
    return len(parent)


def _locate(root: Element, block: BlockEntry) -> Element:
    hits = xpath(root, block.locator.xpath)
    if not hits:
        raise EngineError(
            ErrorCode.PACKAGE_INCOMPLETE,
            "按块索引定位不到目录控件，模板可能已被改动",
            where={"part": block.locator.part, "xpath": block.locator.xpath},
            remediation=["重新入库该模板以刷新块索引"],
        )
    index = block.locator.index or 0
    if index >= len(hits):
        raise EngineError(
            ErrorCode.PACKAGE_INCOMPLETE,
            f"目录控件序号越界：{index}（命中 {len(hits)} 项）",
            where={"part": block.locator.part, "xpath": block.locator.xpath},
            remediation=["重新入库该模板以刷新块索引"],
        )
    return hits[index]


def _has_field_runs(element: Element) -> bool:
    """元素（段落或运行）内是否含域字符或域代码。"""
    return any(localname(node) in _FIELD_TAGS for node in element.iter())


def _hoist_field_runs(kept: Sequence[Element], surplus: Sequence[Element], target: Element) -> int:
    """把**属于外层 TOC 域**的域运行搬到存活段落末尾，返回搬移数量。

    待删的目录条目里混着两类域运行：条目自带的 ``PAGEREF`` 域（应随条目一起删掉），
    以及包裹整块的 ``TOC`` 域的 ``end``（必须留下，否则目录退化成静态文本）。
    两者同在一个段落里，按标签名区分不出来——故按**域的嵌套深度**区分：

    * 从存活段落结束时的深度出发继续扫（TOC 域此时是"开着的"，深度 ≥ 1）；
    * 只有让深度**归零**的那个 ``fldChar end`` 属于 TOC 域，搬走；
    * 深度归零之前的所有域运行都在某个内层域里，随段落一起删。

    "把所有含域字符的运行都搬走"是错的：它会把被删条目的 PAGEREF 域整体挂到存活
    条目上，于是第一个目录项后面跟着**两个页码**，而页面上看不出这是缺陷。
    """
    depth = _field_depth_after(kept)
    if depth <= 0:
        # 存活段落里没有未闭合的域 ⇒ 待删段落里不存在"必须保住"的域运行。
        return 0

    hoisted = 0
    for paragraph in surplus:
        for run in list(paragraph.iter(qn("w:r"))):
            marker = field_marker(run)
            if marker == FIELD_BEGIN:
                depth += 1
                continue
            if marker != FIELD_END:
                continue
            depth -= 1
            if depth > 0:
                continue
            # 深度归零 ⇒ 这个 end 关掉的是外层 TOC 域，它必须活下来。
            _move_run(run, target)
            hoisted += 1
            depth = 0
    return hoisted


def _move_run(run: Element, target: Element) -> None:
    """把一个 ``w:r`` 从原处搬到 ``target`` 末尾。只搬运行，不搬段落壳子。"""
    parent = run.getparent()
    if parent is not None:
        parent.remove(run)
    target.append(run)


def _push_outer_field_end(preceding: Sequence[Element], source: Element, target: Element) -> int:
    """把 ``source`` 里关闭**外层 TOC 域**的 ``end`` 运行搬到 ``target`` 末尾。

    ``preceding`` 是 ``source`` **之前**的条目（不含 ``source`` 自己）："当前还开着
    几层域"要从它们末尾算起，把 ``source`` 算进去就等于把它自己那个待搬的 ``end``
    先抵扣掉了。

    与 :func:`_hoist_field_runs` 是同一件事的两个方向：那里是"带 ``end`` 的段落要被
    删掉，把 ``end`` 救出来"；这里是"带 ``end`` 的段落不再是最后一条，把 ``end``
    挪到新的最后一条"。

    **条目只增不减时这一步是必需的**。模板目录条目比文档骨架少时，新条目插在
    ``last_kept`` 之后，而 ``TOC`` 域的 ``end`` 还留在 ``last_kept`` 里——域的结果
    于是只覆盖到第 ``reused`` 条。产物在页面上看不出异常（五个条目一应俱全），
    但**用户在 Word 里更新目录会得到一份重复的目录**：域内的条目被整份重建，
    域外那几条原样留着，两份叠在一起。

    判据与 :func:`_hoist_field_runs` 相同——按域**嵌套深度**：条目自带的 ``PAGEREF``
    域在段内自相配对、不影响深度，只有把深度减到零的那个 ``end`` 才属于外层 TOC 域。
    """
    depth = _field_depth_after(preceding)
    if depth <= 0:
        return 0

    moved = 0
    for run in list(source.iter(qn("w:r"))):
        marker = field_marker(run)
        if marker == FIELD_BEGIN:
            depth += 1
            continue
        if marker != FIELD_END:
            continue
        depth -= 1
        if depth > 0:
            continue
        _move_run(run, target)
        moved += 1
        depth = 0
    return moved


def field_marker(run: Element) -> str | None:
    """运行里的域标记类型（``begin`` / ``end``）；不是域标记运行则 ``None``。"""
    for node in run.iter(qn("w:fldChar")):
        return attr(node, "w:fldCharType")
    return None


def _field_depth_after(elements: Sequence[Element]) -> int:
    """扫过 ``elements`` 之后仍开着的域层数（``begin`` 加、``end`` 减）。"""
    depth = 0
    for element in elements:
        for node in element.iter(qn("w:fldChar")):
            kind = attr(node, "w:fldCharType")
            if kind == FIELD_BEGIN:
                depth += 1
            elif kind == FIELD_END:
                depth -= 1
    return depth


def _prototype_style(paragraph: Element) -> str | None:
    return attr(find_first(find_first(paragraph, "w:pPr"), "w:pStyle"), "w:val")


def _flatten_outline(outline: OutlineSkeleton) -> Iterator[tuple[int, str]]:
    """按文档顺序展开章节骨架为 ``(层级, 标题)``。"""

    def visit(nodes: Sequence[ChapterNode]) -> Iterator[tuple[int, str]]:
        for node in nodes:
            yield node.level, node.title
            yield from visit(node.children)

    yield from visit(outline.chapter_tree)


def _prototype_by_style(prototypes: Sequence[Element]) -> dict[str, Element]:
    mapping: dict[str, Element] = {}
    for prototype in prototypes:
        style_id = _prototype_style(prototype)
        if style_id is not None and style_id not in mapping:
            mapping[style_id] = prototype
    return mapping


def _resolve_level_styles(
    level_order: Sequence[int],
    prototype_by_style: Mapping[str, Element],
    style_for_level: Mapping[int, str] | None,
    result: TocResult,
) -> dict[int, str]:
    if style_for_level is not None:
        return {level: style_for_level[level] for level in level_order if level in style_for_level}
    inferred = {
        level: style_id for level, style_id in zip(level_order, prototype_by_style, strict=False)
    }
    if inferred:
        result.warnings.append(
            "目录层级样式由模板现有条目的样式顺序推断，未经契约确认："
            f"推断结果 {inferred}。请在档案中显式声明 style_for_level 后重新构建。"
        )
    return inferred


def toc_anchor_names(pkg: DocxPackage, *, part: str = DOCUMENT_PART) -> list[str]:
    """部件中按文档序出现的目录书签名（``_Toc\\d+``）——目录项的锚点候选。"""
    target = pkg.find_part(part)
    if target is None:
        return []
    return [name for name in _bookmark_names(target.read_tree) if _BOOKMARK_NAME_RE.match(name)]


def bind_missing_toc_bookmarks(
    pkg: DocxPackage, paragraphs: Sequence[Element], *, part: str = DOCUMENT_PART
) -> int:
    """给**还没有** ``_Toc`` 书签的标题段落补一个，返回新增数量（FR-M4-09）。

    为什么这件事必须有人做：目录条目的超链接与 ``PAGEREF`` 域都按**书签名**指向正文
    标题，而模板自带的 ``_Toc`` 书签挂在模板的**示例标题**上——注入步骤把示例内容区
    整片裁掉时，那些书签跟着一起没了。不补，目录条目就指向一堆不存在的锚点：
    Word 刷新后页码全空，点击也不跳转。:func:`rebuild_toc` 只把这件事写成警告
    （"需在渲染阶段补齐书签"），补齐是渲染层的事，故这里给出唯一的一份实现。

    幂等：段落里已有任何 ``_Toc\\d+`` 书签就跳过。名字与 ``w:id`` 都从包内现有取值
    续编——撞车的后果是目录项指到**别的**标题上，比"没有锚点"更难发现。
    """
    target = pkg.find_part(part)
    if target is None:
        return 0
    root = target.read_tree
    used = set(_bookmark_names(root))
    next_id = _next_bookmark_id(root)
    created = 0
    for paragraph in paragraphs:
        if any(_BOOKMARK_NAME_RE.match(name) for name in _bookmark_names(paragraph)):
            continue
        name = _fresh_toc_name(used)
        used.add(name)
        bind_toc_bookmark(paragraph, name=name, bookmark_id=next_id)
        next_id += 1
        created += 1
    if created:
        target.mark_dirty()
    return created


def bind_toc_bookmark(paragraph: Element, *, name: str, bookmark_id: int) -> None:
    """给段落挂一对 ``_Toc`` 书签（``start`` 与 ``end``）。

    位置照 Word 的写法：``w:bookmarkStart`` 紧跟 ``w:pPr`` 之后（没有 ``pPr`` 就放
    段落最前），``w:bookmarkEnd`` 追加在段落末尾。**两端必须成对**——只挂一端不是
    "半成品"，而是**文档损坏**（``normalize`` 对书签配对的纪律同此）。
    """
    start = etree.Element(qn("w:bookmarkStart"))
    start.set(qn("w:id"), str(bookmark_id))
    start.set(qn("w:name"), name)
    properties = find_first(paragraph, "w:pPr")
    if properties is not None:
        properties.addnext(start)
    else:
        paragraph.insert(0, start)
    end = etree.Element(qn("w:bookmarkEnd"))
    end.set(qn("w:id"), str(bookmark_id))
    paragraph.append(end)


def _bookmark_names(root: Element) -> Iterator[str]:
    """元素（或段落）内按文档序的 ``w:bookmarkStart/@w:name`` 取值。"""
    for element in root.iter(qn("w:bookmarkStart")):
        name = attr(element, "w:name")
        if name:
            yield name


def _next_bookmark_id(root: Element) -> int:
    """包内下一个可用的书签 ``w:id``（现有最大值 +1；一个书签都没有时从 1 起）。"""
    largest = 0
    for element in root.iter(qn("w:bookmarkStart"), qn("w:bookmarkEnd")):
        value = attr(element, "w:id")
        if value is None or not value.isdigit():
            continue
        largest = max(largest, int(value))
    return largest + 1


def _fresh_toc_name(used: set[str]) -> str:
    """取一个尚未被占用的 ``_Toc`` 书名，沿用 Word 的 8 位补零形态。"""
    index = 1
    while f"{_TOC_NAME_PREFIX}{index:08d}" in used:
        index += 1
    return f"{_TOC_NAME_PREFIX}{index:08d}"


def _build_entry(prototype: Element, title: str, anchor: str | None) -> Element:
    """深拷贝原型后做最小改动，产出新条目。"""
    entry = deep_copy(prototype)
    _strip_block_field_runs(entry)
    _strip_block_page_break(entry)
    _rewrite_entry(entry, title, anchor)
    return entry


def _strip_block_page_break(paragraph: Element) -> None:
    """从条目副本里摘掉**版块级**的强制分页（``w:pPr/w:pageBreakBefore``）。

    目录块的第一条往往带着"目录另起一页"的 ``pageBreakBefore``——那是**整个目录
    版块**的属性（渲染层第 9 步把它落在受保护区首段上），不是条目原型的属性。
    原型恰好是首段时，深拷贝会把这道分页一起复制给每一个新建条目：产物表面只是
    "多了一个条目"，实际却是每个新条目各占一页，而且分页点落在白名单之外——
    门禁会据此判 FR-M5-04 阻断（分页合规）。故克隆出来的条目一律抹掉这道分页。
    """
    properties = find_first(paragraph, "w:pPr")
    if properties is None:
        return
    break_before = find_first(properties, "w:pageBreakBefore")
    if break_before is not None:
        properties.remove(break_before)


def _strip_block_field_runs(paragraph: Element) -> None:
    """从条目副本里摘掉**属于外层 TOC 域**的域运行。

    目录块的第一条带着 ``TOC`` 域的 ``begin``（＋指令＋``separate``），最后一条带着
    它的 ``end``。拿这类段落当原型新建条目时，那些运行会被一起复制——产物里于是出现
    **两个** ``TOC \\o "1-3"`` 指令与两组不配对的域字符，Word 打开后目录行为不可预期
    （而页面上只是"多了个条目"，看不出异常）。

    条目自己的 ``PAGEREF`` 域必须留下——它才是这个条目的页码。判据是**配对性**：
    段落内能自相配对的域（``begin``…``end``）属于条目，配不上的那个（开头的 ``begin``
    / 结尾的 ``end``）属于外层 TOC 域。
    """
    runs = [run for run in paragraph.iter(qn("w:r")) if _has_field_runs(run)]
    if not runs:
        return
    matched_begin, matched_end = _matched_field_pairs(runs)

    drop: list[Element] = []
    pending_outer = False
    for index, run in enumerate(runs):
        if _is_toc_instruction(run):
            drop.append(run)
            pending_outer = True
            continue
        kind = field_marker(run)
        if kind == FIELD_BEGIN:
            if index in matched_begin:
                pending_outer = False
                continue
            drop.append(run)
            pending_outer = True
            continue
        if kind == FIELD_END:
            if index not in matched_end:
                drop.append(run)
            continue
        if kind == FIELD_SEPARATE and pending_outer:
            drop.append(run)
        pending_outer = False

    for run in drop:
        parent = run.getparent()
        if parent is not None:
            parent.remove(run)


def _matched_field_pairs(runs: Sequence[Element]) -> tuple[set[int], set[int]]:
    """段落内自相配对的 ``begin`` / ``end`` 运行下标（栈式匹配）。"""
    stack: list[int] = []
    matched_begin: set[int] = set()
    matched_end: set[int] = set()
    for index, run in enumerate(runs):
        kind = field_marker(run)
        if kind == FIELD_BEGIN:
            stack.append(index)
        elif kind == FIELD_END and stack:
            matched_begin.add(stack.pop())
            matched_end.add(index)
    return matched_begin, matched_end


def _is_toc_instruction(run: Element) -> bool:
    """运行里是否含目录域指令（``TOC \\o "1-3"``）。"""
    return any(_TOC_FIELD_RE.match(node.text or "") for node in run.iter(qn("w:instrText")))


def _rewrite_entry(entry: Element, title: str, anchor: str | None) -> None:
    """改写条目的标题文本、书签锚点与 ``PAGEREF`` 域代码。

    只动第一个 ``w:t``：目录条目的文本结构是"标题 -> 制表符 -> 页码域"，
    页码那个 ``w:t`` 必须留给域去填，改了它反而会让 Word 把域结果写坏。
    """
    texts = list(entry.iter(qn("w:t")))
    if texts:
        set_text(texts[0], title)

    for hyperlink in entry.iter(qn("w:hyperlink")):
        if anchor is None:
            hyperlink.attrib.pop(qn("w:anchor"), None)
        else:
            hyperlink.set(qn("w:anchor"), anchor)

    if anchor is None:
        return
    for instruction in entry.iter(qn("w:instrText")):
        current = instruction.text or ""
        if _BOOKMARK_NAME_RE.search(current):
            instruction.text = _BOOKMARK_NAME_RE.sub(anchor, current)


def _request_field_update(pkg: DocxPackage, result: TocResult) -> None:
    """置 ``w:updateFields``，让 Word 打开产物时自动刷新页码。

    没有这一步，产物里显示的是模板的旧页码——比"目录为空"更糟，因为它是**错的
    而不是缺的**，使用者很难察觉。设置部件缺失时如实记入 warnings。
    """
    part = pkg.find_part(_SETTINGS_PART)
    if part is None:
        result.warnings.append("模板缺少 word/settings.xml，无法置位 updateFields")
        return
    element = find_first(part.tree, "w:updateFields")
    if element is None:
        element = etree.Element(qn("w:updateFields"))
        part.tree.append(element)
    element.set(qn("w:val"), "true")
    part.mark_dirty()
    if part.path not in result.parts_touched:
        result.parts_touched.append(part.path)


def toc_field_levels(pkg: DocxPackage, toc_block: BlockEntry) -> tuple[int, int] | None:
    """从目录域代码 ``TOC \\o "1-3"`` 读出层级范围。

    用于校验"骨架层级超出目录域覆盖范围"——超出部分的标题不会出现在目录里，
    属于静默丢内容，必须在 qa 阶段报出来。
    """
    part = pkg.find_part(toc_block.locator.part)
    if part is None:
        return None
    for element in part.read_tree.iter(qn("w:instrText")):
        text = element.text or ""
        if not _TOC_FIELD_RE.match(text):
            continue
        match = re.search(r'\\o\s*"(\d+)-(\d+)"', text)
        if match:
            return int(match.group(1)), int(match.group(2))
    return None


def toc_summary(result: TocResult) -> str:
    """人读摘要（用于作业详情与报告）。"""
    if result.entries == 0:
        return "目录未生成：模板中没有可用作原型的目录条目"
    return (
        f"目录已重建 {result.entries} 条（层级 {result.levels}），"
        f"绑定书签 {result.bookmarks_bound} 个"
        + ("，TOC 域已保留" if result.field_preserved else "，TOC 域缺失")
        + ("，需在 Word 中刷新页码" if result.needs_refresh else "")
    )


__all__ = [
    "FIELD_BEGIN",
    "FIELD_END",
    "FIELD_SEPARATE",
    "TocResult",
    "bind_missing_toc_bookmarks",
    "bind_toc_bookmark",
    "field_marker",
    "needs_refresh",
    "rebuild_toc",
    "toc_anchor_names",
    "toc_field_levels",
    "toc_summary",
]
