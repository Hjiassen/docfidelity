"""薄适配层：隔离 openai-agents（DD-12.4 / 需求 5.2.4 硬约束 2 / NFR-21 / 验收 39）。

本模块存在的理由是四条硬约束，测试逐条盯住：

1. **惰性 import** —— 没装 openai-agents 的机器上，导入 `docfidelity.agents` 与调用
   适配层都必须照常工作（内核不因缺模型而不可用）。
2. **`available()` 是显式探针** —— 路由表要在**决策之前**就知道该不该走模型路径。
3. **置信度必须由接入方提供，未提供即不达标** —— 这一条靠"默认返回 None"落地，
   而 None 在路由层被当成 0.0；测在适配层就是"没有钩子时 confidence 必须是 None"。
4. **密钥只进不出** —— `describe()` 会被写进日志与界面。

`agents` 包**不一定装在跑测试的机器上**（本项目刻意保持"没模型也能跑"），故两个分支
都用注入来构造：`sys.modules["agents"] = None` 强制 ImportError，
注入 `ModuleType` 则构造"已装"的场景。这样测试结论与开发机的安装状态无关。

**代价要说清楚**：注入的假 SDK 是**照着自己的假设手写**的，所以它能证明"适配层的分支
对不对"，证明不了"假设本身对不对"。`_usage()` 按 ``result.usage`` 取 token 那一版就是
这么漏过去的（`RunResult` 根本没有这个属性，token 归集恒为空，而这里的假结果**手写了**
``.usage``）。故真 SDK 的形状断言单独放在 `test_agents_sdk_shape.py`——两个文件的分工
是"分支逻辑 vs 形状假设"。
"""

from __future__ import annotations

import json
import sys
import types
from typing import Any

import pytest

from docfidelity.agents.routing import AdapterOutcome, DecisionRequest
from docfidelity.agents.sdk_adapter import (
    GRAMMAR_BACKENDS,
    GrammarCapability,
    NullAdapter,
    OpenAIAgentsAdapter,
    ScriptedAdapter,
    probe_grammar,
)
from docfidelity.contracts.errors import ErrorCode

pytestmark = pytest.mark.unit


# ----------------------------------------------------------------- 替身
class _FakeAgent:
    """只记下构造参数：用来证明适配层**不设 handoff、不注册工具**，且**显式给了模型**。"""

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs


class _FakeModel:
    """记下 `OpenAIChatCompletionsModel` 的构造参数（含我们注入的客户端）。"""

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs


class _Client:
    """客户端替身：只为断言"端点真的交给了 SDK"，不发任何请求。"""

    def __init__(self, base_url: str, api_key: str | None) -> None:
        self.base_url = base_url
        self.api_key = api_key


def _offline_client(base_url: str, api_key: str | None) -> Any:
    return _Client(base_url, api_key)


class _Final:
    """结构化输出的最小替身：**既有属性、也能 model_dump**（真实 pydantic 模型即如此）。"""

    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = dict(payload)
        for key, value in payload.items():
            object.__setattr__(self, key, value)

    def model_dump(self) -> dict[str, Any]:
        return dict(self._payload)


class _Usage:
    def __init__(
        self, *, input_tokens: int | None = None, output_tokens: int | None = None
    ) -> None:
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


class _Response:
    """一次模型调用的返回：usage 挂在**它**身上（真 `ModelResponse` 即如此）。"""

    def __init__(
        self, *, input_tokens: int | None = None, output_tokens: int | None = None
    ) -> None:
        self.usage = _Usage(input_tokens=input_tokens, output_tokens=output_tokens)


class _Result:
    """`RunResult` 的替身。

    **刻意不给它 `.usage`**：真 `RunResult` 没有这个属性，给了就等于替 SDK 编一个不存在的
    形状，让"按 result.usage 取 token"这种错在测试里看起来是对的（这正是它曾经漏过去的原因）。
    """

    def __init__(self, final: Any = None, raw_responses: Any = ()) -> None:
        self.final_output = final
        self.raw_responses = list(raw_responses)


class _RunConfig:
    """记下 `RunConfig` 的构造参数（用来断言 trace 默认被关掉）。"""

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.tracing_disabled = kwargs.get("tracing_disabled")


