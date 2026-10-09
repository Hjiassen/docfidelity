"""结构化输出的**本地**实现（DD-12.4 的降级路径）。

为什么需要它
------------
DD-12.4 把结构化输出的第一手段定为"请求侧的 JSON Schema"：把输出模型交给 SDK，
由它在请求里带 `response_format=json_schema`。**那是要求，不是事实**——
第三方 OpenAI 兼容网关会把 `response_format` 静默忽略，此时模型**根本不知道**
要输出什么形状：它只被告知"只输出结构化结果"，而形状原本是靠请求参数传达的。
症状是一句与病因无关的 `ModelBehaviorError: Invalid JSON when parsing model output`
（DD-12.4 第 1286 行原文预判了这个情形）。

于是本模块提供那条降级路径的三块：

1. :func:`schema_hint` —— 把输出模型折成一段**形状说明**放进提示词。
   形状由模型自动派生，不是人手写的文案：改契约即改提示词，两者不可能漂移。
   （`agents.instructions` 的纪律"能靠 schema 约束的就不靠提示词叮嘱"针对的是
   **人手写**的叮嘱；这里是机器派生，且该纪律的前提——schema 约束可用——恰好不成立。）
2. :func:`extract_json_object` —— 从模型回复里取出 JSON 对象。只做**结构抽取**
   （剥 markdown 围栏、配平括号），**不做语义修补**：缺字段就是缺字段，
   绝不补默认值——那正是硬规则 19 要消灭的"静默采用"。
3. :func:`parse_structured` —— 用 pydantic 本地校验，失败时给出**具体到第一处**的原因。
   原因必须具体：重试环路把 ``detail`` 原样喂回给模型（`routing._failure_hint`），
   一句笼统的"输出不符合 Schema"等于把同一个错误问三遍（`routing` 文档第 3 条）。

两条边界
--------
* **本模块不 import ``agents``**：抽取与校验是纯 pydantic + stdlib 的事，
  把它塞进适配层会让"验证一个输出形状"必须先装 openai-agents。
* **不替模型决定格式版本**：``format_version`` 由本地确定，模型无权选。
  实测过它的危害：模型一旦回一个自编的版本号，``FormatModel`` 的版本校验会抛
  ``FormatVersionError``——一个与"模型没按 schema 作答"完全无关的失败。
"""

from __future__ import annotations

import json
import re
import types
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final, Literal, Union, get_args, get_origin

from pydantic import BaseModel, ValidationError

from docfidelity.contracts.base import FormatModel

__all__ = [
    "SCHEMA_RULES",
    "StructuredParse",
    "extract_json_object",
    "output_instructions",
    "parse_structured",
    "schema_hint",
]

#: 交给模型前拼在 Agent instructions 后面的硬要求。
#: 与 `instructions.COMMON_RULES` 第 4 条（"只输出结构化结果"）**不是重复**：
#: 那一条说的是"别写解释"，这三条说的是"用什么**语法**写"——
#: 缺了后者的症状是模型给出一段"看得懂但解析不了"的散文式 JSON。
SCHEMA_RULES: Final[tuple[str, ...]] = (
    "只输出一个 JSON 对象，不要输出数组或裸值。",
    "不要用 markdown 代码块包裹，前后不要有任何解释文字。",
    "不要输出字段清单以外的字段。",
)

#: 嵌套结构的展开深度：顶层字段展开**一层**，再深的层只报"对象"。
#: 更深的展开会把一份大契约（如 `ContentIR`）铺成几百行提示词，
#: 而小模型对长提示词尤其敏感（需求 5.2.5）；NFR-25 的上下文预算也不允许。
_MAX_DEPTH: Final = 1

#: ```json ... ``` / ``` ... ``` 围栏。缺了它，模型最常见的"礼貌包装"
#: 会让整段回复判为非法 JSON。
_FENCE_RE: Final = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)

#: 由本地确定、不从模型回复里取的字段（见模块文档"两条边界"）。
_LOCAL_OWNED: Final[frozenset[str]] = frozenset({"format_version"})

_SCALARS: Final[Mapping[Any, str]] = {str: "字符串", int: "整数", float: "小数", bool: "布尔"}


def schema_hint(model: type[FormatModel]) -> str:
    """把输出模型折成一段字段清单（供提示词使用）。

    刻意**不**直接贴 ``model_json_schema()``：那份 JSON 带 ``$defs`` / ``$ref``，
    小模型读不懂引用、却会被引用层的字面量带偏（需求 5.2.1 点名多跳推理弱）。
    逐字段渲染成自然语言的代价是丢一点精度，换来的是"不用理解引用语法"。
    """
    fields = [(name, info) for name, info in model.model_fields.items() if name not in _LOCAL_OWNED]
    if not fields:
        return "输出必须是一个 JSON 对象。"
    lines = [f"输出必须是一个 JSON 对象，字段如下（共 {len(fields)} 个）："]
    for name, info in fields:
        required = "必填" if info.is_required() else "可选"
        lines.append(f"- {name}：{_type_label(info.annotation, depth=0)}，{required}")
    lines.append("取不到值的可选字段留空或省略，不要编造取值。")
    return "\n".join(lines)


