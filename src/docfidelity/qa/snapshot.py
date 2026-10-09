"""结构快照：**只比结构，不比字节**的回归基线（DD-17.2 / FR-M5-13 / TASK-P0-19）。

它解决的是什么
--------------
"这次改动有没有动到产物的形态"需要一个可存进仓库、可逐字比对的答案。逐字节比对
做不到：产物里有两类**必然不同**的东西——``docProps/core.xml`` 的时间戳、各部件
的关系 ID 与书签编号分配（它们由遍历顺序决定，换个 lxml 小版本就可能变）。拿字节
做基线，CI 会长期红着，而红的原因与"改动是否有害"毫无关系。

于是把包**投影**成一个结构描述：段落数、样式引用直方图、表格形状、图片尺寸、
分页点位置、分节数。这份投影对上面两类易变标识天然不敏感，对"真的改动了文档"
则立刻可见。

和 ``qa.compare`` 是什么关系
----------------------------
那边的 :func:`~docfidelity.qa.compare.metrics_of` 是**同一次构建内**的粗粒度计数
（"段落 1284 条"），服务于 FR-M5-11 的稳定化比对与 FR-M5-13 的"与上一版产物比对"。
本模块是**跨构建的离线基线**：它把投影落成 ``tests/golden/*.json`` 存进仓库，供
integration 层比对（DD-17.1 的"golden 结构快照一致"）。两者的交集只有"段落数"
一项，刻意不合并——一个要在运行期产出用于报告，一个要能被人 diff 与评审。

两处**未**按 DD-17.2 示例落实的字段
-----------------------------------
1. ``pictures[].caption`` —— 不记。DD-17.2 的示例格式里有它，但产物目前不放图片
   （``FactSnapshot`` 不带 assets，``render_stage`` 不注入 ``asset_loader``），
   记一个恒为空的字段只会让"图注丢了"与"根本没有图"长得一样。图片通路打通后补。
2. ``page_breaks[].after_unit`` —— 改记 ``index`` + ``before``。``unit_id`` **不落地**
   到 docx（它只活在 ``RenderContext.injected_tables`` 这类内存表里），从产物反推
   不出来；硬造一个"最近的标题文本"当单元名，会在标题改写时给出误导性的差异条目。
   ``index`` 是分页点所在段落的 body 顶层下标（``render.pagination.break_sites``
   的第三项），``before`` 是它起第一个非空段落的文本前缀——两者都**只由结构决定**。
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from docfidelity.ooxml import (
    DOCUMENT_PART,
    DocxPackage,
    attr,
    find_all,
    find_first,
    iter_elements,
    localname,
    paragraph_text,
    read_extents,
)
from docfidelity.render.pagination import break_sites

__all__ = [
    "BREAK_LOCAL",
    "BREAK_BEFORE_LOCAL",
    "PREFIX_CHARS",
    "SnapshotDiff",
    "diff_snapshots",
    "structure_snapshot",
]

#: 分页点的两种载体（``render.pagination`` 的同类常量在这里**另起一份**：那边的
#: 是渲染期用于插分页的，这里是读产物用的。两者取同一个字面量，改一处即错——故在
#: ``tests/unit/test_qa_snapshot.py`` 里断言两边相等，而不是靠"记得同步"）。
BREAK_LOCAL: Final = "br"
BREAK_BEFORE_LOCAL: Final = "pageBreakBefore"

#: ``page_breaks[].before`` 取多长。够人认出"这是目录那一段"，又短到 diff 里一行放得下。
PREFIX_CHARS: Final = 40

#: 会被计进样式直方图的引用属性。与 ``qa.compare._STYLE_REF_LOCALS`` 同集合。
_STYLE_REF_LOCALS: Final[tuple[str, ...]] = ("pStyle", "tblStyle", "rStyle")


def structure_snapshot(pkg: DocxPackage, *, doc_key: str = "") -> dict[str, Any]:
    """把包投影成 DD-17.2 形状的结构描述（可直接 ``json.dump``）。

    ``doc_key`` 由调用方给：产物里没有"这份文档在项目里叫什么"这个事实，它属于
    构建输入而不是产物（``ContentIR.doc_key``）。由调用方传而不是从文件名猜——
    文件名是 ``output_filename()`` 拼出来的，改命名规则就会让基线与产物对不上。
    """
    style_refs: Counter[str] = Counter()
    paragraphs = 0
    for part in pkg.parts(scope="content"):
        for element in part.read_tree.iter():
            name = localname(element)
            if name == "p":
                paragraphs += 1
            elif name in _STYLE_REF_LOCALS:
                value = attr(element, "w:val")
                if value:
                    style_refs[value] += 1

    return {
        "doc_key": doc_key,
        "paragraph_count": paragraphs,
        # 转成普通 dict 并按样式名排序：``Counter`` 的 repr 带类型名，而 JSON 的
        # 键序会影响 diff 的可读性（也会让"只改了顺序"看起来像改动）。
        "style_refs": dict(sorted(style_refs.items())),
        "tables": _tables(pkg),
        "pictures": _pictures(pkg),
        "page_breaks": _page_breaks(pkg),
        "sections": _sections(pkg),
    }


def _tables(pkg: DocxPackage) -> list[dict[str, Any]]:
    """逐张表的形状。``widths_twips`` 取 ``w:tblGrid`` 的**声明**宽度。

    用声明宽度而不是实际渲染宽度：后者要等 Word 排版，而基线必须在没有 Word 的
    CI 上算得出来（DD-17.1 的 integration 层"需模型：否"，同理"需 Office：否"）。

    ``w:tbl`` 用深度优先找（``find_all`` 只看直接子元素，表格在 ``w:body`` 里，
    拿它找会恒返回空集），而行与列用**直接子**找——嵌套表格的行不属于外层表，
    混进来会让"表里套了张表"被数成外层行数变多。
    """
    shapes: list[dict[str, Any]] = []
    for part in pkg.parts(scope="content"):
        for table in iter_elements(part.read_tree, "w:tbl"):
            grid = find_first(table, "w:tblGrid")
            columns = [] if grid is None else find_all(grid, "w:gridCol")
            shapes.append(
                {
                    "rows": len(find_all(table, "w:tr")),
                    "cols": len(columns),
                    "widths_twips": [_int_of(attr(column, "w:w")) for column in columns],
                }
            )
    return shapes


def _pictures(pkg: DocxPackage) -> list[dict[str, Any]]:
    """逐张图的显示尺寸（EMU）。**不含** ``caption``——见模块文档的说明。"""
    return [
        {
            "cx": extent.cx,
            "cy": extent.cy,
            "anchor": extent.anchor,
            "paragraph_index": extent.paragraph_index,
        }
        for extent in read_extents(pkg)
    ]


def _page_breaks(pkg: DocxPackage) -> list[dict[str, Any]]:
    """分页点：位置（body 顶层下标）+ 载体 + 其后第一段非空文本的前缀。

    三样都是**结构量**：下标只由段落顺序决定，文本前缀只由内容决定。带着它们，
    一条"分页点从第 12 段挪到第 13 段"的差异能被人直接读懂；只记个总数则要回去
    翻报告。
    """
    part = pkg.find_part(DOCUMENT_PART)
    if part is None:
        return []
    root = part.read_tree
    body = find_first(root, "w:body")
    if body is None:
        return []
    top = list(body)
    sites: list[dict[str, Any]] = []
    for paragraph, kind, index in break_sites(body):
        sites.append(
            {
                "index": -1 if index is None else index,
                "kind": kind,
                # 不在 body 顶层的分页点（藏在 ``w:sdt`` 里的目录受保护区就是这种）
                # 取它自己的段落文本——那时没有"之后的第一段"可依。
                "before": (
                    _first_text_after(top, index)
                    if index is not None
                    else paragraph_text(paragraph).strip()[:PREFIX_CHARS]
                ),
            }
        )
    return sites


def _first_text_after(top: Sequence[Any], index: int | None) -> str:
    """``top[index]`` 起第一个非空段落的文本前缀；找不到返回空串。

    从分页点**所在**段起找而不是下一段：``pageBreakBefore`` 的分页发生在该段落
    之前，此时"分页之后的第一段"就是它自己。
    """
    if index is None or index < 0:
        return ""
    for element in top[index:]:
        for paragraph in iter_elements(element, "w:p"):
            text = paragraph_text(paragraph).strip()
            if text:
                return text[:PREFIX_CHARS]
    return ""


def _sections(pkg: DocxPackage) -> int:
    """分节数 = 全部 ``w:sectPr``（body 末尾那一个是最后一节，段内的是分节符）。

    ``w:sectPr`` 只有正文部件里有意义：页眉页脚与脚注里也可能出现同名元素，
    但那些不构成"节"。故这里**只数 ``word/document.xml``**，且用深度优先找
    （最后一个 ``sectPr`` 是 ``w:body`` 的直接子元素，段内分节符在 ``w:pPr`` 里）。
    """
    part = pkg.find_part(DOCUMENT_PART)
    if part is None:
        return 0
    return len(list(iter_elements(part.read_tree, "w:sectPr")))


def _int_of(raw: str | None) -> int:
    try:
        return int(raw or 0)
    except ValueError:  # pragma: no cover - 产物由我们自己写，畸形宽度不该出现
        return 0


# ------------------------------------------------------------------ 比对
def diff_snapshots(baseline: Mapping[str, Any], candidate: Mapping[str, Any]) -> list[str]:
    """逐字段比对两份快照，返回人读的差异条目（空列表 = 一致）。

    ``doc_key`` 不参与比对：它是**调用方传进来的**而不是产物里的，两边不同只能说明
    传错了名字，那不叫"结构变了"。静默忽略太隐蔽，故这里显式跳过并说明理由。
    """
    diffs: list[str] = []
    for key in ("paragraph_count", "sections"):
        if baseline.get(key) != candidate.get(key):
            diffs.append(f"{key}: {baseline.get(key)} → {candidate.get(key)}")
    if baseline.get("style_refs") != candidate.get("style_refs"):
        diffs.extend(
            _style_diffs(baseline.get("style_refs") or {}, candidate.get("style_refs") or {})
        )
    for key in ("tables", "pictures", "page_breaks"):
        before, after = baseline.get(key) or [], candidate.get(key) or []
        if before != after:
            diffs.append(f"{key}: {len(before)} 条 → {len(after)} 条")
            diffs.extend(_item_diffs(key, before, after))
    return diffs


def _style_diffs(before: Mapping[str, int], after: Mapping[str, int]) -> list[str]:
    """样式直方图的差异。逐样式列条目——"某个样式从 3 次变 0 次"是关键信号，
    只报"样式集合变了"会把它埋掉。"""
    lines: list[str] = []
    for name in sorted(set(before) | set(after)):
        old, new = before.get(name), after.get(name)
        if old != new:
            lines.append(f"style_refs[{name}]: {old} → {new}")
    return lines


def _item_diffs(key: str, before: Sequence[Any], after: Sequence[Any]) -> list[str]:
    """按最长的公共前缀/后缀剥掉，只报中段——整段挪位时条目会全部错位，
    逐条列出会产出几十行噪声。"""
    head = 0
    while head < min(len(before), len(after)) and before[head] == after[head]:
        head += 1
    tail = 0
    while (
        tail < min(len(before), len(after)) - head
        and before[len(before) - 1 - tail] == after[len(after) - 1 - tail]
    ):
        tail += 1
    dropped = [f"{key}[{head}]: {item}" for item in before[head : len(before) - tail]]
    added = [f"{key}[{head}]: {item}" for item in after[head : len(after) - tail]]
    return [*[f"- {line}" for line in dropped], *[f"+ {line}" for line in added]]


@dataclass(frozen=True)
class SnapshotDiff:
    """``diff_snapshots`` 的带包装形式：跟着产物引用一起走，便于报告引用。"""

    doc_key: str = ""
    lines: list[str] = field(default_factory=list)

    @property
    def equal(self) -> bool:
        return not self.lines

    def summary(self, *, limit: int = 5) -> str:
        if self.equal:
            return f"{self.doc_key or '产物'} 结构与基线一致"
        head = self.lines[:limit]
        more = "" if len(self.lines) <= limit else f"（另有 {len(self.lines) - limit} 条）"
        return f"{self.doc_key or '产物'} 结构与基线有 {len(self.lines)} 处差异：{'；'.join(head)}{more}"