class _ModelSettings:
    """记下 `ModelSettings` 的构造参数（用来断言本地模式只要 json_object）。"""

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs


def _fake_sdk(*, result: Any = None, boom: BaseException | None = None) -> types.ModuleType:
    module = types.ModuleType("agents")
    module.Agent = _FakeAgent  # type: ignore[attr-defined]
    module.OpenAIChatCompletionsModel = _FakeModel  # type: ignore[attr-defined]
    module.RunConfig = _RunConfig  # type: ignore[attr-defined]
    module.ModelSettings = _ModelSettings  # type: ignore[attr-defined]

    def _runner(agent: Any, prompt: str, **kwargs: Any) -> Any:
        if boom is not None:
            raise boom
        _runner.last_kwargs = kwargs  # type: ignore[attr-defined]
        return result

    _runner.last_kwargs = {}  # type: ignore[attr-defined]
    module.Runner = types.SimpleNamespace(run_sync=_runner)  # type: ignore[attr-defined]
    return module


def _adapter(monkeypatch: pytest.MonkeyPatch, sdk: types.ModuleType | None, **kwargs: Any) -> Any:
    """构造适配层，并按 `sdk` 是否为 None 决定"装没装 openai-agents"。

    ``client_factory`` 缺省注入离线替身：本文件因此**连 ``openai`` 都不必安装**
    （它是 ``openai-agents`` 的依赖，而这里整个 ``agents`` 都是假的）。
    """
    monkeypatch.setitem(sys.modules, "agents", sdk)
    kwargs.setdefault("client_factory", _offline_client)
    return OpenAIAgentsAdapter(model="m-1", base_url="http://127.0.0.1:8000/v1", **kwargs)


def _request(point: str = "dedup.semantic_duplicate") -> DecisionRequest:
    return DecisionRequest(
        decision_point=point, target="c1", candidates=["dup", "new"], inputs={"path": "a.py"}
    )


# -------------------------------------------------------- 受限解码能力探测
def test_probe_grammar_uses_injected_prober_verbatim() -> None:
    """探测器的存在是为了让"服务端知道自己的推理服务"能覆盖本机探测。"""
    expected = GrammarCapability(supported=True, backend="custom", detail="服务端自报")
    assert probe_grammar(prober=lambda: expected) == expected


def test_probe_grammar_default_never_raises() -> None:
    """探测失败不是错误：它只改变降级路径（校验 → 重试 → 规则兜底）。"""
    capability = probe_grammar()
    assert isinstance(capability, GrammarCapability)
    assert capability.supported is False or capability.backend in GRAMMAR_BACKENDS
    assert capability.detail


# ------------------------------------------------------------- 纯确定性模式
def test_null_adapter_is_available_but_never_used() -> None:
    """`NullAdapter` 不是"没接好的适配层"，而是总开关关闭时的正式实现。"""
    adapter = NullAdapter()
    assert adapter.available() is False
    assert adapter.describe()["kind"] == "null"


def test_null_adapter_call_degrades_without_raising() -> None:
    outcome = NullAdapter().call(_request(), None)
    assert outcome.ok is False
    assert outcome.schema_ok is False
    assert outcome.error == ErrorCode.MODEL_UNAVAILABLE
    assert outcome.detail


# ------------------------------------------------------------- 脚本回放器
def test_scripted_adapter_reports_its_points() -> None:
    adapter = ScriptedAdapter(
        script={
            "placement.section_slot": AdapterOutcome(ok=True, confidence=0.9),
            "copy.generate": AdapterOutcome(ok=True, confidence=0.9),
        }
    )
    assert adapter.available() is True
    assert adapter.describe()["scripted_points"] == ["copy.generate", "placement.section_slot"]


def test_scripted_adapter_records_every_call() -> None:
    adapter = ScriptedAdapter(script={"copy.generate": AdapterOutcome(ok=True, confidence=0.9)})
    adapter.call(_request("copy.generate"), None)
    adapter.call(_request("copy.generate"), None)
    assert len(adapter.calls) == 2
    assert adapter.calls[0].decision_point == "copy.generate"


