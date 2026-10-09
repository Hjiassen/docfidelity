"""术语变量与连读重复（DD-8.2 / FR-M3-09 / FR-M5-08）。

术语替换**只在这一处实现**（DD-8.2 明确要求）：章节标题、正文段落、表格单元格、
图表标题，一律经 :func:`glossary_apply`。需求 1.2 第 3 条说的正是这份代价——
同一份术语表被两处代码各解释一次，两处迟早漂移，而漂移的表现是"文档里同一个软件名
有两种写法"：没有任何机械门禁能判它错，只能靠人眼逐页看。

三类处理
--------
1. **变量替换**：``{{软件名}}`` → 术语表里的规范写法。
2. **旧称归一**：术语声明的 ``forbidden`` 写法一律改写成 ``canonical_form``。
   与替换**合并在一处实现**的理由：两者若各写一份，"统一"与"禁用校验"就会对
   同一段文本给出两个结论。
3. **连读重复修补**（:func:`fix_concat_duplication`）：替换后若规范写法的**词尾**
   与紧随文本的**词头**重合，合并之。例：术语「XXX分析仪」插到原文「分析仪整体软件」
   之前，拼出来成了「XXX分析仪分析仪整体软件」。

``forbidden`` 与变量名共用同一份条目表，因此 ``{{旧称}}`` 一样能解析——术语表是
唯一来源，界面上不出现第二套叫法（需求 8.3）。
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Final

from docfidelity.contracts.config import Terminology, TerminologyEntry
from docfidelity.contracts.ir import ContentIR, ContentUnit

__all__ = [
    "GLOSSARY_RULE_ID",
    "TERMINOLOGY_RULE_ID",
    "VARIABLE_PATTERN",
    "apply_glossary_to_ir",
    "apply_variables",
    "fix_concat_duplication",
    "glossary_apply",
    "unresolved_variables",
]

#: 术语能力的规则编号（FR-M3-09：术语表与变量）。
GLOSSARY_RULE_ID: Final = "FR-M3-09"

#: 术语一致门禁的规则编号（FR-M5-08）。门禁与本模块**共用同一实现**（DD-11 附录 C）。
TERMINOLOGY_RULE_ID: Final = "FR-M5-08"

#: 变量占位符。允许内部空白：``{{ 软件名 }}`` 与 ``{{软件名}}`` 等价。
VARIABLE_PATTERN: Final = re.compile(r"\{\{\s*([^{}]+?)\s*\}\}")

#: 连读重复的最小重合长度。取 2 而非 1：单字重合是正常中文（「分析仪」+「仪器」），
#: 合并掉就成了「分析仪器」，把两个不同的词粘成了一个。
_MIN_OVERLAP: Final = 2

#: 修补的最大轮次。每轮文本严格变短，故必然收敛；上限只是防御性护栏。
_MAX_PASSES: Final = 8

#: 需要套用术语的可读文本字段（``ContentUnit.fields`` 内）。
_TEXT_LIST_FIELDS: Final = ("items",)
_TEXT_FIELDS: Final = ("caption",)
_TABLE_COLUMN_FIELD: Final = "columns"
_TABLE_ROW_FIELD: Final = "rows"


# -- 生效条目 -------------------------------------------------------------


def _entries(terms: Terminology) -> list[TerminologyEntry]:
    """生效的术语条目。``scope == "none"`` 表示本档案不套用术语表。"""
    if terms.scope == "none":
        return []
    return list(terms.entries)


def _canonical_forms(terms: Terminology) -> list[str]:
    """全部规范写法，**长词在前**。

    长词在前是为了让连带重复的合并可预期：「XXX分析仪」与「分析仪」同时存在时，
    先处理长词才不会把长词切成两段。
    """
    forms = {entry.canonical_form for entry in _entries(terms) if entry.canonical_form}
    return sorted(forms, key=lambda form: (-len(form), form))


# -- 变量替换 -------------------------------------------------------------


def _resolve_variable(name: str, terms: Terminology) -> str:
    """把变量名解析为规范写法；解析不到时**原样保留**。

    原样保留而不是替换成空串：留着的 ``{{软件名}}`` 会在 :func:`unresolved_variables`
    里被查出来并记一条 Finding；换成空串则变成正文里凭空少一个词，谁也不会发现。
    """
    for entry in _entries(terms):
        if name == entry.term or name == entry.canonical:
            return entry.canonical_form
    return "{{" + name + "}}"


def apply_variables(text: str, terms: Terminology) -> str:
    """旧称归一 + ``{{变量}}`` 替换。

    ``forbidden`` 先于变量替换执行：旧称在原文里可能是紧邻 ``{{变量}}`` 的裸文本，
    先归一才能让随后的连读重复检查看到真实的拼接结果。
    """
    result = text
    for entry in _entries(terms):
        for bad in entry.forbidden:
            if bad:
                result = result.replace(bad, entry.canonical_form)
    return VARIABLE_PATTERN.sub(lambda match: _resolve_variable(match.group(1), terms), result)


def unresolved_variables(text: str, terms: Terminology) -> list[str]:
    """文本里**未能解析**的变量名（去重后按字典序）。"""
    names = {match.group(1) for match in VARIABLE_PATTERN.finditer(apply_variables(text, terms))}
    return sorted(names)


# -- 连读重复 -------------------------------------------------------------


def fix_concat_duplication(text: str, terms: Terminology) -> str:
    """合并"规范写法词尾"与"后文词头"的重合。"""
    forms = _canonical_forms(terms)
    if not forms or not text:
        return text
    result = text
    for _ in range(_MAX_PASSES):
        collapsed = _collapse_once(result, forms)
        if collapsed == result:
            break
        result = collapsed
    return result


def _collapse_once(text: str, forms: list[str]) -> str:
    for form in forms:
        cursor = 0
        while (found := text.find(form, cursor)) >= 0:
            tail = text[found + len(form) :]
            overlap = _overlap(form, tail)
            if overlap:
                text = text[: found + len(form)] + tail[overlap:]
            cursor = found + len(form)
    return text


def _overlap(form: str, tail: str) -> int:
    """``form`` 的词尾与 ``tail`` 的词头的最长重合长度（不足 2 返回 0）。"""
    for size in range(min(len(form), len(tail)), _MIN_OVERLAP - 1, -1):
        if form[-size:] == tail[:size]:
            return size
    return 0


# -- 统一入口 -------------------------------------------------------------


def glossary_apply(text: str, terms: Terminology) -> str:
    """文本 → 术语处理后的文本。装配期**唯一**的术语入口（DD-8.2）。"""
    if not text:
        return text
    return fix_concat_duplication(apply_variables(text, terms), terms)


def apply_glossary_to_ir(ir: ContentIR, terms: Terminology) -> None:
    """对整棵 IR 套用术语（原地修改）。

    覆盖"人眼会读到的"全部文本：``text``、要点列表、表格列头与单元格、图表标题。
    ``trace`` / ``origin`` **不套用**——它们是引用句柄（``kind:key``），改动会让
    追溯链指向不存在的对象。
    """
    if not _entries(terms):
        return
    for unit in ir.walk():
        _apply_to_unit(unit, terms)


def _apply_to_unit(unit: ContentUnit, terms: Terminology) -> None:
    if unit.text:
        unit.text = glossary_apply(unit.text, terms)
    fields = unit.fields
    for key in _TEXT_FIELDS:
        value = fields.get(key)
        if isinstance(value, str) and value:
            fields[key] = glossary_apply(value, terms)
    for key in _TEXT_LIST_FIELDS:
        value = fields.get(key)
        if isinstance(value, list):
            fields[key] = [
                glossary_apply(item, terms) if isinstance(item, str) else item for item in value
            ]
    columns = fields.get(_TABLE_COLUMN_FIELD)
    if isinstance(columns, list):
        fields[_TABLE_COLUMN_FIELD] = [
            glossary_apply(item, terms) if isinstance(item, str) else item for item in columns
        ]
    rows = fields.get(_TABLE_ROW_FIELD)
    if isinstance(rows, list):
        fields[_TABLE_ROW_FIELD] = [_glossary_row(row, terms) for row in rows]


def _glossary_row(row: object, terms: Terminology) -> object:
    if not isinstance(row, list):
        return row
    return [glossary_apply(item, terms) if isinstance(item, str) else item for item in row]


def iter_texts(ir: ContentIR) -> Iterator[str]:
    """IR 中全部可读文本（供门禁与调试使用，不参与装配逻辑）。"""
    for unit in ir.walk():
        if unit.text:
            yield unit.text
        for key in (*_TEXT_FIELDS, *_TEXT_LIST_FIELDS):
            value = unit.fields.get(key)
            if isinstance(value, str):
                yield value
            elif isinstance(value, list):
                yield from (item for item in value if isinstance(item, str))
        columns = unit.fields.get(_TABLE_COLUMN_FIELD)
        if isinstance(columns, list):
            yield from (item for item in columns if isinstance(item, str))
        rows = unit.fields.get(_TABLE_ROW_FIELD)
        if isinstance(rows, list):
            for row in rows:
                if isinstance(row, list):
                    yield from (item for item in row if isinstance(item, str))
