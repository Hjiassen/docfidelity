"""工具规格与结构化失败（DD-12.2）。

三条纪律的落点
--------------
1. **输入输出必然是可序列化的 Pydantic 模型** —— 每个工具的输入模型由
   :meth:`ToolSpec.args_model` 用声明出来的扁平参数**生成**（``pydantic.create_model``），
   因此"模型能编造一个不存在的参数"这件事在**校验层**就被挡住（``extra="forbid"``），
   而不是靠提示词叮嘱（对应需求 5.2.1 的"工具选择易错 · 编造不存在的参数"）。
2. **必须返回结构化失败** —— 工具不向模型抛裸异常，一律回 :class:`ToolFailure`，
   带 ``code`` / ``retryable`` / ``remediation``，模型据此决定重试或降级。
3. **白名单** —— 规格只**声明**能力与实现位置，不改写任何 XML；指向 ``ooxml`` 的
   ``handler_ref`` 被 :func:`validate_spec` 直接拒绝（决策三：Agent 只做决策）。

为什么参数是声明而不是类型标注
------------------------------
工具描述要交给模型（一句动作 + 参数说明 + 一个最小示例，需求 5.2.5），而提示词要能被
版本化与回归（NFR-17）。把参数写成 `tuple[ToolParam, ...]` 让"模型看到的描述"与
"运行时校验的形状"**来自同一份数据**——分两处写必然漂移，而漂移的表现是
"提示词说有这个参数、运行时拒收它"。
"""

from __future__ import annotations

import importlib
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Final, Literal, cast

from pydantic import Field, create_model

from docfidelity.contracts.base import FormatModel
from docfidelity.contracts.config import Terminology
from docfidelity.contracts.domain import DomainModel, DomainObject
from docfidelity.contracts.errors import EngineError, ErrorCode
from docfidelity.contracts.fact import AssetRef, Fact, FactSnapshot
from docfidelity.contracts.ir import ContentIR
from docfidelity.contracts.profile import DocTypeProfile
from docfidelity.contracts.qa import TraceMatrix
from docfidelity.contracts.template import TemplateFingerprint, TemplatePackage

__all__ = [
    "FORBIDDEN_HANDLER_PREFIXES",
    "MAX_PARAMS_PER_TOOL",
    "ParamKind",
    "ToolApprovalPolicy",
    "ToolAvailability",
    "ToolFailure",
    "ToolGroup",
    "ToolOutput",
    "ToolParam",
    "ToolResult",
    "ToolSpec",
    "as_args",
    "call_kwargs",
    "failure_of",
    "validate_spec",
]

#: 工具分组（需求 5.5 的六组）。分组只用于"归属呈现"与人工检索，不参与分派逻辑
#: ——按分组写 `if group == ...` 的分支会让"新加一个工具"变成"改一处分派"。
ToolGroup = Literal["template", "facts", "content", "render", "gate", "increment"]

#: 参数类型的**闭集**。刻意不含"任意 JSON"这一档：一旦允许嵌套对象，"参数扁平"
#: （NFR-25）就只是口号，而小模型对嵌套参数的填充正确率是最低的一档。
#:
#: 取值规则（一条，无例外）：**每个取值要么是标量，要么对应一个 `contracts` 模型**。
#: 于是"这个参数怎么校验"永远只有一种答案（pydantic），不必为某个工具特判；
#: 也正是这条规则让 :meth:`ToolSpec.args_model` 能把全部工具统一生成出输入模型。
ParamKind = Literal[
    "str",
    "int",
    "float",
    "bool",
    "str_list",
    "float_list",
    "content_ir",
    "fingerprint",
    "template_package",
    "terminology",
    "fact_list",
    "asset_ref_list",
    "domain_object_list",
    "fact_snapshot",
    "doc_type_profile",
    "trace_matrix",
    "domain_model_list",
]

#: 工具能力的三种状态。区分它们不是为了好看，而是为了让"缺什么"可执行：
#: ``context_bound`` 的入口吃**流水线中途**的运行时上下文，``unbound`` 则是能力本身
#: 还没有。``context_bound`` **不等于**"缺一个 JSON 门面"——当上下文本身不可 JSON 化
#: （如 render 中途的 ``RenderContext``）时，补门面等于另开一条落笔路径（ADR-003），
#: 故这类工具保持不可见是**结论**而非欠账（见 ``library.py`` 的"门面下沉"）。
ToolAvailability = Literal["callable", "context_bound", "unbound"]

#: 单个工具的参数个数上限。超过它不是"功能强"，是**该拆成两个工具**——
#: 参数越多，模型选错的概率按参数个数累积（需求 5.2.1）。
MAX_PARAMS_PER_TOOL: Final = 8