def test_scripted_adapter_uncovered_point_degrades() -> None:
    adapter = ScriptedAdapter(script={})
    outcome = adapter.call(_request("copy.generate"), None)
    assert outcome.ok is False
    assert outcome.error == ErrorCode.MODEL_UNAVAILABLE
    assert "copy.generate" in outcome.detail


def test_scripted_adapter_hands_out_copies() -> None:
    """回放的结论必须**每次都是新对象**：共享同一个可变载荷会让一处改写污染所有重放。"""
    scripted = AdapterOutcome(ok=True, confidence=0.9, payload={"k": 1})
    adapter = ScriptedAdapter(script={"copy.generate": scripted})
    first = adapter.call(_request("copy.generate"), None)
    assert first is not scripted
    first.payload = {"k": 2}
    assert adapter.call(_request("copy.generate"), None).payload == {"k": 1}


def test_scripted_adapter_can_report_unavailable() -> None:
    adapter = ScriptedAdapter(script={}, available_flag=False)
    assert adapter.available() is False


# ----------------------------------------------------- openai-agents 适配层
def test_adapter_unavailable_without_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """两个条件缺一不可：装了 SDK **且** 配了端点。只有模型名不算就绪。"""
    monkeypatch.setitem(sys.modules, "agents", _fake_sdk())
    adapter = OpenAIAgentsAdapter(model="m-1", base_url=None)
    assert adapter.available() is False
    assert adapter.call(_request(), None).error == ErrorCode.MODEL_UNAVAILABLE


def test_adapter_unavailable_without_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    """没装 openai-agents 时**不炸**，只在 describe 里说明差什么（惰性 import 的意义）。"""
    adapter = _adapter(monkeypatch, None)
    assert adapter.available() is False
    described = adapter.describe()
    assert "openai-agents 未安装" in described["detail"]
    assert described["available"] is False


def test_adapter_import_failure_is_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    """失败也要缓存：每次决策都重试一次 import，会让"没装"变成每次调用付一次代价。"""
    adapter = _adapter(monkeypatch, None)
    adapter.describe()
    assert adapter._sdk is None
    assert adapter.describe()["detail"]


def test_adapter_describe_never_leaks_the_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """NFR-21：describe 会被写进日志与界面，密钥只进不出。"""
    adapter = _adapter(monkeypatch, _fake_sdk(), api_key="sk-super-secret")
    described = adapter.describe()
    assert "api_key" not in {key.lower() for key in described}
    assert "sk-super-secret" not in repr(described)
    assert "sk-super-secret" not in adapter.describe().__str__()
    assert "sk-super-secret" not in repr(adapter)


def test_adapter_returns_structured_output(monkeypatch: pytest.MonkeyPatch) -> None:
    result = _Result(
        final=_Final({"choice": "dup", "reason": "同功能"}),
        raw_responses=[_Response(input_tokens=120, output_tokens=8)],
    )
    adapter = _adapter(monkeypatch, _fake_sdk(result=result))
    outcome = adapter.call(_request(), None)

    assert outcome.ok is True
    assert outcome.schema_ok is True
    assert outcome.payload == {"choice": "dup", "reason": "同功能"}
    assert outcome.chosen == "dup"
    assert outcome.model == "m-1"
    assert outcome.tokens_in == 120
    assert outcome.tokens_out == 8
    assert outcome.latency_ms is not None and outcome.latency_ms >= 0


def test_adapter_sums_tokens_across_model_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    """一次运行可能问多次模型（重试 / 多跳），成本归集要**求和**而不是只记最后一次。"""
    result = _Result(
        final=_Final({"choice": "dup"}),
        raw_responses=[
            _Response(input_tokens=100, output_tokens=10),
            _Response(input_tokens=150, output_tokens=20),
        ],
    )
    adapter = _adapter(monkeypatch, _fake_sdk(result=result))
    outcome = adapter.call(_request(), None)

    assert outcome.tokens_in == 250
    assert outcome.tokens_out == 30


