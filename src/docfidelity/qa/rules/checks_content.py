"""内容与追溯类规则：FR-M5-08 / 09 / 10 / 12 / 17。

这五条的共同点：它们的判据**不在版面上**，而在"说得通吗"这一层——术语是不是同一个
写法、该有的内容有没有落位、每条内容追不追得到来源、人眼有没有看过、引擎有没有偷偷
长出文档类型的概念。

复用纪律
--------
术语一致（08）复用 ``compose.glossary``，槽位未填（09）复用 ``compose.slots``，
占位残留（09）复用 ``render.placeholder_residue_in_package``，类型无关（17）复用
``qa.type_agnostic``。这几样在各自模块里都有"唯一实现"的明确声明，门禁再写一份
等于把那条声明作废——而作废的表现是"装配期说统一了、门禁说没统一"。

（09 的占位残留刻意**不用** ``render.placeholder_residue``：那条判据依赖渲染期
"改动前的落点"，门禁手里只有产物，重建出的落点已经指向别的段落。详见
``_placeholder_residue``。）
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

from docfidelity.contracts.config import Terminology
from docfidelity.contracts.qa import Finding

from ..engine import QaContext, RuleSpec, register_check
from ..type_agnostic import scan_ir, scan_sources
from ._support import finding, paragraph_texts

__all__ = [
    "check_completeness",
    "check_spotcheck",
    "check_terminology",
    "check_traceability",
    "check_type_agnostic",
]

#: 单条判定里最多列出的条目数。
_MAX_LISTED: Final = 10

#: 「待确认项」的展示口径（``ContentIR.unresolved`` 的条目）。
_PENDING_NOTE: Final = "待确认项（已进入裁决队列，不阻断构建）"


# ------------------------------------------------------------------ FR-M5-08
def check_terminology(spec: RuleSpec, context: QaContext) -> list[Finding]:
    """FR-M5-08：禁用词 / 旧称命中数 = 0；变量替换后无连读重复。

    检查**两处**文本：内容 IR（装配后的规范文本）与产物段落（真正交付出去的文字）。
    只查 IR 会漏掉"渲染期又拼回去了一次"；只查产物会漏掉"IR 里就没归一，只是恰好
    渲染时被别的替换盖住"。两处都用 ``compose.glossary`` 的同一组函数判定。
    """
    from docfidelity.compose.glossary import (
        fix_concat_duplication,
        iter_texts,
        unresolved_variables,
    )

    terms = context.terminology
    if terms is None:
        return []
    texts = sorted({*iter_texts(context.ir), *paragraph_texts(context.pkg, context.document_part)})
    texts = [text for text in texts if text.strip()]

    stale: list[str] = []
    for text in texts:
        for entry in terms.entries:
            for word in entry.forbidden:
                if word and word in text:
                    stale.append(f"{word} → {entry.canonical_form}")

    unresolved: set[str] = set()
    repeated: list[str] = []
    for text in texts:
        unresolved.update(unresolved_variables(text, terms))
        if fix_concat_duplication(text, terms) != text:
            repeated.append(text)

    findings: list[Finding] = []
    if stale:
        findings.append(
            finding(
                spec,
                "blocker",
                f"术语旧称 / 禁用写法命中 {len(stale)} 处",
                part=context.document_part,
                evidence=", ".join(sorted(set(stale))[:_MAX_LISTED]),
                suggestion="把命中处改为术语表声明的规范写法，或把该写法加入术语表的 forbidden",
            )
        )
    if repeated:
        findings.append(
            finding(
                spec,
                "blocker",
                f"变量替换后存在连读重复 {len(repeated)} 处",
                part=context.document_part,
                evidence=" | ".join(text[:120] for text in repeated[:_MAX_LISTED]),
                suggestion="检查术语规范写法的词尾是否与紧随文本的词头重合（DD-8.2）",
            )
        )
    if unresolved:
        findings.append(
            finding(
                spec,
                "major",
                f"存在 {len(unresolved)} 个未能解析的术语变量",
                evidence=", ".join(sorted(unresolved)[:_MAX_LISTED]),
                suggestion="在术语表里补上这些变量，或修正正文里的变量名",
            )
        )
    return findings


# ------------------------------------------------------------------ FR-M5-09
def check_completeness(spec: RuleSpec, context: QaContext) -> list[Finding]:
    """FR-M5-09：槽位未填数、占位符残留数、待确认项统计。

    产出的是**统计型判定**（每类一条，带计数与样例），而不是逐条详列：
    "哪些槽位没填"由 ``compose.slots`` 在装配期逐条给出，门禁在这里再列一遍
    只会让同一件事在报告里出现两次。
    """
    findings: list[Finding] = []

    residue = _placeholder_residue(context)
    if residue:
        findings.append(
            finding(
                spec,
                "major",
                f"产物中仍有 {len(residue)} 处模板占位文本残留",
                part=context.document_part,
                evidence=", ".join(residue[:_MAX_LISTED]),
                suggestion="确认这些占位所在版块已登记为受保护区并完成清理",
            )
        )

    slot_findings = _slot_findings(context)
    if slot_findings:
        findings.append(
            finding(
                spec,
                "major",
                f"{len(slot_findings)} 个骨架槽位未按体例落位",
                part=context.document_part,
                evidence=" | ".join(item.message for item in slot_findings[:_MAX_LISTED]),
                suggestion="检查投影规则是否为该槽位产出了内容，或该槽位是否应声明为可选",
            )
        )

    if context.ir.unresolved:
        findings.append(
            finding(
                spec,
                "minor",
                f"{len(context.ir.unresolved)} 项{_PENDING_NOTE}",
                evidence=", ".join(sorted(context.ir.unresolved)[:_MAX_LISTED]),
                suggestion="在裁决队列里逐项确认：接受 / 改配置 / 豁免",
            )
        )
    return findings


def _placeholder_residue(context: QaContext) -> list[str]:
    """占位残留：走**产物侧**的文本探针，不走渲染上下文。

    为什么不能复用 ``render.placeholder_residue``
    --------------------------------------------
    那条判据落在定位上，而它的正确性依赖"改动**之前**算好的落点"
    （``ctx.placeholder_sites``）——门禁手里只有产物，重建上下文后位置型定位符
    （``(//w:p)[n]``）已指向别的段落，判出来的是"那一格里有字"。实测形态：产物里
    一个占位字都没有，门禁却报"仍有 3 处占位残留"（那 3 格分别落到了版本号、修订
    表和表头单元格上）。``render.placeholder_residue`` 的文档把"重新解析定位符"
    列为已知错误前提，正是这个形态。

    故这里改问一个在产物上**成立**的问题：这段占位原文还印在产物里吗
    （``render.placeholder_residue_in_package``）。
    """
    from docfidelity.render import placeholder_residue_in_package

    return list(placeholder_residue_in_package(context.pkg, context.template.placeholders.entries))


def _slot_findings(context: QaContext) -> list[Finding]:
    """复用 ``compose.slots`` 的"必填槽位为空"判据，落位情况由**产物侧**重新推导。

    判据只能做到**章节**粒度，这是产物决定的
    ----------------------------------------
    ``ContentUnit`` 不带"我落在哪个槽位"的字段（槽位归属是装配期的 ``ProjectionSpec.slot``
    声明，落位后合并进章节标题的 ``children``）。所以从产物反推只能得到"这一章有没有
    内容"，得不到"这一章的哪个槽位空了"。

    据此把判据收窄：**声明了必填槽位、而产物里该章节没有任何内容的章节**才参与校验。
    有内容的章节一律不参检——否则会退化成"每个多槽位章节都至少误报一条"。同一个
    ``slot_id`` 在骨架里重复声明这类档案缺陷与内容无关，由装配期的
    ``compose.builder`` 用**带槽位归属的**完整信息判定（那里的结论会经 ``upstream``
    并入报告），门禁不在这里再判一遍。

    这不是"漏检"而是"不误检"：产物侧判不了的就不给结论，编一个结论比空着更危险。

    骨架标题要先过术语替换再比对
    ---------------------------
    骨架标题允许写变量（``"{{系统名}}适用范围"``），而 IR 里的标题文本已经过
    ``compose.glossary.glossary_apply``（术语的唯一入口）变成了 ``"示例分析仪适用范围"``。
    两边直接比字符串，等于把每一个"标题带变量"的章节都判成"必填槽位未填充"——
    报告上写着"章节「{{系统名}}适用范围」的必填槽位未填"，而产物里那一章明明有内容。
    故比对前把骨架标题过同一遍术语替换（``_title_keys``）。
    """
    if context.profile is None:
        return []
    from docfidelity.compose.slots import SlotPolicy, check_slots

    skeleton = context.profile.genre.chapter_skeleton
    if not skeleton:
        return []
    headings = {
        (unit.text or "").strip(): unit
        for unit in context.ir.walk()
        if unit.type == "heading" and (unit.text or "").strip()
    }
    unfilled = [
        chapter
        for chapter in skeleton
        if chapter.slots and not _chapter_children(headings, context.terminology, chapter.title)
    ]
    if not unfilled:
        return []
    # ``placed`` 传空映射：先把"章节无内容"这个已知条件落实在骨架筛选上，
    # 再让 ``check_slots`` 只跑"必填为空"那一支（类型越界分支按槽位归属判，产物给不出）。
    return list(check_slots(unfilled, {}, policy=SlotPolicy()))


def _chapter_children(
    headings: Mapping[str, Any], terms: Terminology | None, title: str
) -> list[Any]:
    for key in _title_keys(terms, title):
        heading = headings.get(key)
        if heading is not None:
            return list(heading.children)
    return []


def _title_keys(terms: Terminology | None, title: str) -> tuple[str, ...]:
    """骨架标题在产物侧的候选写法：**原文 + 术语替换后**（两者都试）。"""
    stripped = title.strip()
    if terms is None:
        return (stripped,)
    from docfidelity.compose.glossary import glossary_apply

    resolved = glossary_apply(stripped, terms).strip()
    return (stripped,) if resolved == stripped else (stripped, resolved)


# ------------------------------------------------------------------ FR-M5-10
def check_traceability(spec: RuleSpec, context: QaContext) -> list[Finding]:
    """FR-M5-10：每个可追溯对象都有落位；未落位项列出。

    ``orphans`` 与 ``unused`` 都是**信息**而非错误（契约 :class:`TraceMatrix` 原文），
    故这里不把它们判成 blocker：把"这一章本来就没来源"升级成阻断，会让人为了过门禁
    而给内容编一个来源。判成 ``major`` + 列出条目，让人去判断。
    """
    traces = context.traces
    if traces is None:
        return []
    orphans = sorted(traces.orphans)
    unused = sorted(traces.unused)
    if not orphans and not unused:
        return []
    return [
        finding(
            spec,
            "major",
            f"追溯矩阵不完整：未落位 {len(orphans)} 项 / 未使用领域对象 {len(unused)} 项",
            part=context.document_part,
            evidence=(
                f"未落位: {', '.join(orphans[:_MAX_LISTED])}"
                if orphans
                else f"未使用: {', '.join(unused[:_MAX_LISTED])}"
            ),
            suggestion="逐个确认：补充投影规则让对象落位，或确认该项确实不需要进入文档",
        )
    ]


# ------------------------------------------------------------------ FR-M5-12
def check_spotcheck(spec: RuleSpec, context: QaContext) -> list[Finding]:
    """FR-M5-12：渲染目检（docx → PDF → PNG，按关键词定位页）。

    降级必须**写进报告**（DD-10.5）：渲染服务不可用时，报告要说明"没有做目检，
    原因是 X"，而不是安静地返回空——"没检查"与"检查通过"必须在报告里可区分
    （与契约 ``Report`` 对"门禁未执行 vs 门禁全通过"的同一条纪律）。
    """
    result: Any = context.spotcheck
    if result is None:
        return []
    keywords = _keywords(spec)
    if getattr(result, "degraded", False):
        reason = getattr(result, "reason", None) or "渲染服务不可用"
        return [
            finding(
                spec,
                "major",
                "渲染目检未能执行（已降级）：未做页级目检",
                evidence=f"原因：{reason}",
                suggestion="安装 Office 或 LibreOffice / PyMuPDF 后重跑目检；本次结论不含目检",
            )
        ]
    pages = list(getattr(result, "pages", []) or [])
    if not pages:
        return [
            finding(
                spec,
                "minor",
                "渲染目检已执行，但按关键词未定位到任何页面",
                evidence=f"关键词：{', '.join(keywords) or '<未声明>'}",
                suggestion="确认关键词是否出现在文档中，或在档案里补充目检关键词",
            )
        ]
    return []


def _keywords(spec: RuleSpec) -> list[str]:
    raw = spec.params.get("keywords", [])
    if not isinstance(raw, list):
        return []
    return [str(item) for item in raw if str(item).strip()]


# ------------------------------------------------------------------ FR-M5-17
def check_type_agnostic(spec: RuleSpec, context: QaContext) -> list[Finding]:
    """FR-M5-17：以禁用词表检索引擎源码与内容 IR 输出，命中即失败。

    源码一侧默认**关**（``params.roots`` 为空即跳过）：CI 已经每次提交都扫一遍源码，
    让每一次构建再扫一遍整个内核只会拖慢构建而不增加发现力。IR 一侧永远开——
    它是**本次构建的产物**，只在构建期才存在。
    """
    hits = scan_ir(context.ir)
    roots = _roots(spec)
    if roots:
        hits.extend(scan_sources(roots))
    if not hits:
        return []
    return [
        finding(
            spec,
            "blocker",
            f"类型无关检索命中 {len(hits)} 处（引擎代码 / IR 输出出现文档类型名或领域概念名）",
            part=context.document_part if any(hit.path == "<ir>" for hit in hits) else None,
            evidence=" | ".join(hit.as_text() for hit in hits[:_MAX_LISTED]),
            suggestion="把文档类型 / 领域概念移出引擎代码与 IR，改由外部档案声明",
        )
    ]


def _roots(spec: RuleSpec) -> list[str]:
    raw = spec.params.get("roots", [])
    if not isinstance(raw, list):
        return []
    return [str(item) for item in raw if str(item).strip()]


register_check("terminology", check_terminology)
register_check("completeness", check_completeness)
register_check("traceability", check_traceability)
register_check("spotcheck", check_spotcheck)
register_check("type_agnostic", check_type_agnostic)
