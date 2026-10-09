"""三条 IR 硬约束自检（DD-3.2（2） / NFR-28 / 验收 34 / 附录 D「IR 纯净」）。

IR 是 ``compose`` 与 ``render`` 之间**唯一**的接口。它一旦带上样式痕迹或类型痕迹，
"样式层零改动"（模板保真）与"与文档类型无关"（决策六）两条承诺就同时失效——而且
失效方式是**静默**的：渲染层照常工作，只是成品开始出现"表头掉色"或"换个文档类型
就生成不出来"这类无法归因的现象。

三条硬约束对应的检查
--------------------
=========================  ==================================================================
无样式痕迹                 ``contracts.ir.FORBIDDEN_IR_TOKENS``（``w:`` / ``twips`` /
                           ``styleId`` / ``pStyle`` / ``tblStyle``）不得出现在 IR 里
无领域概念标签             引擎自有的那层表面（``type`` / 字段名 / ``unit_id``）不得出现
无文档类型名               外部词表里的类型名与领域概念名
=========================  ==================================================================

后两条为什么只查**引擎表面**而不查全部文本
------------------------------------------
"IR 不含领域概念名"约束的是**引擎的词汇**，不是使用者的数据。事实源里写着一个
文档类型名（例如某份清单里的一行标题）并不违反任何承诺，把它判成 blocker 会让
合法事实无法接入。因此:

* **引擎表面** = 单元键名、``unit_id`` / ``type`` / ``level`` / ``fields`` 的**键**；
* 事实数据的**值**交给 ``qa.type_agnostic``（P0-12 类型无关检索器）按词表复核，
  那一层有完整词表，并且知道哪些命中属于数据。

样式痕迹不适用"只看表面"的放松：``styleId`` 这类 token 只会来自样式层，出现在任何
位置都说明有东西把 OOXML 词汇漏进了 IR，故对**整份序列化**做检查。

``trace`` 与 ``origin`` 是**白名单**：它们存 ``kind:key`` 引用句柄（DD-3.2（2）的
已知边界，见 TASK-P0-08 描述）。剔除它们之后，IR 的其余部分必须干净。
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any, Final

from docfidelity.contracts.errors import EngineError, ErrorCode
from docfidelity.contracts.ir import FORBIDDEN_IR_TOKENS, ContentIR

__all__ = [
    "PURITY_RULE_ID",
    "assert_ir_clean",
    "check_ir_purity",
    "ir_payload",
    "ir_surface",
]

#: IR 硬约束自检的规则编号（附录 C：FR-M5-17 类型无关 | integrity | blocker）。
PURITY_RULE_ID: Final = "FR-M5-17"

#: 引用句柄字段：存 ``kind:key`` 或源定位，**不作为裸词汇参与检索**。
_REFERENCE_FIELDS: Final = ("trace", "origin")

#: 引擎表面里保留的结构字段（值参与检索）。
_STRUCTURAL_FIELDS: Final = ("unit_id", "type", "level")


def ir_payload(ir: ContentIR) -> dict[str, Any]:
    """IR 的可检索序列化（剔除 ``trace`` / ``origin`` 引用句柄）。"""
    payload = ir.model_dump(mode="json")
    _strip(payload)
    return payload


def _strip(node: Any) -> None:
    if isinstance(node, dict):
        for key in _REFERENCE_FIELDS:
            node.pop(key, None)
        for value in node.values():
            _strip(value)
    elif isinstance(node, list):
        for item in node:
            _strip(item)


def ir_surface(ir: ContentIR) -> list[str]:
    """引擎自有的词汇表面：结构字段取值 + ``fields`` 的**键**（不含事实取值）。"""
    surface: list[str] = []
    for unit in ir.walk():
        for name in _STRUCTURAL_FIELDS:
            value = getattr(unit, name)
            if value is not None:
                surface.append(f"{name}={value}")
        surface.extend(f"field={key}" for key in sorted(unit.fields))
    return surface


def check_ir_purity(ir: ContentIR, *, extra_words: Iterable[str] = ()) -> list[str]:
    """返回命中的 token（``token`` 形如 ``styleId`` / ``文档类型名``）；空即纯净。

    返回字符串而不是 ``Finding``：调用方需要把命中折进自己的 ``Finding``
    （规则编号、定位、文案由调用方决定），而本函数只负责"命中了什么"。
    """
    text = json.dumps(ir_payload(ir), ensure_ascii=False, sort_keys=True)
    hits = [token for token in FORBIDDEN_IR_TOKENS if token in text]
    surface = "\n".join(ir_surface(ir))
    hits.extend(word for word in extra_words if word and word in surface)
    return _dedupe(hits)


def assert_ir_clean(ir: ContentIR, *, extra_words: Iterable[str] = ()) -> None:
    """硬自检：IR 不纯净即抛 :class:`EngineError`。

    抛 ``E_INTERNAL`` 而不是业务错误码：IR 带样式痕迹是**引擎缺陷**，不是使用者
    可以处置的问题——错误信息里给出命中的 token，便于定位到产出它的那行代码。
    """
    hits = check_ir_purity(ir, extra_words=extra_words)
    if not hits:
        return
    raise EngineError(
        ErrorCode.INTERNAL,
        f"内容 IR 违反硬约束，出现禁用 token：{hits}",
        where={
            "tokens": hits,
            "forbidden": list(FORBIDDEN_IR_TOKENS),
            "hint": "IR 只表达'这是什么内容'；样式由渲染层按模板契约决定（DD-3.2（2））",
        },
        remediation=[
            "定位产出该 token 的装配代码并移除它",
            "若该 token 来自事实数据，请在事实源侧清理（它几乎必然来自样式层）",
        ],
    )


def _dedupe(items: Iterable[str]) -> list[str]:
    seen: dict[str, None] = {}
    for item in items:
        seen.setdefault(item, None)
    return list(seen)