def test_adapter_takes_tokens_from_raw_responses_not_from_the_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """token 挂在 ``raw_responses[i].usage`` 上（``RunResult`` 没有 ``usage``）。

    这条用例专门造一个"看起来更像真的"的假结果：把 usage 放在 ``result`` 上而
    ``raw_responses`` 为空。按旧口径（``result.usage``）会取到 999，按真口径取到 None。
    """
    result = _Result(final=_Final({"choice": "dup"}))
    result.usage = _Usage(input_tokens=999, output_tokens=999)  # 真实 SDK 上不存在的位置
    adapter = _adapter(monkeypatch, _fake_sdk(result=result))
    outcome = adapter.call(_request(), None)

    assert outcome.tokens_in is None and outcome.tokens_out is None


def test_adapter_default_mode_keeps_output_type_out_of_the_sdk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """默认走本地结构化：形状进 instructions，`output_type` **不**交给 SDK。

    交给 SDK 等于请求带 `response_format=json_schema`，而网关会把它**静默忽略**
    （sdk_adapter 模块文档第 7 条）——交了不但没用，还把"模型根本不知道该输出什么形状"
    这件事藏起来。故默认模式下 SDK 只该看到 json_object，形状由提示词传达。
    """
    from docfidelity.agents.outputs import DuplicateJudgement

    captured: dict[str, Any] = {}

    def _runner(agent: Any, prompt: str) -> Any:
        captured["agent"] = agent
        captured["prompt"] = prompt
        return _Result(final='{"is_duplicate": true, "reason": "同功能"}')

    adapter = _adapter(monkeypatch, _fake_sdk(), runner=_runner)
    outcome = adapter.call(_request(), DuplicateJudgement)

    kwargs = captured["agent"].kwargs
    assert "output_type" not in kwargs
    assert kwargs["name"] == "dedup.semantic_duplicate"
    assert "is_duplicate" in kwargs["instructions"]
    assert kwargs["model_settings"].kwargs["extra_args"] == {
        "response_format": {"type": "json_object"}
    }
    assert outcome.ok is True and outcome.schema_ok is True
    assert outcome.payload is not None and outcome.payload["is_duplicate"] is True


def test_adapter_native_mode_hands_output_type_to_the_sdk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """端点确实支持 JSON Schema 时，换回 SDK 原生路径（DD-12.4 的第一手段）。"""
    from docfidelity.agents.outputs import DuplicateJudgement

    captured: dict[str, Any] = {}

    def _runner(agent: Any, prompt: str) -> Any:
        captured["agent"] = agent
        return _Result(final=_Final({"is_duplicate": True, "reason": "同功能"}))

    adapter = _adapter(monkeypatch, _fake_sdk(), runner=_runner, native_structured_output=True)
    outcome = adapter.call(_request(), DuplicateJudgement)

    kwargs = captured["agent"].kwargs
    assert kwargs["output_type"] is DuplicateJudgement
    assert "model_settings" not in kwargs  # json_object 与 output_type 不能并存
    assert "is_duplicate" not in kwargs["instructions"]  # 形状由 schema 传达，不靠提示词
    assert outcome.ok is True and outcome.schema_ok is True
    assert adapter.describe()["structured_output"] == "native"


def test_adapter_local_mode_reports_format_failure_as_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """模型答了但答的不是 JSON ⇒ `ok=True, schema_ok=False`（格式问题，重试有意义）。

    这正是改造前丢掉的那一维：旧代码把它折成 ``ok=False``，于是 `routing._failure_hint`
    走"调用失败"分支，重试时喂给模型的是"模型服务与构建 worker 分离部署"——
    与病因无关，等于把同一个错误问三遍。
    """
    from docfidelity.agents.outputs import DuplicateJudgement

    adapter = _adapter(monkeypatch, _fake_sdk(result=_Result(final="我觉得这一段是重复的")))
    outcome = adapter.call(_request(), DuplicateJudgement)

    assert outcome.ok is True
    assert outcome.schema_ok is False
    assert outcome.payload is None
    assert "找不到 JSON 对象" in outcome.detail


