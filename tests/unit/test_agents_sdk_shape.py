"""对**真** ``openai-agents`` 的形状断言（DD-12.4 的适配层前提 / NFR-18）。

为什么单独一个文件
------------------
`test_agents_adapter.py` 刻意全用注入的假 SDK：本项目要求"没模型也要能跑"（验收 39），
测试结论因此必须与开发机的安装状态无关。代价是**假 SDK 是照着自己的假设手写的**——
它能证明"适配层的分支对不对"，证明不了"假设本身对不对"。

两次同类的错都出在这个盲区，且都不是分支写错，而是**形状猜错**：

* ``builder._refreshed`` 按 ``step.name`` 访问 ``StepResult``（真字段是 ``step.step``）；
* ``_usage()`` 按 ``result.usage`` 取 token（真路径是 ``result.raw_responses[i].usage``，
  ``RunResult`` 连 ``usage`` 属性都没有）——于是 token 归集恒为空，
  而"没有数据"与"模型不花钱"在报告里长得一样（NFR-18 静默失效）。

本文件的断言针对**真模块**：没装 ``openai-agents`` 时整文件 skip（内核本来就不依赖它），
装了才跑。不做网络请求（只构造对象 / 只调离线注入的 runner）。
**升级 SDK 之后这些用例若失败，正是要提醒"形状变了，去看适配层"。**
"""

from __future__ import annotations

import inspect
import types
from typing import Any

import pytest

from docfidelity.agents.routing import DecisionRequest
from docfidelity.agents.sdk_adapter import NO_AUTH_PLACEHOLDER, OpenAIAgentsAdapter, _usage

pytestmark = pytest.mark.unit

agents = pytest.importorskip("agents", reason="形状断言只在装了 openai-agents 时有意义")


def _response(input_tokens: int, output_tokens: int) -> Any:
    """一个**真的** ``ModelResponse``（usage 是 ``agents.Usage``，不是替身）。"""
    return agents.ModelResponse(
        output=[],
        usage=agents.Usage(input_tokens=input_tokens, output_tokens=output_tokens),
        response_id=None,
    )


def _run_result(raw_responses: list[Any]) -> Any:
    """一个**真的** ``RunResult``。

    构造它需要把全部必填参数补齐——这正是本文件的价值所在：SDK 增删必填字段时，
    这里会以"这条断言炸了"的方式暴露，而不是让适配层在运行时悄悄取到 ``None``。
    """
    return agents.RunResult(
        input="prompt",
        new_items=[],
        raw_responses=raw_responses,
        final_output=None,
        input_guardrail_results=[],
        output_guardrail_results=[],
        tool_input_guardrail_results=[],
        tool_output_guardrail_results=[],
        context_wrapper=agents.RunContextWrapper(context=None),
        _last_agent=agents.Agent(name="probe", instructions="只回答一个问题"),
    )


# ------------------------------------------------------------------ NFR-18
def test_usage_lives_on_raw_responses_and_not_on_the_run_result() -> None:
    """`_usage()` 的取数路径必须与真 SDK 一致。"""
    assert "raw_responses" in inspect.signature(agents.RunResult.__init__).parameters
    assert not hasattr(
        agents.RunResult, "usage"
    ), "RunResult 若真的长了 usage 属性，这里要重新评估取数口径（raw_responses 仍是权威来源）"

    result = _run_result([_response(120, 8)])

    assert _usage(result, "input_tokens") == 120
    assert _usage(result, "output_tokens") == 8


def test_usage_sums_over_every_model_call_in_one_run() -> None:
    """一次 run 可能问多次模型（重试 / 多跳），成本归集是求和。"""
    result = _run_result([_response(100, 10), _response(150, 20)])

    assert _usage(result, "input_tokens") == 250
    assert _usage(result, "output_tokens") == 30


def test_usage_is_none_when_no_model_call_happened() -> None:
    """没有调用就留空——不编 0，否则"没有依据"与"真的没花钱"分不开。"""
    assert _usage(_run_result([]), "input_tokens") is None


# --------------------------------------------------------------- 端点注入
def test_the_sdk_needs_an_explicit_model_or_it_uses_its_own_endpoint() -> None:
    """这条断言是 `_model_for` 存在的理由。

    ``Agent(model=None)``（SDK 缺省）不是"用你配的端点"，而是"用 SDK 自己的默认模型
    与默认端点"。故适配层**必须**显式注入，否则请求会真的发出去——只是发到了别人家。
    """
    plain = agents.Agent(name="probe", instructions="只回答一个问题")

    assert plain.model is None