def output_instructions(model: type[FormatModel]) -> str:
    """拼装追加到 instructions 末尾的"输出格式"段（规则 + 形状）。"""
    rules = "".join(f"{index}. {rule}" for index, rule in enumerate(SCHEMA_RULES, start=1))
    return f"输出格式：\n{rules}\n\n{schema_hint(model)}"


@dataclass(frozen=True)
class StructuredParse:
    """一次本地解析的结果。``reason`` 为空当且仅当 ``payload`` 非空。"""

    payload: dict[str, Any] | None
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.payload is not None


def extract_json_object(text: str) -> str | None:
    """从一段可能是散文的回复里取出**第一个配平的 JSON 对象**；取不到返回 ``None``。

    先看 markdown 围栏内部，再退回全文：围栏里若不是对象（比如模型把对象写在
    围栏外、围栏里只放了个说明），退回全文仍有机会救回来。
    """
    candidate = text.strip()
    if not candidate:
        return None
    fenced = _FENCE_RE.search(candidate)
    if fenced is not None:
        inner = _first_balanced_object(fenced.group(1).strip())
        if inner is not None:
            return inner
    return _first_balanced_object(candidate)


def parse_structured(model: type[FormatModel], text: str) -> StructuredParse:
    """把模型回复解析成 ``model`` 的载荷；失败时给出具体原因（喂回重试用）。"""
    raw = extract_json_object(text)
    if raw is None:
        return StructuredParse(None, f"回复里找不到 JSON 对象（开头：{_head(text)}）")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        return StructuredParse(
            None, f"JSON 语法错误：{exc.msg}（第 {exc.lineno} 行第 {exc.colno} 列）"
        )
    # 不需要再判"顶层是不是对象"：`_first_balanced_object` 只交回以 ``{`` 起、括号配平的
    # 片段，它能通过 ``json.loads`` 就必然是一个对象。多写一层判定的代价是留一条永远
    # 走不到的分支——而"看起来能处理数组"会让下一个人以为数组是被支持的。
    cleaned = {key: value for key, value in data.items() if key not in _LOCAL_OWNED}
    try:
        validated = model.model_validate(cleaned)
    except ValidationError as exc:
        return StructuredParse(None, _first_error(exc))
    payload = validated.model_dump(mode="json")
    return StructuredParse(payload if isinstance(payload, dict) else None, "")


# --------------------------------------------------------------------- 内部
def _first_balanced_object(text: str) -> str | None:
    """扫描出第一个括号配平的 ``{...}``（故交回的片段必然是 JSON 对象）。

    必须自己扫而不能用正则：字符串字面量里的 ``}``（例如文案里写了一个公式）
    会让非贪婪匹配提前截断，而截断出来的"合法 JSON"会把错误的载荷一路写进报告。
    """
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return None


def _first_error(exc: ValidationError) -> str:
    """把 pydantic 的报错折成"第一处错在哪"，而不是把整棵错误树抛出去。

    最常见两类失败（多字段 / 缺字段）给**可照做的**话术：这两句会被 `routing`
    原样喂回给模型当重试反馈，而 pydantic 的原文（``Extra inputs are not permitted`` /
    ``Field required``）虽然没错，却没说该删谁、该补谁——模型只能再猜一次。
    """
    errors = exc.errors()
    if not errors:
        return "输出不符合输出契约"
    first = errors[0]
    location = ".".join(str(part) for part in first.get("loc", ())) or "（根对象）"
    kind = first.get("type", "")
    if kind == "extra_forbidden":
        return f"多出了字段清单以外的字段 {location}，请去掉它"
    if kind == "missing":
        return f"缺少必填字段 {location}"
    return f"字段 {location} 不合法：{first.get('msg', '')}"


def _type_label(annotation: Any, *, depth: int) -> str:
    """把类型标注折成一句人话。``depth`` 到了上限就不再展开嵌套模型。"""
    origin = get_origin(annotation)
    args = get_args(annotation)
    if origin is Union or origin is types.UnionType:
        return _union_label(args, depth=depth)
    if origin is Literal:
        return "取值 " + " / ".join(repr(value) for value in args)
    if origin in (list, set, frozenset, tuple):
        inner = _type_label(args[0], depth=depth) if args else "任意"
        return f"数组<{inner}>"
    if origin is dict:
        return "对象"
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        if depth >= _MAX_DEPTH:
            return "对象"
        parts = [
            f"{name}: {_type_label(info.annotation, depth=depth + 1)}"
            for name, info in annotation.model_fields.items()
            if name not in _LOCAL_OWNED
        ]
        return "对象（" + "、".join(parts) + "）" if parts else "对象"
    return _SCALARS.get(annotation, getattr(annotation, "__name__", str(annotation)))


def _union_label(args: tuple[Any, ...], *, depth: int) -> str:
    options = [arg for arg in args if arg is not type(None)]
    label = " 或 ".join(_type_label(arg, depth=depth) for arg in options)
    return f"{label}（可为空）" if len(options) != len(args) else label


def _head(text: str, limit: int = 80) -> str:
    """失败原因里附一小段原文开头，便于人看出"模型到底答了什么"。"""
    flat = " ".join(text.split())
    if not flat:
        return "（空）"
    return flat[:limit] + ("…" if len(flat) > limit else "")