def test_adapter_local_mode_accepts_fenced_and_prose_wrapped_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """模型最常见的两种"礼貌包装"不该被判成格式错（只做结构抽取，不做语义修补）。"""
    from docfidelity.agents.outputs import DuplicateJudgement

    fenced = '```json\n{"is_duplicate": true, "reason": "同功能"}\n```'
    prose = '好的，结论是 {"is_duplicate": true, "reason": "同功能"} 请查收。'
    for text in (fenced, prose):
        adapter = _adapter(monkeypatch, _fake_sdk(result=_Result(final=text)))
        outcome = adapter.call(_request(), DuplicateJudgement)
        assert outcome.ok is True and outcome.schema_ok is True, text
        assert outcome.payload is not None and outcome.payload["is_duplicate"] is True


def test_adapter_local_mode_reason_is_actionable_for_the_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """失败原因要**能照着改**：pydantic 原文（"Field required"）说不出该补哪个字段。"""
    from docfidelity.agents.outputs import DuplicateJudgement

    adapter = _adapter(monkeypatch, _fake_sdk(result=_Result(final='{"reason": "重复"}')))
    outcome = adapter.call(_request(), DuplicateJudgement)

    assert outcome.ok is True and outcome.schema_ok is False
    assert "is_duplicate" in outcome.detail

    extra = _adapter(
        monkeypatch,
        _fake_sdk(result=_Result(final='{"is_duplicate": true, "reason": "x", "bonus": 1}')),
    )
    extra_outcome = extra.call(_request(), DuplicateJudgement)
    assert extra_outcome.schema_ok is False
    assert "bonus" in extra_outcome.detail


def test_adapter_ignores_model_supplied_format_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """格式版本由本地确定，模型无权选——它自编一个版本号会招来与病因无关的失败。"""
    from docfidelity.agents.outputs import DuplicateJudgement

    adapter = _adapter(
        monkeypatch,
        _fake_sdk(
            result=_Result(final='{"format_version": "9.9.9", "is_duplicate": true, "reason": "x"}')
        ),
    )
    outcome = adapter.call(_request(), DuplicateJudgement)

    assert outcome.ok is True and outcome.schema_ok is True
    assert outcome.payload is not None
    assert outcome.payload["format_version"] == "1.0.0"


def test_adapter_registers_no_tools_and_no_handoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """本层只回答一个问题：工具执行由调用方决定，多跳 Agent 是小模型点名的短板。"""
    captured: dict[str, Any] = {}

    def _runner(agent: Any, prompt: str) -> Any:
        captured["agent"] = agent
        return _Result(final=_Final({"choice": "dup"}))

    adapter = _adapter(monkeypatch, _fake_sdk(), runner=_runner)
    adapter.call(_request(), None)
    assert set(captured["agent"].kwargs) == {"name", "instructions", "model"}


def test_adapter_hands_the_endpoint_to_the_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    """A1：``base_url`` / ``api_key`` 必须真的进 SDK。

    漏掉这一步的症状不是报错，而是**请求发到了别人家**（SDK 自己的默认模型与
    ``api.openai.com``），而 ``available()`` / ``describe()`` 还在报"就绪"。
    故断言落在"SDK 拿到的模型对象里有什么"，而不是"适配层有没有 base_url 字段"。
    """
    captured: dict[str, Any] = {}

    def _runner(agent: Any, prompt: str) -> Any:
        captured["model"] = agent.kwargs["model"]
        return _Result(final=_Final({"choice": "dup"}))

    adapter = _adapter(monkeypatch, _fake_sdk(), api_key="sk-local-1", runner=_runner)
    adapter.call(_request(), None)

    model = captured["model"]
    assert isinstance(model, _FakeModel), "必须是 chat-completions 形态（第三方网关普遍只支持它）"
    assert model.kwargs["model"] == "m-1"
    client = model.kwargs["openai_client"]
    assert client.base_url == "http://127.0.0.1:8000/v1"
    assert client.api_key == "sk-local-1"


def test_adapter_builds_the_client_once_across_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    """客户端只建一次：适配层被复用（一个作业一个会话、多个决策点），连接数不该随之增长。"""
    built: list[str] = []

    def _counting_client(base_url: str, api_key: str | None) -> Any:
        built.append(base_url)
        return _Client(base_url, api_key)

    adapter = _adapter(monkeypatch, _fake_sdk(result=_Result(final=_Final({"choice": "dup"}))))
    adapter.client_factory = _counting_client
    adapter.call(_request(), None)
    adapter.call(_request("copy.generate"), None)

    assert built == ["http://127.0.0.1:8000/v1"]


