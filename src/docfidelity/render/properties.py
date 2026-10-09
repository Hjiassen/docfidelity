"""文档属性与文件名（DD-9.1 第 12 步 / NFR-02 确定性）。

这一步只做两件事：把构建元数据写进 ``docProps/core.xml``，以及按确定性规则给出
产物文件名。看起来琐碎，但它是**两条纪律的交汇点**：

* **时间戳的落点**（DD-9.7）：时间戳只允许写进 ``docProps/core.xml``，**不得**参与
  内容结构。故本模块是渲染层里唯一可以写时间的地方，且时间由调用方传入——
  内核不调 ``datetime.now()``（那会让"同输入连续构建三次产物一致"当场失效）。
* **不存在的属性不写**：``None`` 表示"保持模板原值"，不是"清空"。把模板的
  ``dc:creator`` 写成空串看起来无害，实际抹掉了"这份文件是谁做的"这条信息，
  而它在合规评审里是要用的。

缺部件时不伪造
--------------
``docProps/core.xml`` 缺失时记一条 major 并跳过，**不新建**：新建部件还要同时补
``[Content_Types].xml`` 的 Override 与包级关系，少任何一样都会产出"Word 能打开、
属性面板空白"的文件——那种缺陷看起来像"属性没生效"，实际是包的完整性被破坏。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Final

from docfidelity.contracts.ir import ContentIR
from docfidelity.ooxml import (
    Element,
    ensure_child_in_order,
    find_first,
    localname,
    set_text,
)

from .context import TEMPLATE_FIDELITY_RULE_ID, RenderContext

__all__ = [
    "CORE_PART",
    "PROPERTIES_RULE_ID",
    "PropertiesPolicy",
    "PropertiesReport",
    "apply_properties",
    "output_filename",
    "sanitize_filename",
]

#: 核心属性部件路径。
CORE_PART: Final = "docProps/core.xml"

#: 属性写入失败 / 部件缺失所用规则编号（属性属模板保真范畴）。
PROPERTIES_RULE_ID: Final = TEMPLATE_FIDELITY_RULE_ID

#: ``docProps/core.xml`` 子元素的常规顺序（Word 的写出顺序）。局部名即可比较：
#: ``dc:title`` / ``dcterms:created`` / ``cp:lastModifiedBy`` 的局部名各不相同，
#: 不会互相顶替。
CORE_PROPERTIES_ORDER: Final[tuple[str, ...]] = (
    "title",
    "subject",
    "creator",
    "keywords",
    "description",
    "lastModifiedBy",
    "revision",
    "created",
    "modified",
    "category",
    "contentStatus",
)

#: ``dcterms`` 日期属性必须带 ``xsi:type="dcterms:W3CDTF"``，否则 Word 不认。
_XSI_TYPE: Final = "{http://www.w3.org/2001/XMLSchema-instance}type"
_W3CDTF: Final = "dcterms:W3CDTF"

#: 文件名里需要替换掉的字符（Windows / POSIX 取并集，外加控制字符区间）。
_UNSAFE_FILENAME_CHARS: Final = '<>:"/\\|?*'


@dataclass(frozen=True)
class PropertiesPolicy:
    """属性策略。所有字段为 ``None`` 即"保持模板原值"。

    ``created`` / ``modified`` 必须由调用方传 **ISO 8601 字符串**：渲染层不产生
    时间——一次构建里出现两个不同的"当前时间"是确定性缺陷的经典来源，
    而把它收在参数上，调用方（pipeline）就能在续跑时复用同一份时间戳。
    """

    title: str | None = None
    creator: str | None = None
    created: str | None = None
    modified: str | None = None
    filename: str | None = None
    suffix: str = ".docx"


@dataclass
class PropertiesReport:
    """属性写入结果。"""

    written: list[str] = field(default_factory=list)
    filename: str = ""
    touched_parts: list[str] = field(default_factory=list)
    skipped_missing_part: bool = False

    def as_payload(self) -> dict[str, Any]:
        return {
            "written": list(self.written),
            "filename": self.filename,
            "touched_parts": list(self.touched_parts),
            "skipped_missing_part": self.skipped_missing_part,
        }


def apply_properties(ctx: RenderContext, *, policy: PropertiesPolicy) -> PropertiesReport:
    """写文档属性并给出产物文件名。"""
    report = PropertiesReport(filename=output_filename(ctx.ir, policy=policy))
    part = ctx.pkg.find_part(CORE_PART)
    if part is None:
        report.skipped_missing_part = True
        ctx.note_once(
            PROPERTIES_RULE_ID,
            "major",
            "core-properties-missing",
            f"模板包内没有 {CORE_PART}，文档属性未写入（不伪造该部件）",
            part=CORE_PART,
            suggestion="重新入库该模板；确需属性，请在模板原件中保留核心属性部件",
        )
        return report

    root = part.tree
    _write(root, "dc:title", policy.title, report)
    _write(root, "dc:creator", policy.creator, report)
    _write(root, "cp:lastModifiedBy", policy.creator, report)
    _write(root, "dcterms:created", policy.created, report, dated=True)
    _write(root, "dcterms:modified", policy.modified, report, dated=True)

    if report.written:
        part.mark_dirty()
        report.touched_parts = [CORE_PART]
    return report


def _write(
    root: Element,
    tag: str,
    value: str | None,
    report: PropertiesReport,
    *,
    dated: bool = False,
) -> None:
    """写一个核心属性。``value is None`` 即保持原值（一次都不碰）。"""
    if value is None:
        return
    element = find_first(root, tag)
    if element is None:
        element = ensure_child_in_order(root, tag, CORE_PROPERTIES_ORDER)
    set_text(element, value)
    if dated:
        element.set(_XSI_TYPE, _W3CDTF)
    report.written.append(localname(element))


def output_filename(ir: ContentIR, *, policy: PropertiesPolicy | None = None) -> str:
    """产物文件名：显式声明优先，否则按 ``项目-文档键`` 确定性派生。

    派生规则**只用 IR 里的字段**（``project_id`` / ``doc_key``）：这两者在续跑时
    必然相同，故同一输入永远得到同一个文件名。带模板版本会让"换模板版本 → 文件名
    变了 → 产物目录里新旧并存"，反而更难判断哪份是本次的。
    """
    settings = policy or PropertiesPolicy()
    if settings.filename:
        return sanitize_filename(settings.filename)
    stem = "-".join(item for item in (ir.project_id, ir.doc_key) if item)
    return sanitize_filename(stem or "document") + settings.suffix


def sanitize_filename(name: str) -> str:
    """把文件名里的非法字符换成下划线，并收敛首尾空白与点。

    不处理路径分隔符以外的任何东西：**重命名**是调用方的事，本函数只保证
    产出的名字在任何文件系统上都写得下去。
    """
    cleaned = "".join(
        "_" if char in _UNSAFE_FILENAME_CHARS or ord(char) < 32 else char for char in name
    )
    return cleaned.strip().strip(".") or "document"
