"""工具白名单（DD-12.2 第 3 条：本文件是**唯一出口**）。

分工
----
* `spec.py` —— 规格与结构化失败的定义、声明自检；
* `library.py` —— 具体工具的声明（数据）；
* 本文件 —— 白名单出口 + 调用闸门 :func:`invoke`。

不变量（由测试与启动自检共同盯住）
----------------------------------
1. 白名单里**不存在任何 OOXML 直写**：``handler_ref`` 指向 ``docfidelity.ooxml`` /
   ``docx`` / ``lxml`` 一律被 :func:`~docfidelity.agents.tools.spec.validate_spec` 拒绝
   （决策三）。
2. 已接线工具的 ``handler_ref`` **必须能解析到可调用对象**——点路径写错的后果是
   "模型调一个永远失败的工具"，而它看起来和"这次刚好失败"一模一样。
3. 写盘类工具**必须**标 ``requires_approval``。
"""

from __future__ import annotations

import time
from collections.abc import Iterable, Mapping
from typing import Any

from pydantic import ValidationError

from docfidelity.contracts.base import FormatModel
from docfidelity.contracts.errors import EngineError, ErrorCode
from docfidelity.platform.fs import safe_join

from .library import TOOL_SPECS, TOOLS_BY_NAME
from .spec import (
    ToolApprovalPolicy,
    ToolFailure,
    ToolOutput,
    ToolResult,
    ToolSpec,
    as_args,
    call_kwargs,
    failure_of,
    validate_spec,
)

__all__ = [
    "TOOLS",
    "TOOL_SPECS",
    "assert_whitelist_sane",
    "descriptions",
    "exposed_names",
    "get",
    "guard_approval",
    "guard_call",
    "guard_paths",
    "invoke",
    "names",
]


#: 名称 → 规格。对外的**唯一**白名单视图。
TOOLS: Mapping[str, ToolSpec] = TOOLS_BY_NAME


def names() -> tuple[str, ...]:
    """白名单全部工具名（含未暴露的），按声明顺序。"""
    return tuple(spec.name for spec in TOOL_SPECS)


def exposed_names() -> tuple[str, ...]:
    """进入模型可见面的工具名（见 ``ToolSpec.exposed``）。"""
    return tuple(spec.name for spec in TOOL_SPECS if spec.exposed)


def descriptions() -> dict[str, str]:
    """已暴露工具的描述文本（"一句动作 + 参数 + 最小示例"，需求 5.2.5）。

    返回值同时是 :func:`docfidelity.agents.instructions.instructions_digest` 的输入：
    工具描述是模型看到的另一半输入，只版本化提示词而放过描述，等于漏掉一半的回归面。
    """
    return {spec.name: spec.describe() for spec in TOOL_SPECS if spec.exposed}


def get(name: str) -> ToolSpec:
    """按名取规格。未登记的工具名是**调用方的编程错误**，直接抛。

    与"模型请求了一个不存在的工具"要分开处置：后者走 :func:`invoke` 的结构化失败，
    因为那是运行期输入；前者是代码里写错了名字，静默兜底只会把它藏起来。
    """
    spec = TOOLS.get(name)
    if spec is None:
        raise EngineError(
            ErrorCode.INTERNAL,
            f"未登记的工具名：{name}",
            where={"tool": name},
            remediation=["在 agents/tools/library.py 登记该能力，或改用已登记的工具"],
        )
    return spec


def assert_whitelist_sane() -> None:
    """白名单自检：逐条跑声明校验，并确认已接线工具的实现位置可解析。

    在**测试**与未来的启动自检里调用。理由与 ``qa`` 的规则加载同源：声明错误要在
    加载期炸，而不是等模型调到那一条才发现（那时看到的只是一次"工具调用失败"）。
    """
    for spec in TOOL_SPECS:
        validate_spec(spec)
        if spec.bound:
            spec.handler()


def guard_approval(spec: ToolSpec, policy: ToolApprovalPolicy) -> ToolFailure | None:
    """高危审批检查。**排在参数校验之前**。

    顺序是刻意的：未获批准的高危动作不该先被"校验一遍参数"。否则模型可以用一串
    未批准的调用探出参数形状（哪几个字段必填、类型如何），而探路本身就不该发生。
    """
    if spec.requires_approval and not policy.approves(spec.name):
        return ToolFailure(
            code=ErrorCode.CONFIRM_REQUIRED,
            message=f"工具 {spec.name} 属高危动作（写盘 / 覆盖 / 刷新域），需要人工确认后才可执行",
            retryable=False,
            remediation=["在界面确认该动作后重试", "或改用只读工具"],
        )
    return None


def guard_paths(
    spec: ToolSpec, args: FormatModel, policy: ToolApprovalPolicy
) -> ToolFailure | None:
    """路径作用域检查。**排在参数校验之后**——路径值要先成为合法字符串才谈得上越界。"""
    for param in spec.path_params:
        escaped = _escapes_roots(str(getattr(args, param)), policy.allowed_roots)
        if escaped is not None:
            return ToolFailure(
                code=ErrorCode.PATH_ESCAPE,
                message=f"工具 {spec.name} 的路径参数 {param} 落在允许范围之外",
                retryable=False,
                where={"param": param, "detail": escaped},
                remediation=["改用允许目录下的路径"],
            )
    return None


def guard_call(spec: ToolSpec, args: FormatModel, policy: ToolApprovalPolicy) -> ToolFailure | None:
    """工具输入护栏的**组合入口**：审批 → 路径。返回 ``None`` 表示放行。

    判定逻辑只有这一处，`agents.guardrails` 只做转发（DD-12.5 把这一层的落点记在
    guardrails，而实现必须只有一份——两份的下场是"护栏拦了、闸门放了"，
    两个入口给出相反答案，且没人能说清哪个对）。
    """
    return guard_approval(spec, policy) or guard_paths(spec, args, policy)