def test_adapter_selects_each_point_model_and_reuses_the_endpoint_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[Any] = []
    clients: list[Any] = []

    def runner(agent: Any, prompt: str) -> Any:
        captured.append(agent.kwargs["model"])
        return _Result(final=_Final({"choice": "dup"}))

    def client_factory(base_url: str, api_key: str | None) -> Any:
        client = _Client(base_url, api_key)
        clients.append(client)
        return client

    adapter = _adapter(monkeypatch, _fake_sdk(), runner=runner, client_factory=client_factory)
    for model in ("specialist", None, "specialist"):
        request = _request().model_copy(update={"model": model})
        outcome = adapter.call(request, None)
        assert outcome.model == (model or "m-1")
    assert [model.kwargs["model"] for model in captured] == ["specialist", "m-1", "specialist"]
    assert captured[0] is captured[2]
    assert len(clients) == 1


def test_adapter_disables_sdk_trace_export_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """SDK 缺省会开 trace 导出——那是**不在数据边界里的外呼**（DD-13.7 / NFR-21）。

    它不报错、不影响产出，所以只能靠断言挡住：部署写着 ``local``、端点指着内网，
    而整轮运行（含提示词与模型输出）被上报到了 SDK 自己的追踪服务。
    """
    sdk = _fake_sdk(result=_Result(final=_Final({"choice": "dup"})))
    adapter = _adapter(monkeypatch, sdk)
    adapter.call(_request(), None)

    run_config = sdk.Runner.run_sync.last_kwargs["run_config"]
    assert run_config.tracing_disabled is True


def test_adapter_can_opt_into_tracing(monkeypatch: pytest.MonkeyPatch) -> None:
    """要开就显式开：那时传 ``None``（= 用 SDK 缺省），而不是传一个"没关"的配置。"""
    sdk = _fake_sdk(result=_Result(final=_Final({"choice": "dup"})))
    adapter = _adapter(monkeypatch, sdk, tracing_enabled=True)
    adapter.call(_request(), None)

    assert sdk.Runner.run_sync.last_kwargs["run_config"] is None


def test_adapter_prompt_lists_candidates(monkeypatch: pytest.MonkeyPatch) -> None:
    """候选注入（需求 5.2.2）：不要求模型想出答案，只要求它在给定集合里选。"""
    captured: dict[str, Any] = {}

    def _runner(agent: Any, prompt: str) -> Any:
        captured["prompt"] = prompt
        return _Result(final=_Final({"choice": "dup"}))

    adapter = _adapter(monkeypatch, _fake_sdk(), runner=_runner)
    adapter.call(_request(), None)
    assert "dup" in captured["prompt"] and "new" in captured["prompt"]
    assert "dedup.semantic_duplicate" in captured["prompt"]


