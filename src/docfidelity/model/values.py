"""取值原语与字段映射求值（DD-6.2 的 ``fields`` 一侧）。

从 ``projection.py`` 分出来，只为把那份文件压在 800 行以内（NFR-09）——
**口径没有拆**：``scalar_text`` / ``sort_key`` / ``read_field`` 在本层仍是各自唯一的实现。

三条容易写错、且错了不报错的地方
--------------------------------
1. **值 → 文本只有一处实现**（:func:`scalar_text`）。过滤匹配、分组键、表格单元格、
   要点文本都调它。多一份实现就会出现"过滤说匹配上了、分组说不是同一组"。
2. **排序需要全序**（:func:`sort_key`）。事实源里同一字段混类型是常态
   （``"3"`` 与 ``3``），直接 ``sorted`` 会抛 ``TypeError``。
3. **``bool`` 不是数字**。Python 里 ``True`` 是 ``int`` 的实例，``sum`` 会把一个
   布尔开关算进合计里，且没有任何地方会报错。
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator, Mapping, Sequence
from typing import Any, Final

from docfidelity.contracts.domain import DomainObject
from docfidelity.contracts.errors import EngineError, ErrorCode

from .validation import is_blank

__all__ = [
    "AGGREGATE_NAMES",
    "cell_text",
    "first_non_blank",
    "is_aggregate",
    "optional_int",
    "optional_text",
    "read_field",
    "resolve_expression",
    "scalar_text",
    "sort_key",
]

_AGGREGATE_RE: Final = re.compile(r"^(?P<name>count|sum|min|max|join)\((?P<arg>[^()]*)\)$")

#: 无参聚合：``count``（等价于 ``count()``）。
_COUNT_EXPR: Final = "count"

_JOIN_SEPARATOR: Final = "、"

#: 投影 ``fields`` 里可用的聚合函数名。封闭集合——档案不能自定义语义。
AGGREGATE_NAMES: Final[tuple[str, ...]] = ("count", "sum", "min", "max", "join")


# -- 取值 -----------------------------------------------------------------


def read_field(obj: DomainObject, path: str | None) -> Any:
    """按点路径取值；任一段缺失即返回 ``None``。

    不抛错：字段缺失是**常态**（可选字段、版本差异），由 ``required`` 与 Finding 管。
    在这里抛错会让一个可选字段的缺席升级成整批求值失败。

    点路径只在对象上下钻，**不索引集合**——要遍历集合用 ``for_each``。
    """
    if not path:
        return None
    current: Any = obj.fields
    for step in path.split("."):
        if not isinstance(current, Mapping):
            return None
        current = current.get(step)
    return current


def scalar_text(value: Any) -> str:
    """取值 → 文本。**全内核唯一的一处实现**（本层内）。"""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return value
    if isinstance(value, int | float):
        return json.dumps(value)
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def cell_text(value: Any) -> str:
    """表格单元格文本：标量走 :func:`scalar_text`，复合值折成确定性 JSON。"""
    if isinstance(value, str | int | float | bool) or value is None:
        return scalar_text(value)
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def sort_key(value: Any) -> tuple[int, float, str]:
    """全序键：``None`` < 布尔 < 数值 < 文本 < 其它。

    混合类型必须有一个全序，否则 ``sorted`` 会在 ``str`` 与 ``int`` 之间抛
    ``TypeError``——而事实源里的同一个字段混类型是很常见的。
    """
    if value is None:
        return (0, 0.0, "")
    if isinstance(value, bool):
        return (1, float(value), "")
    if isinstance(value, int | float):
        return (2, float(value), "")
    if isinstance(value, str):
        return (3, 0.0, value)
    return (4, 0.0, scalar_text(value))


def first_non_blank(values: Iterator[Any]) -> Any:
    for value in values:
        if not is_blank(value):
            return value
    return None


def optional_text(value: Any) -> str | None:
    return None if is_blank(value) else scalar_text(value)


def optional_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return int(value)


def is_number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


# -- 字段映射求值 ---------------------------------------------------------


def is_aggregate(expression: str) -> bool:
    """该字段表达式是不是聚合（``count`` / ``sum(x)`` …）。"""
    stripped = expression.strip()
    return stripped == _COUNT_EXPR or _AGGREGATE_RE.match(stripped) is not None


def resolve_expression(
    expression: str, items: Sequence[DomainObject], representative: DomainObject | None
) -> Any:
    """求值一条字段表达式。

    普通字段取自**代表条目**（排序后的首条）；聚合在整组上计算。
    """
    stripped = expression.strip()
    if stripped == _COUNT_EXPR:
        return len(items)
    match = _AGGREGATE_RE.match(stripped)
    if match is not None:
        return _aggregate(match.group("name"), match.group("arg").strip(), items)
    if representative is None:
        return None
    return read_field(representative, stripped)


def _aggregate(name: str, argument: str, items: Sequence[DomainObject]) -> Any:
    values = [read_field(item, argument) for item in items] if argument else []
    if name == _COUNT_EXPR:
        return len(items) if not argument else sum(1 for value in values if not is_blank(value))
    present = [value for value in values if not is_blank(value)]
    if not present:
        return None
    if name == "sum":
        # 布尔被排除：把"有/无"这个开关算进合计是错的，而且不会报错。
        numbers = [float(value) for value in present if is_number(value)]
        return sum(numbers) if numbers else None
    if name == "join":
        # 去重但保序：dict 保插入序，set 不保——报告里的取值顺序必须可复现。
        seen: dict[str, None] = {}
        for value in present:
            seen.setdefault(scalar_text(value), None)
        return _JOIN_SEPARATOR.join(seen)
    if name in {"min", "max"}:
        return (min if name == "min" else max)(present, key=sort_key)
    raise EngineError(
        ErrorCode.PROFILE_INVALID,
        f"未知的聚合函数：{name!r}",
        where={"aggregate": name, "expression": f"{name}({argument})"},
        remediation=[f"改用 {' / '.join(AGGREGATE_NAMES)} 之一"],
    )
