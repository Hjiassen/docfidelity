"""薄适配层：隔离 openai-agents（DD-12.4 / 需求 5.2.4 硬约束 2）。

"换模型、换版本、换推理服务只允许改 instructions 与适配层，不得触碰契约、渲染与门禁"——
这句话的可执行形式就是本模块：**全仓库唯一 import ``agents`` 的地方**。
只要守住这一条，"升级模型"就永远是一个局部改动。

四条落地取舍
------------
1. **惰性 import**。``agents`` 是模型 extra 的依赖，但"模型不可用必须可运行"（验收 39）
   要求内核在没有它、没有推理服务的机器上照常工作。故 import 发生在第一次调用时，
   失败被折成 ``AdapterOutcome(ok=False)``，而不是让导入本模块直接炸。
2. **``available()`` 是显式探针**，不是"试一下看报不报错"。路由表要在**决策之前**
   就知道该不该走模型路径；靠异常探测会让"没有模型"表现为一堆报错而不是一条干净的降级。
3. **``confidence`` 必须由接入方提供**。openai-agents 的返回值里没有置信度；
   本地推理服务的 logprob（配合受限解码）才是它的现实来源。适配层留一个注入点，
   **未提供时一律按不达标处理**——保守方向的默认，正是验收 39 要的"不静默采用"。
4. **密钥只进不出**：``describe()`` 里永不出现 key，日志同理（NFR-21）。
5. **端点显式注入**（`_model_for`）。``base_url`` / ``api_key`` 必须真的交给 SDK：
   ``Agent`` 的 ``model`` 缺省是 ``None``，此时 SDK 会退回它自己的默认模型与默认端点。
   即"配了端点却没交给它"的后果不是报错，而是**请求发到了别人家**，
   而本层的 ``available()`` 与 ``describe()`` 还在报"就绪"。
6. **trace 导出默认关闭**（`_run_config`）。SDK 缺省会把整轮运行（含提示词与模型输出）
   上报到它自己的追踪服务，那是**一条不在数据边界里的外呼**（DD-13.7 / NFR-21）：
   部署写着 ``local``、端点指着内网，而运行痕迹到了公网。要开就显式开。
7. **结构化输出默认在本地兜底**（`_build_agent` / `structured` 模块）。DD-12.4 把
   ``response_format=json_schema`` 定为第一手段，但**网关会把它静默忽略**（同文档
   第 1286 行已预判）。此时模型根本不知道要输出什么形状，症状是一句与病因无关的
   ``Invalid JSON when parsing model output``。故默认改走
   "``json_object`` + instructions 内嵌字段清单 + pydantic 本地校验"，
   把形状与合法性都拿回自己手里；端点确实支持 JSON Schema 时可用
   ``native_structured_output=True`` 换回 SDK 的原生路径。
8. **"调用失败"与"格式不合格"必须分开**（`_interpret`）。前者重试没有意义，
   后者有——而 ``AdapterOutcome`` 的 ``ok`` / ``schema_ok`` 就是为这个区分而设的
   （见 `routing.AdapterOutcome` 文档）。把两者折成一个 ``ok=False``，
   重试时喂给模型的反馈就变成一句与病因无关的话（第 7 条的旧症状正是如此）。
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Final

from docfidelity.contracts.base import FormatModel
from docfidelity.contracts.errors import ErrorCode

from . import structured
from .outputs import AgentName, chosen_label
from .routing import AdapterOutcome, DecisionRequest

__all__ = [
    "GRAMMAR_BACKENDS",
    "NO_AUTH_PLACEHOLDER",
    "GrammarCapability",
    "NullAdapter",
    "OpenAIAgentsAdapter",
    "ScriptedAdapter",
    "probe_grammar",
]

#: 本机可用的受限解码后端（需求 5.2.5：受限解码由推理服务侧提供）。
#: 探测的语义是"**当前进程**有没有能力做受限解码"，而不是"远端服务有没有开"——
#: 后者只能由真实请求证明，故服务端可以注入自己的 prober 覆盖默认实现。
GRAMMAR_BACKENDS: Final[tuple[str, ...]] = ("xgrammar", "outlines")

#: ``api_key`` 为空时给 SDK 的占位串（**不是密钥**）。
#:
#: openai SDK 把 ``api_key=None`` 解释成"去读环境变量 ``OPENAI_API_KEY``"，
#: 读不到才报错。本地推理服务（vLLM / Ollama / llama.cpp）通常不校验密钥，
#: 于是那个回退是纯粹的有害动作：一个**没配密钥**的部署会因为开发机上恰好存在
#: ``OPENAI_API_KEY`` 而把请求发到别处去，且沿途没有任何一处会提示这件事。
#: 传占位串是把那条回退堵死——要不要认证由部署配置说了算。
NO_AUTH_PLACEHOLDER: Final = "not-needed"


class GrammarCapability(FormatModel):
    """受限解码能力探测结果（DD-12.4：探测失败则标记 ``grammar_disabled``）。"""

    supported: bool
    backend: str | None = None
    detail: str = ""


def probe_grammar(*, prober: Callable[[], GrammarCapability] | None = None) -> GrammarCapability:
    """探测受限解码能力。默认探本机是否装了已知后端。

    探测失败**不是错误**：它只改变降级路径（"校验 → 重试 ≤ 2 → 规则兜底"），
    而那条路径本来就必须长期存在（DD-12.4 的兜底行）。
    """
    if prober is not None:
        return prober()
    found: list[str] = []
    for name in GRAMMAR_BACKENDS:
        try:
            __import__(name)
        except ImportError:
            continue
        found.append(name)
    if found:
        return GrammarCapability(
            supported=True, backend=found[0], detail=f"检出受限解码后端：{', '.join(found)}"
        )
    return GrammarCapability(
        supported=False,
        backend=None,
        detail="未检出受限解码后端，标记 grammar_disabled 并降级为「校验 + 重试 + 规则兜底」",
    )


class NullAdapter:
    """纯确定性模式（``DF_MODEL_ENABLED=false`` / 未配置模型）。

    它**不是**一个"没接好的适配层"，而是一个正式实现：模型总开关关闭时全链路走规则，
    产出仍完整（验收 39）、token 用量为 0（NFR-18）。
    """

    def available(self) -> bool:
        return False

    def describe(self) -> dict[str, Any]:
        return {
            "kind": "null",
            "available": False,
            "model": None,
            "grammar_supported": None,
            "detail": "纯确定性模式：模型关闭，全链路走规则默认值",
        }

    def call(
        self, request: DecisionRequest, output_type: type[FormatModel] | None
    ) -> AdapterOutcome:
        return AdapterOutcome(
            ok=False,
            schema_ok=False,
            error=ErrorCode.MODEL_UNAVAILABLE,
            detail="模型未启用（纯确定性模式），按规则默认值处理",
        )


@dataclass
class ScriptedAdapter:
    """按决策点预置返回的适配层：单测替身 + **离线回放**（需求 10.9 / NFR-18）。

    回放能力不是测试专用品：外部使用者不愿为验证功能承担模型 API 费用，
    故"把一次真实调用的输出录下来、之后离线重放"是开源版的必要退路。
    """

    script: Mapping[str, AdapterOutcome] = field(default_factory=dict)
    available_flag: bool = True
    model_name: str = "scripted"
    calls: list[DecisionRequest] = field(default_factory=list)

    def available(self) -> bool:
        return self.available_flag

    def describe(self) -> dict[str, Any]:
        return {
            "kind": "scripted",
            "available": self.available_flag,
            "model": self.model_name,
            "scripted_points": sorted(self.script),
        }

    def call(
        self, request: DecisionRequest, output_type: type[FormatModel] | None
    ) -> AdapterOutcome:
        self.calls.append(request)
        outcome = self.script.get(request.decision_point)
        if outcome is None:
            return AdapterOutcome(
                ok=False,
                schema_ok=False,
                error=ErrorCode.MODEL_UNAVAILABLE,
                detail=f"脚本未覆盖决策点 {request.decision_point}",
            )
        return outcome.model_copy()


#: 从 SDK 结果里取置信度的钩子。**默认返回 None**（= 不达标）。
#: 现实来源是本地推理服务的 logprob；把它做成注入点而不是"猜一个数"，
#: 是因为猜出来的置信度会让低置信项静默通过。
ConfidenceHook = Callable[[Any], float | None]

#: 构造模型客户端（chat-completions 形态）的钩子：``(base_url, api_key) -> client``。
#: 缺省惰性 import ``openai`` 建 ``AsyncOpenAI``；注入假对象即可完全离线地测适配层，
#: **连 ``openai`` 都不必安装**（见本模块测试的模块文档）。
ClientFactory = Callable[[str, str | None], Any]


@dataclass
class OpenAIAgentsAdapter:
    """openai-agents 的薄封装（OpenAI 兼容端点 → vLLM / SGLang / Ollama / llama.cpp）。

    ``runner`` 可以注入（测试传一个假 runner 即可覆盖真实调用链），
    ``confidence_of`` 见模块文档第 3 条，``client_factory`` 见第 5 条。
    """

    model: str
    base_url: str | None = None
    api_key: str | None = field(default=None, repr=False)
    confidence_of: ConfidenceHook | None = None
    runner: Any = None
    grammar: GrammarCapability | None = None
    client_factory: ClientFactory | None = None
    #: 是否允许 SDK 把运行痕迹导出到它自己的追踪服务。**默认 False**（见模块文档第 6 条）。
    tracing_enabled: bool = False
    #: 是否把结构化输出交给 SDK 的原生路径（``output_type`` → ``response_format=json_schema``）。
    #: **默认 False** = 走本地兜底（``json_object`` + instructions 内嵌形状 + pydantic 自校验）。
    #: 换到确实支持 JSON Schema 的端点时置 True 可拿回服务端强约束（见模块文档第 7 条）。
    native_structured_output: bool = False
    _sdk: Any = field(default=None, init=False, repr=False)
    _model_objects: dict[str, Any] = field(default_factory=dict, init=False, repr=False)
    _client_object: Any = field(default=None, init=False, repr=False)

    # -------------------------------------------------------------- 探测
    def _load_sdk(self) -> tuple[Any, str | None]:
        """惰性载入 SDK。返回 ``(模块, 失败原因)``，失败原因进 ``describe`` 让人查得到。"""
        if self._sdk is not None:
            return self._sdk, None
        try:
            import agents
        except ImportError as exc:
            return None, f"openai-agents 未安装（缺少 {exc.name}）"
        self._sdk = agents
        return agents, None

    def available(self) -> bool:
        sdk, _ = self._load_sdk()
        return sdk is not None and bool(self.base_url)

    def describe(self) -> dict[str, Any]:
        sdk, reason = self._load_sdk()
        grammar = self.grammar or probe_grammar()
        return {
            "kind": "openai_agents",
            "available": sdk is not None and bool(self.base_url),
            "model": self.model,
            "base_url": self.base_url,
            "detail": reason or "就绪",
            "structured_output": "native" if self.native_structured_output else "local",
            "grammar_supported": grammar.supported,
            "grammar_backend": grammar.backend,
            "grammar_detail": grammar.detail,
        }

    # -------------------------------------------------------------- 调用
    def call(
        self, request: DecisionRequest, output_type: type[FormatModel] | None
    ) -> AdapterOutcome:
        sdk, reason = self._load_sdk()
        if sdk is None or not self.base_url:
            return AdapterOutcome(
                ok=False,
                schema_ok=False,
                error=ErrorCode.MODEL_UNAVAILABLE,
                detail=reason or "未配置模型端点（base_url 为空）",
            )

        started = time.perf_counter()
        model = request.model or self.model
        try:
            agent = self._build_agent(sdk, request, output_type)
            result = self._run(sdk, agent, _prompt(request))
        except Exception as exc:
            return AdapterOutcome(
                ok=False,
                schema_ok=False,
                error=ErrorCode.MODEL_UNAVAILABLE,
                model=model,
                latency_ms=_ms(started),
                detail=f"模型调用失败（{type(exc).__name__}）：模型服务与构建 worker 分离部署，"
                "此处按降级处理",
            )

        final = getattr(result, "final_output", None)
        payload, ok, schema_ok, failure = _interpret(
            final, output_type, native=self.native_structured_output
        )
        return AdapterOutcome(
            ok=ok,
            payload=payload,
            chosen=_chosen_for(output_type, payload, final),
            confidence=None if self.confidence_of is None else self.confidence_of(result),
            schema_ok=schema_ok,
            model=model,
            tokens_in=_usage(result, "input_tokens"),
            tokens_out=_usage(result, "output_tokens"),
            latency_ms=_ms(started),
            detail=failure,
        )

    def _build_agent(
        self, sdk: Any, request: DecisionRequest, output_type: type[FormatModel] | None
    ) -> Any:
        """按决策点构造一个 Agent。

        **不设 handoff、不注册工具**：本层的职责是"回答一个问题"，
        编排与工具执行由 `agents.catalog` 的规格与调用方决定。把两者揉在一起，
        会让"换一个决策点"变成"改一个多跳 Agent 的提示词"——而那正是需求 5.2.1
        点名的小模型短板（多跳推理弱）。

        ``model`` 必须显式给：缺省是 ``None``，SDK 会拿它自己的默认模型 + 默认端点
        去发请求（见 :meth:`_model_for`）。

        输出形状走哪条路由 ``native_structured_output`` 决定（模块文档第 7 条）：
        默认**本地模式**——形状写进 instructions（机器从契约派生，不会漂移）、
        请求里只要 ``json_object``、合法性在本地校验；原生模式则一切都交给 SDK。
        """
        from .instructions import agent_instructions

        owner = _agent_of(request.decision_point)
        name = request.decision_point
        instructions = (
            agent_instructions(owner)
            if owner is not None
            else "只回答当前这一个问题，只输出结构化结果。"
        )
        kwargs: dict[str, Any] = {
            "name": name,
            "model": self._model_for(sdk, request.model or self.model),
        }
        if output_type is not None:
            if self.native_structured_output:
                kwargs["output_type"] = output_type
            else:
                kwargs["instructions"] = (
                    f"{instructions}\n\n{structured.output_instructions(output_type)}"
                )
                kwargs["model_settings"] = self._model_settings(sdk)
        kwargs.setdefault("instructions", instructions)
        return sdk.Agent(**kwargs)

    def _model_settings(self, sdk: Any) -> Any:
        """本地模式下的请求参数：只向服务端要"**是一个 JSON 对象**"，不要形状。

        ``response_format={"type": "json_object"}`` 是所有 OpenAI 兼容服务的共同底线
        （比 ``json_schema`` 普及得多）。它保证语法、不保证形状——形状由 instructions
        里的字段清单负责，合法性由 `structured.parse_structured` 负责。三者叠加才是
        DD-12.4 那条降级路径的完整形态；少任何一环，"模型没按 schema 作答"就会
        重新变成一句无法定位的报错。

        走 ``extra_args`` 而**不是** ``extra_body``：SDK 把它 ``update`` 进
        ``create_kwargs``，且只在**键冲突**时才抛
        ``got multiple values for keyword argument``。本模式刻意不给 ``output_type``，
        于是 SDK 侧的 ``response_format`` 是 ``omit``（`chatcmpl_converter` 对
        "无 schema / 纯文本"返回 ``omit``），不冲突——这正是两个模式不能混用的原因。
        """
        return sdk.ModelSettings(extra_args={"response_format": {"type": "json_object"}})

    def _model_for(self, sdk: Any, model: str) -> Any:
        """把端点配置折成一个 SDK 模型对象（**chat-completions 形态**），并缓存。

        两个"必须显式"的理由都是实测出来的：

        * **必须显式给模型。** ``Agent(model=None)`` 不是"用你配的端点"，
          而是"用 SDK 自己的默认模型与 ``api.openai.com``"。漏掉这一句的症状极隐蔽：
          ``available()`` 报就绪（端点确实配了）、请求也确实发出去了，只是发错了地方。
        * **必须走 chat completions。** SDK 默认用 Responses API，而 OpenAI 兼容的
          第三方推理服务普遍只实现 ``/chat/completions``。走错形态的表现是网关返回
          一个非 JSON 的响应体（HTML 错误页也算），SDK 内部在
          ``isinstance(ret, ChatCompletion)`` 那里折成一句与病因无关的
          ``AttributeError``。

        缓存的是模型对象，故 SDK 客户端只建一次——适配层实例化的次数不随作业数增长。
        """
        if model not in self._model_objects:
            self._model_objects[model] = sdk.OpenAIChatCompletionsModel(
                model=model, openai_client=self._client()
            )
        return self._model_objects[model]

    def _client(self) -> Any:
        """建模型客户端。惰性 import ``openai``（它是 ``openai-agents`` 的依赖）。"""
        if self._client_object is not None:
            return self._client_object
        if self.client_factory is not None:
            self._client_object = self.client_factory(self.base_url or "", self.api_key)
        else:
            from openai import AsyncOpenAI

            self._client_object = AsyncOpenAI(
                base_url=self.base_url, api_key=self.api_key or NO_AUTH_PLACEHOLDER
            )
        return self._client_object

    def _run(self, sdk: Any, agent: Any, prompt: str) -> Any:
        if self.runner is not None:
            return self.runner(agent, prompt)
        return sdk.Runner.run_sync(agent, prompt, run_config=self._run_config(sdk))

    def _run_config(self, sdk: Any) -> Any:
        """每次运行都带上的配置。目前只有一条：**关掉 trace 导出**。

        SDK 的缺省是"开"，它会把整轮运行（提示词、模型输出、工具调用）上报到它自己的
        追踪服务。对本项目来说那是一次**不在数据边界里的外呼**（DD-13.7 / NFR-21）：
        部署形态写着 ``local``、``model_base_url`` 指着内网，而运行痕迹到了公网。
        它不报错、不影响产出，只是安静地多开了一条出口——正是"数据边界"要排除的形态。
        （SDK 在没配它自己的密钥时会跳过导出并打一行提示，所以这条默认值在演示环境里
        看不出来；一旦那台机器的环境变量里有 ``OPENAI_API_KEY``，导出就真的发生了。）

        要开就显式开：``tracing_enabled=True`` 返回 ``None``（= 用 SDK 缺省）。
        那应当是部署方的知情选择，而不是我们的缺省值。
        """
        if self.tracing_enabled:
            return None
        return sdk.RunConfig(tracing_disabled=True)


def _agent_of(point_id: str) -> AgentName | None:
    """决策点 → 归属 Agent（用于取 instructions）。未登记返回 ``None``。

    返回类型写 ``AgentName | None`` 而不是 ``str | None``：下游 `agent_instructions`
    只接受五角色的字面量，用 `str` 会让"多了一个没登记的角色"在提示词层溜过去。
    """
    from .catalog import decision_point

    return decision_point(point_id).owner_agent


def _prompt(request: DecisionRequest) -> str:
    """把请求折成一份短提示。

    刻意保持扁平：候选清单直接列出，上下文按预算裁剪由调用方完成（NFR-25）。
    这里不做二次总结——总结会丢掉候选里的原文，而候选正是模型唯一该选的东西。
    """
    lines = [f"决策点：{request.decision_point}", f"对象：{request.target or '（未指定）'}"]
    if request.candidates:
        lines.append("只能在以下候选中选择：" + "、".join(request.candidates))
    if request.inputs:
        lines.append("输入：" + json.dumps(request.inputs, ensure_ascii=False, sort_keys=True))
    if request.context:
        lines.append("上下文：" + json.dumps(request.context, ensure_ascii=False, sort_keys=True))
    if request.feedback:
        lines.append("上次失败原因：" + "；".join(request.feedback))
    return "\n".join(lines)


def _interpret(
    final: Any, output_type: type[FormatModel] | None, *, native: bool
) -> tuple[dict[str, Any] | None, bool, bool, str]:
    """把 SDK 的终稿折成 ``(载荷, 调用成立吗, 格式合格吗, 原因)``。

    **三态而不是两态**，是 `routing.AdapterOutcome` 的既有区分：``ok`` 说"这次调用
    拿到了东西"，``schema_ok`` 说"拿到的东西符合输出契约"。二者混成一个
    ``ok=False``（本次改造前的行为）的后果是 `routing._failure_hint` 走错分支，
    把"模型服务与构建 worker 分离部署"这句与病因无关的话当成重试反馈——
    而重试反馈正是"校验 → 重试 ≤2"这条降级路唯一能起作用的地方。

    分支覆盖三种终稿形态：

    * ``None`` —— 模型没给终稿（连文本都没有），调用不成立。
    * ``str`` —— **本地模式**的正常形态：抽取 JSON 再本地校验；拿不到就报
      ``ok=True, schema_ok=False``（格式问题，重试有意义）。该点若没有输出契约，
      文字回复不能当结构化结论用，故仍然是不成立。
    * 其它（pydantic 模型 / 映射）—— 原生模式或旧版 SDK 的形态，直接序列化。
    """
    if final is None:
        return None, False, False, "模型没有返回终稿（既不是文本也不是结构化输出）"
    if isinstance(final, str):
        if output_type is None:
            return None, False, False, "该决策点没有输出契约，模型的文字回复不能当结构化结论"
        if native:
            return None, True, False, "SDK 未按 output_type 返回结构化输出（应已由 SDK 解析）"
        parsed = structured.parse_structured(output_type, final)
        if parsed.ok:
            return parsed.payload, True, True, ""
        return None, True, False, parsed.reason
    payload = _payload_of(final)
    if payload is None:
        return None, False, False, "SDK 返回的终稿无法序列化成载荷（形状不认识）"
    return payload, True, True, ""


def _payload_of(final: Any) -> dict[str, Any] | None:
    """把**已经结构化的**终稿（原生模式）折成字典。"""
    if final is None:
        return None
    dump = getattr(final, "model_dump", None)
    if callable(dump):
        payload = dump()
        return payload if isinstance(payload, dict) else None
    if isinstance(final, Mapping):
        return {str(key): value for key, value in final.items()}
    return None


def _chosen_for(output_type: type[FormatModel] | None, payload: Any, final: Any) -> str | None:
    """取"这次选了什么"，**本项目口径优先，SDK 约定兜底**。

    两个口径都要，因为它们覆盖的是不同的东西：

    * `outputs.chosen_label` 认的是**本项目自己的决策点级输出**（``is_duplicate`` /
      ``chapter_path`` / ``unit_id`` …）。它是主口径——本项目五个模型档点没有一个用
      ``choice`` / ``chosen`` / ``value`` 作字段名，只走 :func:`_chosen_of` 的话
      ``Decision.chosen`` **恒为空**（"模型选了什么"在裁决队列里就永远看不见）。
    * `_chosen_of` 认的是 SDK 约定的字段名。它只在原生结构化输出路径（``output_type``
      交给 SDK）与实际返回候选集约定的适配层上成立，故留作兜底而不是删掉。

    两者都不认时返回 ``None``——不编占位值：``Decision.chosen`` 为空是"该点没有选择
    这个概念"的合法表示，编一个假值会让它看起来像是模型的结论。
    """
    if isinstance(payload, Mapping):
        label = chosen_label(output_type, payload)
        if label is not None:
            return label
    # 本项目口径没给出取值时退回 SDK 约定字段名：先看规范化载荷，再看**原始**输出
    # （两者都要看——``final`` 在原生路径下才是那个带着 ``choice`` 的对象，
    # 而载荷有可能是合法但为空的 ``{}``；只看其中一个会漏掉另一条路）。
    for source in (payload, final):
        label = _chosen_of(source)
        if label is not None:
            return label
    return None


def _chosen_of(final: Any) -> str | None:
    """从结构化输出里取"这次选了什么"。

    只认少数几个约定字段名（``choice`` / ``chosen`` / ``value``）：泛化地"取第一个
    字符串字段"会在多字段输出上取错，而错的取值会一路写进报告与追溯链。

    兼容映射型输出（不同 SDK 版本返回 pydantic 模型或 dict）：漏掉这一支的表现是
    "结论的载荷里明明写着他选了什么，留痕上却是空"，而空取值在裁决队列里看起来
    和"模型没给结论"一模一样。
    """
    for attr in ("choice", "chosen", "value"):
        value = final.get(attr) if isinstance(final, Mapping) else getattr(final, attr, None)
        if isinstance(value, str) and value:
            return value
    return None


def _usage(result: Any, attr: str) -> int | None:
    """从 SDK 的 usage 里取 token 数（NFR-18 成本归集）。取不到就留空，不编 0。

    真实路径是 ``result.raw_responses[i].usage``：``RunResult`` **没有** ``usage``
    属性（agent 0.22.2 实测 ``hasattr`` 为假）。早先按 ``result.usage`` 取的那一版
    因此**恒返回 None**——成本归集静默失效，而"没有数据"与"模型不花钱"在报告里
    长得一模一样（两者都是 0 与空）。这是单测用**手写假结果**（照着自己的假设写了
    ``.usage``）时必然漏掉的一类错。

    一次运行可能发生多次模型调用，故**求和**；一条 usage 都取不到时留空，
    让"这个数字没有依据"保持可见，而不是用 0 冒充。
    """
    total = 0
    seen = False
    for response in getattr(result, "raw_responses", None) or ():
        value = getattr(getattr(response, "usage", None), attr, None)
        if isinstance(value, int):
            total += value
            seen = True
    return total if seen else None


def _ms(started: float) -> int:
    return max(0, int((time.perf_counter() - started) * 1000))