def test_adapter_prompt_includes_retry_feedback(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def _runner(agent: Any, prompt: str) -> Any:
        captured["prompt"] = prompt
        return _Result(final=_Final({"choice": "dup"}))

    adapter = _adapter(monkeypatch, _fake_sdk(), runner=_runner)
    request = _request().model_copy(update={"feedback": ["置信度 0.2 低于阈值"]})
    adapter.call(request, None)
    assert "上次失败原因" in captured["prompt"]
    assert "0.2" in captured["prompt"]


def test_adapter_prompt_preserves_input_values_and_context(monkeypatch: pytest.MonkeyPatch) -> None:
    """真实决策必须收到事实值，嵌套输入与引用上下文都不能只剩字段名。"""
    captured: dict[str, str] = {}

    def runner(agent: Any, prompt: str) -> Any:
        captured["prompt"] = prompt
        return _Result(final=_Final({"choice": "dup"}))

    inputs = {"left": "正文中的术语", "right": "标题中的术语", "items": [{"value": 3}]}
    context = {"source_ref": "synthetic-source", "quote": "line 1\nline 2"}
    request = _request().model_copy(update={"inputs": inputs, "context": context})
    adapter = _adapter(monkeypatch, _fake_sdk(), runner=runner)
    adapter.call(request, None)

    lines = captured["prompt"].splitlines()
    input_json = next(line.removeprefix("输入：") for line in lines if line.startswith("输入："))
    context_json = next(
        line.removeprefix("上下文：") for line in lines if line.startswith("上下文：")
    )
    assert json.loads(input_json) == inputs
    assert json.loads(context_json) == context


def test_adapter_missing_confidence_stays_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """没有置信度钩子时**必须是 None**，绝不能替模型"猜一个"高置信度。"""
    adapter = _adapter(monkeypatch, _fake_sdk(result=_Result(final=_Final({"choice": "dup"}))))
    assert adapter.call(_request(), None).confidence is None


def test_adapter_confidence_hook_is_applied(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = _adapter(
        monkeypatch,
        _fake_sdk(result=_Result(final=_Final({"choice": "dup"}))),
        confidence_of=lambda result: 0.83,
    )
    assert adapter.call(_request(), None).confidence == 0.83


def test_adapter_non_structured_output_is_a_schema_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """SDK 没返回结构化输出时如实报 schema 失败，不把字符串硬当结论。"""
    adapter = _adapter(monkeypatch, _fake_sdk(result=_Result(final="dup")))
    outcome = adapter.call(_request(), None)
    assert outcome.ok is False
    assert outcome.schema_ok is False
    assert outcome.payload is None


def test_adapter_missing_final_output_is_a_schema_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = _adapter(monkeypatch, _fake_sdk(result=_Result(final=None)))
    outcome = adapter.call(_request(), None)
    assert outcome.ok is False
    assert outcome.schema_ok is False


def test_adapter_swallows_sdk_exception_and_does_not_leak_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SDK 抛什么都不能让构建失败；同时**不得**把原始异常文本放进 detail（可能含密钥/端点）。"""
    boom = RuntimeError("connection refused; api_key=sk-leak-me")
    adapter = _adapter(monkeypatch, _fake_sdk(boom=boom))
    outcome = adapter.call(_request(), None)
    assert outcome.ok is False
    assert outcome.error == ErrorCode.MODEL_UNAVAILABLE
    assert "RuntimeError" in outcome.detail
    assert "sk-leak-me" not in outcome.detail


def test_adapter_maps_mapping_final_output(monkeypatch: pytest.MonkeyPatch) -> None:
    """非 pydantic 的映射型输出也接受（不同 SDK 版本返回形状不同）。"""
    adapter = _adapter(monkeypatch, _fake_sdk(result=_Result(final={"choice": "new"})))
    outcome = adapter.call(_request(), None)
    assert outcome.ok is True
    assert outcome.payload == {"choice": "new"}
    assert outcome.chosen == "new"


def test_adapter_usage_missing_is_none_not_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    """取不到 token 数就留空，不编 0——NFR-18 的成本归集不能有假的零。"""
    adapter = _adapter(monkeypatch, _fake_sdk(result=_Result(final=_Final({"choice": "dup"}))))
    outcome = adapter.call(_request(), None)
    assert outcome.tokens_in is None and outcome.tokens_out is None


def test_adapter_chosen_prefers_the_conventional_field(monkeypatch: pytest.MonkeyPatch) -> None:
    """泛化地"取第一个字符串字段"会在多字段输出上取错，而错的取值会一路写进追溯链。"""
    payload = {"value": "wrong", "choice": "right"}
    adapter = _adapter(monkeypatch, _fake_sdk(result=_Result(final=_Final(payload))))
    assert adapter.call(_request(), None).chosen == "right"


def test_adapter_describe_reports_grammar_state(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = _adapter(
        monkeypatch,
        _fake_sdk(),
        grammar=GrammarCapability(supported=False, detail="未检出受限解码后端"),
    )
    described = adapter.describe()
    assert described["grammar_supported"] is False
    assert described["grammar_detail"] == "未检出受限解码后端"


def test_adapter_is_available_with_sdk_and_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = _adapter(monkeypatch, _fake_sdk())
    assert adapter.available() is True
    assert adapter.describe()["available"] is True
