"""工具面：白名单不变量 + 调用闸门（DD-12.2 / 需求 5.5）。

本模块盯的是"模型的动作空间"这件事的三条不变量：**不碰 OOXML**（决策三）、
**结构化失败**（不向模型抛裸异常）、**高危必须审批**。三条都属于"不守也没有任何
现象"的类型——工具照常返回结果，只是那个结果可能是在直写 XML，或者异常裸穿到
调用栈上。故这里既测判定，也测"判定接在哪一步"。
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from pydantic import ValidationError

from docfidelity.agents import TOOLS, ToolApprovalPolicy
from docfidelity.agents.tools import (
    TOOL_SPECS,
    assert_whitelist_sane,
    descriptions,
    exposed_names,
    get,
    invoke,
    names,
)
from docfidelity.agents.tools.library import TOOLS_BY_NAME
from docfidelity.agents.tools.spec import (
    FORBIDDEN_HANDLER_PREFIXES,
    MAX_PARAMS_PER_TOOL,
    ToolFailure,
    ToolOutput,
    ToolParam,
    ToolSpec,
    call_kwargs,
    validate_spec,
)
from docfidelity.contracts.base import DEFAULT_FORMAT_VERSION
from docfidelity.contracts.errors import EngineError, ErrorCode
from docfidelity.qa import default_catalog
from tests.fixtures.compose_fixture import api_models, api_profile, api_snapshot

pytestmark = pytest.mark.unit

#: 当前未进入可见面的工具。**只允许缩小**：这份名单的每一次缩短都意味着某个能力
#: 补上了 JSON 化门面，而它的变长意味着有人把工具悄悄撤出了模型的动作空间。
#:
#: 本轮（P0-16 分层补齐）缩短了五项：``template_parse`` / ``content_ir_build`` /
#: ``qa_run`` / ``style_audit`` / ``drift_check`` 的 JSON 门面已落到各自归属层。剩下的
#: 七项里，``unit_section`` 是 ``unbound``（架构上无独立入口），其余六项是 render 的
#: 后半段步骤——入参是**流水线中途**的 ``RenderContext``，门面即 ``render.render()``
#: 阶段本身，见 ``library.py`` 的"门面下沉"。
EXPECTED_UNEXPOSED = frozenset(
    {
        "unit_section",
        "table_section",
        "slot_plan",
        "render_inject",
        "pagination_apply",
        "figure_place",
        "toc_refresh",
    }
)


@pytest.fixture
def scratch_tool() -> Iterator[Callable[[ToolSpec], ToolSpec]]:
    """临时注册一个测试用工具（测异常映射与结构化失败用），用例结束时自动摘除。"""

    created: list[str] = []

    def register(spec: ToolSpec) -> ToolSpec:
        TOOLS_BY_NAME[spec.name] = spec
        created.append(spec.name)
        return spec

    try:
        yield register
    finally:
        for name in created:
            TOOLS_BY_NAME.pop(name, None)


# ------------------------------------------------------------------ 白名单
def test_whitelist_sane_and_names_unique() -> None:
    assert_whitelist_sane()
    assert len(names()) == len(set(names())) == 20
    assert set(names()) == set(TOOLS)


def test_no_ooxml_direct_write_in_whitelist() -> None:
    """决策三的可执行形式：白名单里不存在任何 OOXML 直写。"""
    offenders = [
        spec.handler_ref
        for spec in TOOL_SPECS
        if spec.handler_ref
        and any(
            spec.handler_ref == prefix or spec.handler_ref.startswith(prefix + ".")
            for prefix in FORBIDDEN_HANDLER_PREFIXES
        )
    ]
    assert not offenders, offenders


def test_validate_rejects_ooxml_handler() -> None:
    """对立断言：把实现位置指向 ooxml 必须**当场被拒**，而不是靠人肉检查。"""
    spec = ToolSpec(
        name="rogue",
        group="render",
        summary="直写 XML 的工具。",
        example="rogue()",
        handler_ref="docfidelity.ooxml.doc.save",
    )
    with pytest.raises(EngineError) as caught:
        validate_spec(spec)
    assert caught.value.code == ErrorCode.CONFIG_INVALID
    assert "决策三" in caught.value.message


def test_write_tools_must_require_approval() -> None:
    """写盘 / 覆盖 / 刷新域必须过 guardrail（DD-12.2 第 4 条）。"""
    for spec in TOOL_SPECS:
        if spec.writes:
            assert spec.requires_approval, spec.name
    assert [spec.name for spec in TOOL_SPECS if spec.writes] == [
        "registry_put",
        "toc_refresh",
    ]


def test_validate_rejects_write_without_approval() -> None:
    spec = ToolSpec(
        name="sneaky_write",
        group="template",
        summary="偷偷写盘。",
        example="sneaky_write()",
        handler_ref="docfidelity.registry.write_package",
        writes=True,
    )
    with pytest.raises(EngineError) as caught:
        validate_spec(spec)
    assert "requires_approval" in caught.value.message


def test_all_bound_handlers_resolve() -> None:
    """每个已接线工具都必须解析到可调用对象。

    点路径写错的后果是"模型调一个永远失败的工具"，而它看起来和"这次刚好失败"
    一模一样——所以只在测试里发现是不够的，这里就是发现它的地方。
    """
    bound = [spec for spec in TOOL_SPECS if spec.bound]
    assert len(bound) >= 8
    for spec in bound:
        assert callable(spec.handler()), spec.name


def test_unexposed_set_is_locked() -> None:
    assert set(names()) - set(exposed_names()) == EXPECTED_UNEXPOSED


def test_exposed_tools_have_flat_bounded_params() -> None:
    """暴露面必须扁平：参数个数、非 callable 不得声明参数、描述齐备（需求 5.2.5）。"""
    for spec in TOOL_SPECS:
        if len(spec.params) > MAX_PARAMS_PER_TOOL:
            pytest.fail(f"{spec.name} 参数过多")
        if not spec.exposed:
            assert spec.params == (), spec.name
            assert spec.context_note, spec.name
        assert spec.summary.endswith("。"), spec.name
        assert spec.example, spec.name
        kinds = {param.kind for param in spec.params}
        assert kinds <= {
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
        }, spec.name
    assert set(descriptions()) == set(exposed_names())


def test_get_rejects_unknown_name() -> None:
    assert get("fingerprint_diff").group == "template"
    with pytest.raises(EngineError) as caught:
        get("no_such_tool")
    assert caught.value.code == ErrorCode.INTERNAL


def test_args_model_is_cached_and_forbids_unknown_fields() -> None:
    """参数形状来自同一份声明，且**模型编造的参数名会被直接拒**（需求 5.2.1）。"""
    spec = get("fingerprint_diff")
    model = spec.args_model()
    assert model is spec.args_model()
    # 工具入参模型与其余契约一样继承 FormatModel（多一个 format_version 带默认值）：
    # 这样"被记录进决策轨迹的入参"依然是可序列化且带版本号的形态。
    assert {p.name for p in spec.params} <= set(model.model_fields)
    assert model.model_fields["format_version"].default == DEFAULT_FORMAT_VERSION
    with pytest.raises(ValidationError):
        model.model_validate({"before": {}, "after": {}, "invented": 1})
    # 声明里没有的参数要么被拒（上面），要么根本不会流进实现——`call_kwargs` 只取声明项。
    valid = model.model_validate(
        {"before": {"digest": "a"}, "after": {"digest": "b"}},
    )
    assert set(call_kwargs(spec, valid)) == {"before", "after"}


def test_unexposed_tool_never_declares_params() -> None:
    """非 callable 工具不得声明参数——硬凑一份参数表只会产出"看起来精确的猜测"。"""
    spec = ToolSpec(
        name="guessing",
        group="gate",
        summary="硬凑参数。",
        example="guessing()",
        availability="context_bound",
        handler_ref="docfidelity.qa.run",
        context_note="缺上下文门面",
        params=(ToolParam(name="x", description="猜的"),),
    )
    with pytest.raises(EngineError) as caught:
        validate_spec(spec)
    assert "不是 callable 却声明了参数" in caught.value.message


# -------------------------------------------------------------- 调用闸门
def test_invoke_real_handler_returns_structured_output() -> None:
    """真跑一次确定性实现：契约 → 校验 → 调用 → 结果折成可序列化字典。"""
    result = invoke(
        "fingerprint_diff",
        {
            "before": {"digest": "a", "sections": {"styles": "1", "blocks": "1"}},
            "after": {"digest": "b", "sections": {"styles": "2", "blocks": "1"}},
        },
    )
    assert result.ok is True
    assert result.failure is None
    assert result.output is not None
    assert result.output["data"]["items"] == ["styles"]


def test_invoke_reports_in_place_handler_honestly() -> None:
    """就地生效型实现返回 None，如实记成空 data 加一句 note，不伪造 ok 载荷。"""
    result = invoke(
        "glossary_apply",
        {
            "ir": {
                "project_id": "p",
                "doc_key": "d",
                "profile_id": "pf",
                "template_ref": "t@1.0.0",
            },
            "terms": {"entries": []},
        },
    )
    assert result.ok is True
    assert result.output is not None
    assert result.output["data"] == {}
    assert "无返回值" in (result.output["note"] or "")


def test_invoke_unexposed_tool_is_internal_defect() -> None:
    """调用不可调用的工具 = 实现缺陷（它本不该出现在可见面），不是模型的问题。"""
    result = invoke("render_inject", {})
    assert result.ok is False
    assert result.failure is not None
    assert result.failure.code == ErrorCode.INTERNAL
    assert result.failure.retryable is False


def test_invoke_rejects_unknown_and_missing_params() -> None:
    unknown = invoke(
        "fingerprint_diff",
        {"before": {"digest": "a"}, "after": {"digest": "b"}, "extra": 1},
    )
    assert unknown.failure is not None
    assert unknown.failure.code == ErrorCode.SCHEMA_INVALID
    assert unknown.failure.retryable is True  # 换个参数重试有意义

    missing = invoke("fingerprint_diff", {"before": {"digest": "a"}})
    assert missing.failure is not None
    assert missing.failure.code == ErrorCode.SCHEMA_INVALID


def test_invoke_unknown_tool_name() -> None:
    result = invoke("no_such_tool", {})
    assert result.failure is not None
    assert result.failure.code == ErrorCode.INTERNAL


def test_invoke_requires_approval_before_validation() -> None:
    """审批先于参数校验：未获批的高危动作不该先被"校验一遍参数"。"""
    result = invoke("registry_put", {"package": {"manifest": {}}, "target_dir": "var/x"})
    assert result.failure is not None
    assert result.failure.code == ErrorCode.CONFIRM_REQUIRED
    assert result.requires_approval is True
    assert result.approved is False


def test_invoke_path_tools_fail_closed_without_roots() -> None:
    """没配作用域 = 一律拒绝。默认放行会把"作用域没配"变成"作用域是整个磁盘"。"""
    denied = invoke("block_index", {"root": "var/packages"})
    assert denied.failure is not None
    assert denied.failure.code == ErrorCode.PATH_ESCAPE

    allowed = invoke(
        "block_index",
        {"root": "var/packages"},
        policy=ToolApprovalPolicy.of(allowed_roots=["."]),
    )
    assert allowed.ok is True


def test_invoke_escapes_roots_when_configured() -> None:
    result = invoke(
        "block_index",
        {"root": "../../etc"},
        policy=ToolApprovalPolicy.of(allowed_roots=["var/packages"]),
    )
    assert result.failure is not None
    assert result.failure.code == ErrorCode.PATH_ESCAPE
    assert result.failure.where["param"] == "root"


def test_invoke_maps_engine_error_to_structured_failure(
    scratch_tool: Callable[[ToolSpec], ToolSpec],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """内核异常 → ``ToolFailure``，保留原错误码与可重试性（``fatal=False`` ⇒ 可重试）。"""
    import docfidelity.agents.tools as tools_pkg

    def _boom(**_: object) -> None:
        raise EngineError(
            ErrorCode.SOURCE_LOAD_FAILED,
            "来源暂时读不到",
            fatal=False,
            remediation=["稍后重试"],
        )

    # handler_ref 是惰性点路径，故把实现临时挂到一个可解析的模块路径上
    monkeypatch.setattr(tools_pkg, "_test_explode_engine", _boom, raising=False)
    scratch_tool(
        ToolSpec(
            name="explode_engine",
            group="facts",
            summary="抛内核异常。",
            example="explode_engine()",
            params=(ToolParam(name="x", kind="str", description="占位", required=False),),
            handler_ref="docfidelity.agents.tools._test_explode_engine",
        )
    )
    result = invoke("explode_engine", {})
    assert result.ok is False
    assert result.failure is not None
    assert result.failure.code == ErrorCode.SOURCE_LOAD_FAILED
    assert result.failure.retryable is True
    assert result.failure.remediation == ["稍后重试"]


def test_invoke_never_leaks_bare_exception_text(
    scratch_tool: Callable[[ToolSpec], ToolSpec],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """实现函数抛非内核异常时，只回类型名——原始文案可能带路径与内部细节。"""
    import docfidelity.agents.tools as tools_pkg

    def _boom(**_: object) -> None:
        raise RuntimeError("C:/secret/path 内部细节")

    monkeypatch.setattr(tools_pkg, "_test_bare_boom", _boom, raising=False)
    scratch_tool(
        ToolSpec(
            name="bare_boom",
            group="facts",
            summary="抛裸异常。",
            example="bare_boom()",
            handler_ref="docfidelity.agents.tools._test_bare_boom",
        )
    )
    result = invoke("bare_boom", {})
    assert result.failure is not None
    assert result.failure.code == ErrorCode.INTERNAL
    assert "RuntimeError" in result.failure.message
    assert "secret" not in result.failure.message


def test_invoke_does_not_swallow_base_exception(
    scratch_tool: Callable[[ToolSpec], ToolSpec],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``BaseException`` 不吞：键盘中断必须照常冒出去，否则进程无法正常停止。"""
    import docfidelity.agents.tools as tools_pkg

    def _interrupt(**_: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(tools_pkg, "_test_interrupt", _interrupt, raising=False)
    scratch_tool(
        ToolSpec(
            name="interrupt_tool",
            group="facts",
            summary="抛中断。",
            example="interrupt_tool()",
            handler_ref="docfidelity.agents.tools._test_interrupt",
        )
    )
    with pytest.raises(KeyboardInterrupt):
        invoke("interrupt_tool", {})


def test_tool_output_and_failure_models_are_serializable() -> None:
    assert ToolOutput(data={"a": 1}).model_dump()["data"] == {"a": 1}
    assert ToolFailure(code="X", message="m").model_dump()["retryable"] is False


# ------------------------------------------------- 门面（P0-16 分层补齐）
#: 本轮从 ``context_bound`` 升为 ``callable`` 的五个工具。它们的 JSON 门面落在
#: 各自归属层（registry / compose / qa），agents 只留惰性 ``handler_ref``——
#: 于是"补门面"不必把 OOXML 相关代码搬进 agents（决策三的契约不破）。
_NEW_FACADES = ("template_parse", "content_ir_build", "qa_run", "style_audit", "drift_check")


def test_new_facades_are_exposed_with_flat_params() -> None:
    for name in _NEW_FACADES:
        spec = get(name)
        assert spec.exposed, name
        assert spec.params, name
        assert len(spec.params) <= MAX_PARAMS_PER_TOOL, name


def test_template_parse_facade_returns_a_package_without_writing(template_docx: Path) -> None:
    """template_parse 的 registry 门面：路径进、契约出，且**不落库**。

    "不落库"是它与 ``ingest_template`` 的刻意分工：写盘留在 ``registry_put``（需审批）。
    这里以"目录没有多出任何文件"作为可观察判据，而不是靠"函数没写 target_dir"自证。
    """
    before = set(template_docx.parent.iterdir())
    result = invoke(
        "template_parse",
        {"source": str(template_docx), "template_id": "demo", "version": "1.0.0"},
        policy=ToolApprovalPolicy.of(allowed_roots=[str(template_docx.parent)]),
    )
    assert result.ok is True, result.failure
    assert result.output is not None
    package = result.output["data"]["value"]
    assert package["manifest"]["template_id"] == "demo"
    assert package["manifest"]["version"] == "1.0.0"
    assert set(template_docx.parent.iterdir()) == before


def test_template_parse_facade_fails_closed_on_a_broken_source(tmp_path: Path) -> None:
    """原件打不开 → 结构化失败（不抛裸异常），且**不返回半成品**。"""
    result = invoke(
        "template_parse",
        {"source": str(tmp_path / "absent.docx")},
        policy=ToolApprovalPolicy.of(allowed_roots=[str(tmp_path)]),
    )
    assert result.ok is False
    assert result.failure is not None
    assert result.failure.code == ErrorCode.TEMPLATE_UNREADABLE


def test_content_ir_build_facade_returns_an_ir() -> None:
    """content_ir_build 的 compose 门面：契约对象进、内容 IR 出。"""
    result = invoke(
        "content_ir_build",
        {
            "snapshot": api_snapshot().model_dump(mode="json"),
            "profile": api_profile().model_dump(mode="json"),
            "domain": [model.model_dump(mode="json") for model in api_models()],
            "doc_key": "detail",
            "template_ref": "TPL-API@1.0",
        },
    )
    assert result.ok is True, result.failure
    assert result.output is not None
    ir = result.output["data"]["value"]
    assert ir["doc_key"] == "detail"
    assert ir["profile_id"] == "api-compact"


def test_drift_check_facade_compares_two_paths(template_docx: Path) -> None:
    """drift_check 的 qa 门面：两份路径进、结构快照出；同一份必然一致。"""
    result = invoke(
        "drift_check",
        {"baseline": str(template_docx), "candidate": str(template_docx), "mode": "structure"},
        policy=ToolApprovalPolicy.of(allowed_roots=[str(template_docx.parent)]),
    )
    assert result.ok is True, result.failure
    assert result.output is not None
    snapshot = result.output["data"]["value"]
    assert snapshot["added"] == []
    assert snapshot["removed"] == []
    assert snapshot["changed"] == []


def test_drift_check_facade_rejects_an_unknown_mode(template_docx: Path) -> None:
    """模式不认识 → ``E_SCHEMA_INVALID``：照抄内核的校验，不静默取一个默认模式。"""
    result = invoke(
        "drift_check",
        {"baseline": str(template_docx), "candidate": str(template_docx), "mode": "bytes"},
        policy=ToolApprovalPolicy.of(allowed_roots=[str(template_docx.parent)]),
    )
    assert result.ok is False
    assert result.failure is not None
    assert result.failure.code == ErrorCode.SCHEMA_INVALID


def test_qa_facades_return_reports(template_docx: Path) -> None:
    """qa_run / style_audit 的 qa 门面：产物 + 契约进、门禁报告出（**只读**）。"""
    policy = ToolApprovalPolicy.of(allowed_roots=[str(template_docx.parent)])
    parsed = invoke(
        "template_parse",
        {"source": str(template_docx), "template_id": "demo", "version": "1.0.0"},
        policy=policy,
    )
    assert parsed.output is not None
    package = parsed.output["data"]["value"]
    ir = {
        "project_id": "demo",
        "doc_key": "detail",
        "profile_id": "api-compact",
        "template_ref": "TPL-API@1.0",
    }

    report = invoke(
        "qa_run",
        {"artifact": str(template_docx), "template": package, "ir": ir, "job_id": "j1"},
        policy=policy,
    )
    assert report.ok is True, report.failure
    assert report.output is not None
    assert report.output["data"]["value"]["job_id"] == "j1"

    rule_set_id = sorted(default_catalog().files)[0]
    audited = invoke(
        "style_audit",
        {
            "artifact": str(template_docx),
            "template": package,
            "ir": ir,
            "rule_set_id": rule_set_id,
        },
        policy=policy,
    )
    assert audited.ok is True, audited.failure
    assert audited.output is not None