def invoke(
    name: str,
    args: Mapping[str, Any] | FormatModel | None = None,
    *,
    policy: ToolApprovalPolicy | None = None,
) -> ToolResult:
    """调用一个已暴露的工具，**永远返回结构化结果**，不向调用方抛异常。

    ``policy`` 是准入策略（已获批的高危工具 + 允许的路径根目录），默认空策略：
    空策略下写盘类工具一律被拦、带路径参数的工具一律越界——**失败关闭**是刻意的默认。

    实现缺陷（未暴露的工具、实现函数抛非 ``EngineError``）回 ``E_INTERNAL``；
    ``BaseException``（键盘中断、进程终止）**不吞**，照常向上传播。
    """
    effective = policy or ToolApprovalPolicy()
    spec = TOOLS.get(name)
    if spec is None:
        return _fail(
            name,
            ErrorCode.INTERNAL,
            f"工具 {name} 未在白名单中",
            retryable=False,
            remediation=["只能使用已登记的工具"],
        )
    if not spec.exposed:
        return _fail(
            name,
            ErrorCode.INTERNAL,
            f"工具 {name} 当前不可调用（状态 {spec.availability}）",
            retryable=False,
            remediation=["这是实现缺陷：不可调用的工具不应出现在模型的可见工具面"],
            requires_approval=spec.requires_approval,
        )

    blocked = guard_approval(spec, effective)
    if blocked is not None:
        return ToolResult(
            tool=name,
            ok=False,
            failure=blocked,
            requires_approval=spec.requires_approval,
            approved=effective.approves(name),
        )

    args_model_cls = spec.args_model()
    try:
        validated = args_model_cls.model_validate(dict(as_args(args)))
    except ValidationError as exc:
        detail = "；".join(
            f"{'.'.join(str(part) for part in item['loc'])}: {item['msg']}" for item in exc.errors()
        )
        return _fail(
            name,
            ErrorCode.SCHEMA_INVALID,
            f"工具 {name} 的参数不合法：{detail}",
            retryable=True,
            remediation=["只使用工具说明里列出的参数名与类型，然后重试"],
            requires_approval=spec.requires_approval,
        )

    blocked = guard_paths(spec, validated, effective)
    if blocked is not None:
        return ToolResult(
            tool=name,
            ok=False,
            failure=blocked,
            requires_approval=spec.requires_approval,
            approved=True,
        )

    started = time.perf_counter()
    try:
        raw = spec.handler()(**call_kwargs(spec, validated))
    except EngineError as error:
        return _result(
            spec, ok=False, failure=failure_of(error, tool=name), duration_ms=_ms(started)
        )
    except Exception as exc:
        return _result(
            spec,
            ok=False,
            failure=ToolFailure(
                code=ErrorCode.INTERNAL,
                message=f"工具 {name} 执行失败（{type(exc).__name__}），原始信息已记录到日志",
                retryable=False,
                remediation=["查看作业日志定位实现缺陷"],
            ),
            duration_ms=_ms(started),
        )

    return _result(spec, ok=True, output=_output_of(spec, raw), duration_ms=_ms(started))


def _escapes_roots(candidate: str, roots: Iterable[str]) -> str | None:
    """候选路径是否落在任一允许根内。返回 ``None`` 表示在范围内。"""
    if not roots:
        return "未配置允许的根目录（allowed_roots 为空）"
    for root in roots:
        try:
            safe_join(root, candidate)
        except EngineError:
            continue
        return None
    return f"不在任何允许根之下：{list(roots)}"


def _output_of(spec: ToolSpec, raw: Any) -> dict[str, Any]:
    """把实现函数的返回值折成可序列化字典。

    就地修改型实现返回 ``None``，此时如实记成空 ``data`` 加一句 note，
    而不是伪造一个 ``{"ok": true}``——后者会让人以为工具真的带回了什么。
    """
    if raw is None:
        return ToolOutput(data={}, note=f"{spec.name} 就地生效，无返回值").model_dump()
    if isinstance(raw, ToolOutput):
        return raw.model_dump()
    if isinstance(raw, FormatModel):
        return ToolOutput(data={"value": raw.model_dump()}).model_dump()
    if isinstance(raw, tuple | list):
        return ToolOutput(data={"items": [str(item) for item in raw]}).model_dump()
    if isinstance(raw, Mapping):
        return ToolOutput(data={str(key): str(value) for key, value in raw.items()}).model_dump()
    return ToolOutput(data={"value": str(raw)}).model_dump()


def _ms(started: float) -> int:
    return max(0, int((time.perf_counter() - started) * 1000))


def _fail(
    tool: str,
    code: str,
    message: str,
    *,
    retryable: bool,
    remediation: Iterable[str] = (),
    where: Mapping[str, Any] | None = None,
    requires_approval: bool = False,
) -> ToolResult:
    return ToolResult(
        tool=tool,
        ok=False,
        failure=ToolFailure(
            code=code,
            message=message,
            retryable=retryable,
            where=dict(where or {}),
            remediation=list(remediation),
        ),
        requires_approval=requires_approval,
        approved=not requires_approval,
    )


def _result(
    spec: ToolSpec,
    *,
    ok: bool,
    output: dict[str, Any] | None = None,
    failure: ToolFailure | None = None,
    duration_ms: int,
) -> ToolResult:
    return ToolResult(
        tool=spec.name,
        ok=ok,
        output=output,
        failure=failure,
        requires_approval=spec.requires_approval,
        approved=True,
        duration_ms=duration_ms,
    )
