"""门禁规则实现的共享工具（``qa.rules`` 内部）。

这里只放"多条规则都要用"的东西：定位、取段落 / 表格、复用 ``render`` 的共享校验器、
以及构造 ``Finding`` 的统一写法。规则**自己的判据**一律留在各自的 ``checks_*.py``——
判据放错地方比代码重复更难查：读规则时看不见它，改规则时改不到它。
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from typing import Any

from docfidelity.contracts.errors import EngineError
from docfidelity.contracts.ir import ContentIR, ContentUnit
from docfidelity.contracts.qa import Finding, Location, RuleSeverity
from docfidelity.ooxml import (
    DOCUMENT_PART,
    DocxPackage,
    Element,
    iter_elements,
    paragraph_text,
)

from ..engine import QaContext, RuleSpec

__all__ = [
    "content_units",
    "drain",
    "drain_rule",
    "finding",
    "for_each_paragraph",
    "heading_units",
    "locate",
    "paragraph_texts",
    "render_view",
    "tables_of",
    "tables_of_part",
    "text_of_units",
]


def finding(
    spec: RuleSpec,
    severity: RuleSeverity,
    message: str,
    *,
    part: str | None = None,
    xpath: str | None = None,
    unit_id: str | None = None,
    chapter_path: Sequence[str] = (),
    evidence: str | None = None,
    suggestion: str | None = None,
    page_hint: int | None = None,
) -> Finding:
    """构造一条判定，并把规则的静态信息（编号 / 级别）带进去。"""
    return Finding(
        rule_id=spec.rule_id,
        severity=severity,
        message=message,
        location=Location(
            unit_id=unit_id,
            chapter_path=list(chapter_path),
            part=part,
            xpath=xpath,
            page_hint=page_hint,
        ),
        evidence=evidence,
        suggestion=suggestion,
    )


def locate(element: Element) -> str | None:
    """元素的绝对 XPath（诊断用；游离元素返回 ``None``）。"""
    try:
        return element.getroottree().getpath(element)
    except AttributeError:
        return None


def for_each_paragraph(pkg: DocxPackage, part: str = DOCUMENT_PART) -> Iterator[Element]:
    """逐段落遍历某个部件（按文档序，**含表格单元格里的段落**）。

    必须用**后代**遍历而不是 ``find_all``：``find_all`` 只看直接子元素，而
    ``part.read_tree`` 的根是 ``w:document``、段落挂在 ``w:body`` 底下——写成
    ``find_all(tree, "w:p")`` 会恒返回空列表。这种"检查器恒返回空"的形态与
    "检查器没写"在报告上完全一样（都是零发现），故这里钉住正确的取法。
    """
    found = pkg.find_part(part)
    if found is None:
        return
    yield from iter_elements(found.read_tree, "w:p")


def paragraph_texts(pkg: DocxPackage, part: str = DOCUMENT_PART) -> list[str]:
    """部件的全部段落文本（非空者，按文档序）。"""
    return [text for text in (paragraph_text(p) for p in for_each_paragraph(pkg, part)) if text]


def tables_of_part(pkg: DocxPackage, part: str = DOCUMENT_PART) -> list[Element]:
    found = pkg.find_part(part)
    if found is None:
        return []
    return list(iter_elements(found.read_tree, "w:tbl"))


def tables_of(pkg: DocxPackage) -> list[tuple[str, Element]]:
    """全包内容部件里的表格：``[(部件名, 表元素), ...]``。

    按部件名升序、部件内按文档序——顺序稳定，报告里的条目才可复现（NFR-02）。
    取表用**后代**遍历：``w:tbl`` 挂在 ``w:body`` 底下而不是 ``w:document`` 下，
    用 ``find_all`` 会恒返回空，"表格越界""表头规范"两条规则于是永远"通过"。
    """
    collected: list[tuple[str, Element]] = []
    for part in pkg.parts(scope="content"):
        for table in iter_elements(part.read_tree, "w:tbl"):
            collected.append((part.path, table))
    return collected


def content_units(ir: ContentIR, *, kinds: Sequence[str] | None = None) -> list[ContentUnit]:
    """IR 单元（可按 ``type`` 过滤，保文档序）。

    ``kinds`` 给 ``None`` 表示不过滤。过滤用**集合成员判断**而不是 ``==`` 链：
    后者会被类型无关检索判成"针对具体 kind 的分支"。
    """
    if kinds is None:
        return list(ir.walk())
    wanted = set(kinds)
    return [unit for unit in ir.walk() if unit.type in wanted]


def heading_units(ir: ContentIR) -> list[ContentUnit]:
    return content_units(ir, kinds=("heading",))


def text_of_units(units: Sequence[ContentUnit]) -> list[str]:
    """单元的可读文本（标题 / 正文；表格与图片单元不计入）。"""
    return [unit.text.strip() for unit in units if unit.text and unit.text.strip()]


def render_view(context: QaContext) -> Any | None:
    """构造（并缓存）渲染视图，用于复用 ``render`` 的共享校验器。

    为什么要复用而不是在 ``qa`` 里另写一遍：``whitelist_targets`` / 分页白名单 /
    占位残留这三样各有其"定位受保护区"的逻辑，``qa`` 再写一份必然会与 ``render``
    漂移，而漂移的表现是"门禁报的分页点和产物里的分页点不是同一处"。

    ``profile`` 缺失时返回 ``None``（能力门禁会据此跳过依赖它的规则）——档案例外时
    「静默给出一个结论」比「明说判不了」危险得多。
    """
    if context.profile is None:
        return None
    cached = getattr(context, "_render_view_cache", None)
    if cached is not None:
        return cached

    from docfidelity.render import RenderContext, locate_regions

    try:
        view = RenderContext(
            pkg=context.pkg,
            template=context.template,
            profile=context.profile,
            ir=context.ir,
            document_part=context.document_part,
            protected_blocks=context.template.blocks.blocks,
        )
        # 受保护区必须先登记：``whitelist_targets`` / 占位残留都按它判定。
        locate_regions(view)
    except EngineError:
        return None
    # 视图自己的判定由规则从 ``view.findings`` 取用，不并入门禁判定。
    object.__setattr__(context, "_render_view_cache", view)
    return view


def drain(view: Any) -> list[Finding]:
    """取出渲染视图里累积的判定（复用共享校验器的产物）。"""
    return list(getattr(view, "findings", []))


def drain_rule(view: Any, rule_id: str) -> list[Finding]:
    """取出渲染视图里**属于某条规则**的判定。

    按 ``rule_id`` 过滤是必须的：视图是三条规则共用的，直接取全部会把 FR-M5-02
    的判定记到 FR-M5-04 名下——报告上表现为"分页合规报了样式问题"。
    """
    return [item for item in drain(view) if item.rule_id == rule_id]