#: ``handler_ref`` 不得指向的模块前缀。``ooxml`` 是唯一出口且**不属于 Agent**：
#: 工具表里出现一次 OOXML 直写，决策三就作废（DD-12.2 第 3 条的 CI 断言对象）。
FORBIDDEN_HANDLER_PREFIXES: Final[tuple[str, ...]] = (
    "docfidelity.ooxml",
    "docx",
    "lxml",
)

_PARAM_TYPES: Final[dict[str, Any]] = {
    "str": str,
    "int": int,
    "float": float,
    "bool": bool,
    "str_list": list[str],
    "float_list": list[float],
    "content_ir": ContentIR,
    "fingerprint": TemplateFingerprint,
    "template_package": TemplatePackage,
    "terminology": Terminology,
    "fact_list": list[Fact],
    "asset_ref_list": list[AssetRef],
    "domain_object_list": list[DomainObject],
    "fact_snapshot": FactSnapshot,
    "doc_type_profile": DocTypeProfile,
    "trace_matrix": TraceMatrix,
    "domain_model_list": list[DomainModel],
}


class ToolParam(FormatModel):
    """一个扁平参数的声明。"""

    name: str = Field(description="参数名（snake_case，与实现函数的形参一致）")
    kind: ParamKind = "str"
    description: str = Field(description="一句说明，会原样进入模型看到的工具描述")
    required: bool = True


class ToolFailure(FormatModel):
    """结构化失败。

    ``retryable`` 是给**模型**的提示：``True`` 表示"换个参数再试可能成"，
    ``False`` 表示"再试也一样"（例如能力未接线）。它与 ``EngineError.fatal``
    是两件事：后者决定**阶段**是否中断，前者只决定**这一次工具调用**是否值得重试。
    """

    code: str = Field(description="错误码，取值见 contracts.errors.ErrorCode")
    message: str = Field(description="面向使用者的中文说明")
    retryable: bool = False
    where: dict[str, Any] = Field(default_factory=dict)
    remediation: list[str] = Field(default_factory=list)


class ToolOutput(FormatModel):
    """工具的结构化返回值。

    返回值刻意保持"一个 data 字典 + 一句 note"：内核各模块的返回类型互不相同
    （指纹元组 / 契约对象 / 报表），为每个工具编一份假 schema 只会得到一堆
    "看起来精确、实际是搬运"的模型。真正的形状约束在**输入**侧（模型容易填错的那侧），
    输出侧由各工具文档说明并进入留痕。
    """

    data: dict[str, Any] = Field(default_factory=dict)
    note: str | None = None


class ToolResult(FormatModel):
    """一次工具调用的完整结果（成功与失败同形）。"""

    tool: str
    ok: bool
    output: dict[str, Any] | None = None
    failure: ToolFailure | None = None
    requires_approval: bool = False
    approved: bool = True
    duration_ms: int | None = Field(default=None, ge=0)


@dataclass(frozen=True)
class ToolApprovalPolicy:
    """工具调用的准入策略（DD-12.5 的"工具输入护栏"）。

    ``allowed_roots`` 为空表示**没有配置作用域**，此时凡带路径参数的工具都无法通过
    （失败关闭）。默认放行等于把"作用域没配"悄悄变成"作用域是整个磁盘"。
    """

    approvals: frozenset[str] = field(default_factory=frozenset)
    allowed_roots: tuple[str, ...] = field(default=())

    @classmethod
    def of(
        cls, approvals: Iterable[str] = (), allowed_roots: Iterable[str] = ()
    ) -> ToolApprovalPolicy:
        return cls(frozenset(approvals), tuple(root for root in allowed_roots if root))

    def approves(self, tool: str) -> bool:
        return tool in self.approvals