def test_the_chat_completions_model_carries_the_endpoint_we_inject() -> None:
    """适配层建出的模型对象，真 SDK 认，且端点真的在它身上。

    走 chat-completions 形态是刻意的：SDK 默认用 Responses API，而 OpenAI 兼容的
    第三方推理服务（vLLM / SGLang / Ollama / 本项目演示网关）普遍只实现
    ``/chat/completions``。
    """
    from openai import AsyncOpenAI

    adapter = OpenAIAgentsAdapter(
        model="m-1", base_url="http://127.0.0.1:9/v1", api_key="sk-shape-test"
    )
    captured: dict[str, Any] = {}

    def runner(agent: Any, prompt: str) -> Any:
        captured["agent"] = agent
        return types.SimpleNamespace(final_output=None, raw_responses=[])

    adapter.runner = runner
    adapter.call(
        DecisionRequest(
            decision_point="dedup.semantic_duplicate", target="c1", candidates=["dup", "new"]
        ),
        None,
    )

    model = captured["agent"].model
    assert isinstance(model, agents.OpenAIChatCompletionsModel), (
        "必须走 chat-completions 形态；否则第三方网关会返回非 JSON 响应，"
        "SDK 内部再折成一句与病因无关的 AttributeError"
    )
    assert model.model == "m-1"
    # `_client` 是私有属性：SDK 没给公开访问器，而"端点有没有真的进去"只有它能回答。
    client = model._client
    assert isinstance(client, AsyncOpenAI)
    assert str(client.base_url).startswith("http://127.0.0.1:9")


# ----------------------------------------------------------- 密钥的两条边界
def test_a_missing_key_never_inherits_the_process_openai_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """没配密钥时**不得**让 SDK 去读 ``OPENAI_API_KEY``（NFR-21 的邻居：别把请求发错地方）。

    这是 ``NO_AUTH_PLACEHOLDER`` 存在的理由：本地推理服务通常不校验密钥，
    而 openai SDK 把 ``api_key=None`` 解释成"去读环境变量"。于是一个没配密钥的部署
    会因为开发机上恰好存在 ``OPENAI_API_KEY`` 而连到另一个服务——沿途没有任何提示。
    """
    monkeypatch.setenv("OPENAI_API_KEY", "sk-someone-else")
    adapter = OpenAIAgentsAdapter(model="m-1", base_url="http://127.0.0.1:9/v1")

    client = adapter._client()

    assert client.api_key == NO_AUTH_PLACEHOLDER
    assert client.api_key != "sk-someone-else"


def test_the_placeholder_is_needed_because_none_means_read_the_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """上一条的**反证**：``api_key=None`` 确实会被 SDK 解释成"读环境变量"。

    如果哪天 SDK 不再这么做，这条会失败——那正是"占位串可以去掉"的信号，
    而不是"测试写错了"。
    """
    from openai import AsyncOpenAI

    monkeypatch.setenv("OPENAI_API_KEY", "sk-someone-else")

    client = AsyncOpenAI(base_url="http://127.0.0.1:9/v1", api_key=None)

    assert client.api_key == "sk-someone-else"


# ------------------------------------------------------------- trace 的默认值
def test_the_sdk_exports_traces_unless_we_say_otherwise() -> None:
    """``RunConfig()`` 的缺省是 **tracing 开**——这就是适配层必须显式关掉它的理由。

    SDK 的导出目标不是我们配的端点，而是它自己的追踪服务；对"数据边界只有一个出口"
    的部署而言，那是一次安静的外呼（DD-13.7 / NFR-21）。
    """
    assert agents.RunConfig().tracing_disabled is False
    assert agents.RunConfig(tracing_disabled=True).tracing_disabled is True


# ----------------------------------------------------------- 结构化输出的两条路
def test_local_mode_agent_carries_the_shape_and_asks_for_json_object() -> None:
    """本地模式的三个落点必须**真的落到 SDK 对象上**（而不是只在我们自己的字典里）。

    默认模式下 SDK 看到的应当正好相反于原生模式：``output_type`` 为空（于是请求里
    不会出现 ``response_format=json_schema``）、形状在 ``instructions`` 里、
    ``json_object`` 在 ``model_settings`` 里。
    """
    from docfidelity.agents.outputs import DuplicateJudgement

    adapter = OpenAIAgentsAdapter(model="m-1", base_url="http://127.0.0.1:9/v1")
    captured: dict[str, Any] = {}

    def runner(agent: Any, prompt: str) -> Any:
        captured["agent"] = agent
        return types.SimpleNamespace(final_output=None, raw_responses=[])

    adapter.runner = runner
    adapter.call(
        DecisionRequest(decision_point="dedup.semantic_duplicate", target="c1"),
        DuplicateJudgement,
    )

    agent = captured["agent"]
    assert agent.output_type is None
    assert "is_duplicate" in agent.instructions
    assert isinstance(agent.model_settings, agents.ModelSettings)
    assert agent.model_settings.extra_args == {"response_format": {"type": "json_object"}}


def test_native_and_local_modes_cannot_ride_the_same_request() -> None:
    """两个模式**互斥**的机制证明，也是 `_model_settings` 文档里那句断言的依据。

    ``convert_response_format`` 对"有 schema"给出 ``json_schema``，对"无 schema"给出
    ``omit``；SDK 随后只在**键未被 ``omit``** 时才认为 ``extra_args`` 冲突。故：
    交给 SDK ``output_type`` 时再塞 ``json_object`` 会当场抛
    ``got multiple values for keyword argument``——这就是"原生模式不能带 model_settings"
    的原因，也解释了为什么本地模式必须**刻意不传** ``output_type``。
    """
    from agents.agent_output import AgentOutputSchema
    from agents.models.chatcmpl_converter import Converter
    from openai import omit

    from docfidelity.agents.outputs import DuplicateJudgement

    native = Converter.convert_response_format(AgentOutputSchema(DuplicateJudgement))
    assert native["type"] == "json_schema"
    assert Converter.convert_response_format(None) is omit