@dataclass(frozen=True)
class ToolSpec:
    """一个工具的声明。

    ``availability`` 与 ``handler_ref`` 一起回答两个不同的问题：**能力在哪**（实现位置）
    与**模型能不能直接调**（参数是不是 JSON 化的扁平面）。二者必须分开记——
    把"已实现但入口吃运行时上下文"当成"未实现"，会让人反复重写已经写好的东西；
    把"未实现"当成"已实现"，则会在路线图上凭空多出一块。
    """

    name: str
    group: ToolGroup
    summary: str
    example: str
    params: tuple[ToolParam, ...] = field(default=())
    availability: ToolAvailability = "callable"
    handler_ref: str | None = None
    source_name: str | None = None
    requires_approval: bool = False
    writes: bool = False
    path_params: tuple[str, ...] = field(default=())
    context_note: str | None = None

    @property
    def bound(self) -> bool:
        """是否已接线到确定性实现（与"模型可见"是两件事）。"""
        return self.handler_ref is not None

    @property
    def exposed(self) -> bool:
        """是否进入模型的可见工具面。

        只有"已接线 **且** 参数是 JSON 化扁平面"的工具才暴露：把一个必然报错的工具
        摆在小模型面前，代价不只是这一次调用失败——它会消耗掉宝贵的重试预算，
        并把"工具选错"训练成一种习惯（需求 5.2.1 的三类短板都由此放大）。
        """
        return self.bound and self.availability == "callable"

    def args_model(self) -> type[FormatModel]:
        """生成该工具的输入模型（扁平参数，未知参数直接拒）。"""
        shape = tuple(
            (param.name, param.kind, param.description, param.required) for param in self.params
        )
        return _build_args_model(self.name, shape)

    def describe(self) -> str:
        """模型看到的工具描述：一句动作 + 参数说明 + 一个最小示例（需求 5.2.5）。"""
        if not self.params:
            params = "无参数。"
        else:
            parts = []
            for param in self.params:
                mark = "" if param.required else "（可选）"
                parts.append(f"{param.name}:{param.kind}{mark} —— {param.description}")
            params = "参数：" + "；".join(parts) + "。"
        return f"{self.summary}{params}示例：{self.example}"

    def handler(self) -> Callable[..., Any]:
        """解析并返回实现函数（进程内缓存）。

        用**惰性点路径**而不是静态 import：工具面是给模型的**能力白名单**，
        它列出各层已经确定性地实现好的入口。静态 import 会把 agents 层与
        registry/render/qa 焊死，而 tools 表的用途恰恰是"不改 agents 就能增删能力"。
        点路径写错的后果由测试兜底（每个已接线工具都必须能解析到可调用对象）。
        """
        if self.handler_ref is None:
            raise EngineError(
                ErrorCode.INTERNAL,
                f"工具 {self.name} 尚未接线到确定性实现，无法调用",
                where={"tool": self.name},
                remediation=["在 agents/tools/library.py 中补上 handler_ref"],
            )
        return _resolve(self.handler_ref)


@lru_cache(maxsize=128)
def _resolve(dotted: str) -> Callable[..., Any]:
    module_name, _, attribute = dotted.rpartition(".")
    module = importlib.import_module(module_name)
    target = getattr(module, attribute, None)
    if target is None or not callable(target):
        raise EngineError(
            ErrorCode.INTERNAL,
            f"工具实现位置 {dotted} 不存在或不可调用",
            where={"handler_ref": dotted},
            remediation=["核对 agents/tools/library.py 的 handler_ref 与内核公开面"],
        )
    return cast(Callable[..., Any], target)


@lru_cache(maxsize=128)
def _build_args_model(
    name: str, shape: tuple[tuple[str, str, str, bool], ...]
) -> type[FormatModel]:
    """由扁平参数声明生成 pydantic 输入模型。

    缓存键是"工具名 + 参数形状"而不是工具名：同一个名字声明成不同形状（改规格）时
    必须真的生成新模型，否则改了声明却不生效——那种失效没有任何地方会喊。
    """
    fields: dict[str, Any] = {}
    for param_name, kind, description, required in shape:
        annotation = _PARAM_TYPES[kind]
        if required:
            fields[param_name] = (annotation, Field(description=description))
        else:
            fields[param_name] = (annotation | None, Field(default=None, description=description))
    model = create_model(
        f"{name}_args",
        __base__=FormatModel,
        __module__=__name__,
        **fields,
    )
    return model


def validate_spec(spec: ToolSpec) -> None:
    """规格自检。任何一条不成立都抛 :class:`~docfidelity.contracts.errors.EngineError`。

    这些是**声明错误**，不是运行期数据问题：写错时要在启动/测试时就炸，
    而不是等模型调到这个工具才发现（DD-12.3 对路由表用的是同一种处置）。
    """
    where = {"tool": spec.name}
    if not spec.summary.endswith("。"):
        raise EngineError(
            ErrorCode.CONFIG_INVALID,
            f"工具 {spec.name} 的一句动作缺句号（需求 5.2.5 要求一句话动作）",
            where=where,
        )
    if not spec.example:
        raise EngineError(
            ErrorCode.CONFIG_INVALID,
            f"工具 {spec.name} 缺最小示例（需求 5.2.5 要求每个工具描述附一个示例）",
            where=where,
        )
    if len(spec.params) > MAX_PARAMS_PER_TOOL:
        raise EngineError(
            ErrorCode.CONFIG_INVALID,
            f"工具 {spec.name} 参数 {len(spec.params)} 个，超过 {MAX_PARAMS_PER_TOOL} 个上限；"
            "参数越多模型选错概率越高，应拆分工具",
            where=where,
            remediation=["把该工具拆成两个更专一的工具"],
        )
    names = [param.name for param in spec.params]
    if len(set(names)) != len(names):
        raise EngineError(ErrorCode.CONFIG_INVALID, f"工具 {spec.name} 存在重名参数", where=where)
    if any(not name.isidentifier() or name != name.lower() for name in names):
        raise EngineError(
            ErrorCode.CONFIG_INVALID,
            f"工具 {spec.name} 的参数名必须是 snake_case 标识符（实现函数按形参名接参数）",
            where=where,
        )
    if spec.handler_ref is not None:
        for prefix in FORBIDDEN_HANDLER_PREFIXES:
            if spec.handler_ref == prefix or spec.handler_ref.startswith(prefix + "."):
                raise EngineError(
                    ErrorCode.CONFIG_INVALID,
                    f"工具 {spec.name} 的实现位置 {spec.handler_ref} 落在 {prefix} 内；"
                    "Agent 只做决策，不得直写 OOXML（决策三 / DD-12.2）",
                    where=where,
                    remediation=["改为调用上一层暴露的确定性能力"],
                )
    if spec.writes and not spec.requires_approval:
        raise EngineError(
            ErrorCode.CONFIG_INVALID,
            f"工具 {spec.name} 会写盘但未标 requires_approval（DD-12.2 第 4 条："
            "写盘 / 覆盖 / 删除 / 刷新域必须过 guardrail）",
            where=where,
        )
    if spec.availability == "unbound" and spec.handler_ref is not None:
        raise EngineError(
            ErrorCode.CONFIG_INVALID,
            f"工具 {spec.name} 标为 unbound 却填了实现位置；二者矛盾，"
            "要么把它标成 context_bound，要么去掉 handler_ref",
            where=where,
        )
    if spec.availability == "context_bound":
        if spec.handler_ref is None:
            raise EngineError(
                ErrorCode.CONFIG_INVALID,
                f"工具 {spec.name} 标为 context_bound 但没有实现位置；"
                "该状态的含义是「已实现、入口吃运行时上下文」",
                where=where,
            )
        if not spec.context_note:
            raise EngineError(
                ErrorCode.CONFIG_INVALID,
                f"工具 {spec.name} 标为 context_bound 但没写 context_note；"
                "不写明缺哪个门面，这条状态就只是「未接线」的另一种说法",
                where=where,
            )
    unknown_paths = [name for name in spec.path_params if name not in set(names)]
    if unknown_paths:
        raise EngineError(
            ErrorCode.CONFIG_INVALID,
            f"工具 {spec.name} 的 path_params 声明了不存在的参数：{unknown_paths}",
            where=where,
        )
    if spec.availability != "callable" and spec.params:
        raise EngineError(
            ErrorCode.CONFIG_INVALID,
            f"工具 {spec.name} 不是 callable 却声明了参数；参数表的用途是「给模型看的形状」"
            "与「运行时校验的依据」，对不可调用的工具两者都不成立，硬凑只会产出看起来精确的猜测",
            where=where,
        )
    if spec.availability != "callable" and spec.path_params:
        raise EngineError(
            ErrorCode.CONFIG_INVALID,
            f"工具 {spec.name} 不是 callable 却声明了 path_params（路径检查只在调用时发生）",
            where=where,
        )


def call_kwargs(spec: ToolSpec, args: FormatModel) -> dict[str, Any]:
    """把校验过的入参摊回关键字实参。

    用 ``getattr`` 而**不是** ``model_dump()``：契约型参数（如 `ContentIR`）必须原样
    以模型实例传给实现函数，``model_dump()`` 会把它们降级成字典，于是"类型对了、值也在、
    但下游按属性取值全部 AttributeError"——这类错在参数校验阶段完全看不出来。
    """
    return {param.name: getattr(args, param.name) for param in spec.params}


def failure_of(error: EngineError, *, tool: str, retryable: bool | None = None) -> ToolFailure:
    """内核异常 → 结构化失败。``EngineError.fatal`` 默认映射到 ``retryable`` 的相反值。"""
    return ToolFailure(
        code=error.code,
        message=error.message,
        retryable=(not error.fatal) if retryable is None else retryable,
        where=dict(error.where),
        remediation=list(error.remediation),
    )


def as_args(payload: Mapping[str, Any] | FormatModel | None) -> Mapping[str, Any]:
    """把调用方给的参数归一成字典（``FormatModel`` 走 ``model_dump``）。"""
    if payload is None:
        return {}
    if isinstance(payload, FormatModel):
        return payload.model_dump()
    return payload
