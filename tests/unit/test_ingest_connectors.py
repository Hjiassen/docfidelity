"""五个内建接入器的单测（DD-5.2）。

每个接入器都有两类断言：**产出了什么**（happy path）与**遇到不该收的东西怎么办**
（自引用 ``$ref``、不在白名单的 router、不合规的图片命名、语法错误的源码）。
后者才是接入层的成本所在——接入器最常见的失败不是"读不出来"，而是**安静地少读了一部分**。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from docfidelity.contracts.errors import ConfigurationError, EngineError
from docfidelity.contracts.fact import Fact, FactBundle, KindBinding, SourceConfig
from docfidelity.ingest import FactEmitter, default_registry, run_connector
from docfidelity.ingest.connectors.xlsx import detect_header_row, shape_for
from tests.fixtures.ingest_fixture import (
    assets_config,
    ast_config,
    build_workspace,
    openapi_config,
)

pytestmark = pytest.mark.unit

ENDPOINT_KIND = "endpoint"
PARAMETER_KIND = "parameter"
FIGURE_KIND = "figure"


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    return build_workspace(tmp_path)


def _rooted(cfg: SourceConfig, root: Path) -> SourceConfig:
    """把来源根锚到夹具工作区。

    ``load`` 会把 ``root`` 按工作区解析，``probe`` 不会——它拿到的必须是可直接
    访问的路径，否则测的是"根目录不存在"而不是"试接入算出来的条数"。
    """
    return cfg.model_copy(update={"root": str(root / str(cfg.root))})


def _load(name: str, cfg: SourceConfig, root: Path) -> FactBundle:
    return run_connector(default_registry().get(name), _rooted(cfg, root))


def _of_kind(bundle: FactBundle, kind: str) -> list[Fact]:
    return [fact for fact in bundle.facts if fact.kind == kind]


def _by_locator(bundle: FactBundle) -> dict[str, Fact]:
    return {fact.locator: fact for fact in bundle.facts}


# -- openapi ---------------------------------------------------------------


def test_openapi_emits_one_fact_per_operation(workspace: Path) -> None:
    bundle = _load("openapi", openapi_config(), workspace)
    operations = _of_kind(bundle, ENDPOINT_KIND)
    assert len(operations) == 3
    assert sorted(fact.payload["path"] for fact in operations) == [
        "/health",
        "/orders",
        "/orders/{id}",
    ]
    assert sorted(fact.payload["method"] for fact in operations) == ["GET", "GET", "POST"]


def test_openapi_maps_scope_through_the_binding(workspace: Path) -> None:
    bundle = _load("openapi", openapi_config(), workspace)
    modules = {fact.payload["module"] for fact in _of_kind(bundle, ENDPOINT_KIND)}
    assert modules == {"order", "default"}


def test_openapi_scope_default_is_configurable(workspace: Path) -> None:
    cfg = openapi_config(options={"scope_default": "misc"})
    bundle = _load("openapi", cfg, workspace)
    modules = {fact.payload["module"] for fact in _of_kind(bundle, ENDPOINT_KIND)}
    assert modules == {"order", "misc"}


def test_openapi_extracts_path_parameters_and_body(workspace: Path) -> None:
    bundle = _load("openapi", openapi_config(), workspace)
    facts = _by_locator(bundle)
    body = facts["openapi.json#/paths//orders/post"].payload["parameters"]
    assert [item["name"] for item in body] == ["body"]
    assert body[0]["source"] == "body"
    assert body[0]["required"] is True

    path_level = facts["openapi.json#/paths//orders/{id}/get"].payload["parameters"]
    assert [(item["name"], item["source"], item["type"]) for item in path_level] == [
        ("id", "path", "string")
    ]


def test_openapi_sorts_response_codes_as_text(workspace: Path) -> None:
    bundle = _load("openapi", openapi_config(), workspace)
    fact = _by_locator(bundle)["openapi.json#/paths//orders/post"]
    assert [item["code"] for item in fact.payload["responses"]] == ["200", "400"]


def test_openapi_reads_security_schemes(workspace: Path) -> None:
    bundle = _load("openapi", openapi_config(), workspace)
    fact = _by_locator(bundle)["openapi.json#/paths//orders/post"]
    assert fact.payload["security"] == ["bearerAuth"]
    assert fact.payload["auth_required"] is True


def test_openapi_cyclic_ref_does_not_hang(workspace: Path) -> None:
    """自引用 schema（``Order.parent -> Order``）必须展开到有限深度，而不是把进程打死。

    ``schema`` 形态默认不绑（见 ``openapi_config``），这里显式补一条绑定才能看到它。
    """
    cfg = openapi_config()
    cfg = cfg.model_copy(
        update={
            "bindings": [
                *cfg.bindings,
                KindBinding(shape="schema", kind="schema_def", key=["name"]),
            ]
        }
    )
    bundle = _load("openapi", cfg, workspace)
    schemas = _of_kind(bundle, "schema_def")
    assert len(schemas) == 1
    assert schemas[0].payload["property_count"] == 2


def test_openapi_reports_unbound_shapes(workspace: Path) -> None:
    cfg = openapi_config()  # schema 形态刻意不绑
    bundle = _load("openapi", cfg, workspace)
    messages = [warning.message for warning in bundle.warnings]
    assert any("schema" in message for message in messages)


def test_openapi_derives_a_slash_free_operation_ref(workspace: Path) -> None:
    """接入器**自己派生**的标识不得含分隔符——它随时可能被配置成标识键。

    至于来源里本来就带 ``/`` 的值（如路径），接入器只做转发：那是档案选键的问题，
    接入层替它改值反而会制造"事实与来源不一致"。
    """
    bundle = _load("openapi", openapi_config(), workspace)
    refs = [fact.payload.get("operation_ref") for fact in bundle.facts]
    assert refs and all(ref is not None for ref in refs)
    assert all("/" not in str(ref) and "{" not in str(ref) for ref in refs)


def test_openapi_is_deterministic(workspace: Path) -> None:
    first = _load("openapi", openapi_config(), workspace)
    second = _load("openapi", openapi_config(), workspace)
    assert first.digest == second.digest
    assert [fact.key for fact in first.facts] == [fact.key for fact in second.facts]


def test_openapi_probe_counts_operations(workspace: Path) -> None:
    cfg = openapi_config().model_copy(update={"root": str(workspace / "specs")})
    result = default_registry().get("openapi").probe(cfg)
    assert result.ok is True
    assert result.item_count == 3


def test_openapi_without_a_document_fails_loudly(tmp_path: Path) -> None:
    (tmp_path / "specs").mkdir()
    cfg = openapi_config().model_copy(update={"root": str(tmp_path / "specs")})
    with pytest.raises(ConfigurationError):
        default_registry().get("openapi").load(cfg)


# -- ast -------------------------------------------------------------------


def test_ast_emits_routes_and_models(workspace: Path) -> None:
    bundle = _load("ast", ast_config(), workspace)
    routes = _of_kind(bundle, ENDPOINT_KIND)
    models = _of_kind(bundle, PARAMETER_KIND)
    assert sorted(fact.payload["path"] for fact in routes) == ["/orders", "/orders/{order_id}"]
    assert sorted(fact.payload["name"] for fact in models) == ["OrderCreate", "OrderView"]


def test_ast_ignores_routers_outside_the_whitelist(workspace: Path) -> None:
    bundle = _load("ast", ast_config(), workspace)
    routes = _of_kind(bundle, ENDPOINT_KIND)
    assert all(fact.payload["handler"] != "ignored" for fact in routes)


def test_ast_ignores_undecorated_functions(workspace: Path) -> None:
    bundle = _load("ast", ast_config(), workspace)
    assert all(fact.payload.get("handler") != "helper" for fact in bundle.facts)


def test_ast_reads_model_fields(workspace: Path) -> None:
    bundle = _load("ast", ast_config(), workspace)
    model = next(
        fact for fact in _of_kind(bundle, PARAMETER_KIND) if fact.payload["name"] == "OrderCreate"
    )
    fields = {item["name"]: item for item in model.payload["fields"]}
    assert fields["order_id"]["required"] is True
    assert fields["order_id"]["desc"] == "订单号"
    assert fields["reason"]["required"] is False
    assert fields["urgent"]["required"] is False
    assert fields["urgent"]["desc"] == "加急开关"


def test_ast_never_imports_the_target_module(tmp_path: Path) -> None:
    """被接入的源码里带模块级副作用——如果被 import 了，这个测试会直接炸。"""
    danger = tmp_path / "dangerous_module.py"
    danger.write_text(
        'raise RuntimeError("这个模块绝不该被导入")\n\n\n@router.get("/x")\ndef handler() -> None:\n    """x"""\n',
        encoding="utf-8",
    )
    cfg = ast_config(root=".").model_copy(update={"root": str(tmp_path)})
    bundle = default_registry().get("ast").load(cfg)
    assert len(_of_kind(bundle, ENDPOINT_KIND)) == 1
    assert "dangerous_module" not in sys.modules


def test_ast_warns_about_syntax_errors_and_keeps_going(workspace: Path) -> None:
    broken = workspace / "src" / "broken.py"
    broken.write_text("def oops(:\n", encoding="utf-8")
    bundle = _load("ast", ast_config(), workspace)
    assert any("broken.py" in warning.message for warning in bundle.warnings)
    assert len(_of_kind(bundle, ENDPOINT_KIND)) == 2


def test_ast_warns_when_nothing_is_recognised(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "plain.py").write_text("value = 1\n", encoding="utf-8")
    cfg = ast_config(root="src").model_copy(update={"root": str(tmp_path / "src")})
    bundle = default_registry().get("ast").load(cfg)
    assert bundle.facts == []
    assert any("未识别到任何声明" in warning.message for warning in bundle.warnings)


def test_ast_probe_counts_declarations(workspace: Path) -> None:
    cfg = ast_config().model_copy(update={"root": str(workspace / "src")})
    result = default_registry().get("ast").probe(cfg)
    assert result.item_count == 4  # 2 路由 + 2 模型


# -- orm -------------------------------------------------------------------


def test_orm_emits_model_and_migration_tables(workspace: Path) -> None:
    cfg = SourceConfig(
        source_id="db",
        source_type="orm",
        root=str(workspace),
        bindings=[KindBinding(shape="table", kind="db_table", key=["table"])],
    )
    bundle = run_connector(default_registry().get("orm"), cfg)
    origins = {fact.payload["table"]: fact.payload["origin"] for fact in bundle.facts}
    assert origins == {"order_record": "model", "audit_log": "migration"}


def test_orm_label_prefers_table_comment(workspace: Path) -> None:
    cfg = SourceConfig(
        source_id="db",
        source_type="orm",
        root=str(workspace),
        bindings=[KindBinding(shape="table", kind="db_table", key=["table"])],
    )
    bundle = run_connector(default_registry().get("orm"), cfg)
    model_fact = next(fact for fact in bundle.facts if fact.payload["origin"] == "model")
    assert model_fact.payload["label"] == "订单记录表"


def test_orm_reads_column_details(workspace: Path) -> None:
    cfg = SourceConfig(
        source_id="db",
        source_type="orm",
        root=str(workspace),
        bindings=[KindBinding(shape="table", kind="db_table", key=["table"])],
    )
    bundle = run_connector(default_registry().get("orm"), cfg)
    model_fact = next(fact for fact in bundle.facts if fact.payload["origin"] == "model")
    columns = {item["name"]: item for item in model_fact.payload["columns"]}
    assert columns["id"]["primary_key"] is True
    assert columns["order_no"]["nullable"] is False
    assert columns["order_no"]["type"] == "String(32)"
    assert columns["order_no"]["comment"] == "订单号"
    assert columns["note"]["nullable"] is True


# -- xlsx ------------------------------------------------------------------


def test_xlsx_emits_one_fact_per_sheet(workspace: Path) -> None:
    cfg = SourceConfig(
        source_id="tables",
        source_type="xlsx",
        root=str(workspace / "tables"),
        bindings=[KindBinding(shape="sheet", kind="sheet_ref", key=["sheet"])],
    )
    bundle = run_connector(default_registry().get("xlsx"), cfg)
    assert [fact.payload["sheet"] for fact in bundle.facts] == ["变更点", "键值"]


def test_xlsx_resolves_shared_strings(workspace: Path) -> None:
    cfg = SourceConfig(
        source_id="tables",
        source_type="xlsx",
        root=str(workspace / "tables"),
        bindings=[KindBinding(shape="sheet", kind="sheet_ref", key=["sheet"])],
    )
    bundle = run_connector(default_registry().get("xlsx"), cfg)
    sheet = next(fact for fact in bundle.facts if fact.payload["sheet"] == "变更点")
    cells = sheet.payload["cells"]
    assert cells["A3"] == "编号"
    assert cells["D4"] == "张三"
    assert cells["A5"] == "2"  # 数值单元格保留原样文本


def test_xlsx_resolves_inline_strings(workspace: Path) -> None:
    cfg = SourceConfig(
        source_id="tables",
        source_type="xlsx",
        root=str(workspace / "tables"),
        bindings=[KindBinding(shape="sheet", kind="sheet_ref", key=["sheet"])],
    )
    bundle = run_connector(default_registry().get("xlsx"), cfg)
    sheet = next(fact for fact in bundle.facts if fact.payload["sheet"] == "键值")
    assert sheet.payload["cells"] == {"A1": "键", "B1": "值", "A2": "版本", "B2": "1.0.0"}


def test_xlsx_records_merged_ranges(workspace: Path) -> None:
    cfg = SourceConfig(
        source_id="tables",
        source_type="xlsx",
        root=str(workspace / "tables"),
        bindings=[KindBinding(shape="sheet", kind="sheet_ref", key=["sheet"])],
    )
    bundle = run_connector(default_registry().get("xlsx"), cfg)
    sheet = next(fact for fact in bundle.facts if fact.payload["sheet"] == "变更点")
    assert sheet.payload["merged"] == ["A2:D2"]
    assert (sheet.payload["row_count"], sheet.payload["col_count"]) == (5, 4)


def test_xlsx_detects_the_offset_header_row(workspace: Path) -> None:
    cfg = SourceConfig(
        source_id="tables",
        source_type="xlsx",
        root=str(workspace / "tables"),
        bindings=[KindBinding(shape="sheet", kind="sheet_ref", key=["sheet"])],
    )
    bundle = run_connector(default_registry().get("xlsx"), cfg)
    rows = {fact.payload["sheet"]: fact.payload["header_row"] for fact in bundle.facts}
    assert rows == {"变更点": 2, "键值": 0}


def test_detect_header_row_gives_up_instead_of_guessing() -> None:
    assert detect_header_row([["", ""], ["", "a"]]) is None
    assert detect_header_row([["", ""], ["", ""]]) is None
    assert detect_header_row([["a", "b"], ["", ""]]) == 0


# -- xlsx：逐行形态（``options.row_shape``） --------------------------------
# 夹具工作簿有两张表：``变更点``（表头在第 3 行，4 列，含合并区）与 ``键值``（表头在第 1 行）。
# 两张表属于两种领域类型，正是"按表指定形态"这条设计的现实来源。


def _row_config(**options: object) -> SourceConfig:
    """逐行产出的来源配置：同一形态两条绑定（键名靠 ``field_map`` 对齐）。"""
    return SourceConfig(
        source_id="tables",
        source_type="xlsx",
        root="tables",
        options=dict(options),
        bindings=[
            KindBinding(shape="sheet", kind="sheet_ref", key=["sheet"]),
            KindBinding(shape="row", kind="row_label", key=["编号"], field_map={"键": "编号"}),
            KindBinding(shape="step_row", kind="step", key=["编号"]),
            KindBinding(shape="kv_row", kind="kv", key=["键"]),
        ],
    )


def test_xlsx_row_shape_scalar_covers_every_sheet(workspace: Path) -> None:
    """标量形态 = 每张表都逐行产出，``locator`` 精确到 Excel 行号（1 起）。

    断言按**定位符**比而不是按产出的先后：事实包走的是规范序
    (``kind`` → 键 → 定位符)，与表格里的行序无关——夹具里 ``变更点`` 的标识列是
    ``A-01`` / ``2``，字典序下 ``"2" < "A-01"``，照行号要比就会误判成"产反了"。
    """
    bundle = _load("xlsx", _row_config(row_shape="row"), workspace)
    rows = _of_kind(bundle, "row_label")
    assert sorted(fact.locator.rsplit("#", 1)[-1] for fact in rows) == [
        "变更点!4",
        "变更点!5",
        "键值!2",
    ]
    by_number = {fact.key[0]: fact.payload for fact in rows}
    assert by_number["A-01"] == {
        "编号": "A-01",
        "模块": "订单",
        "变更内容": "新增取消原因",
        "负责人": "张三",
    }
    # 表事实照旧产出：逐行是**追加**，不是替换。
    assert sorted(fact.payload["sheet"] for fact in _of_kind(bundle, "sheet_ref")) == [
        "变更点",
        "键值",
    ]


def test_xlsx_row_shape_mapping_targets_named_sheets(workspace: Path) -> None:
    """对象形态按表给形态——"一个工作簿一种形态"是错的，接口清单与作业步骤是两类。"""
    bundle = _load(
        "xlsx",
        _row_config(row_shape={"变更点": "step_row", "键值": "kv_row"}),
        workspace,
    )
    assert [fact.key for fact in _of_kind(bundle, "step")] == [["2"], ["A-01"]]
    assert [fact.key for fact in _of_kind(bundle, "kv")] == [["版本"]]
    assert _of_kind(bundle, "row_label") == []


def test_xlsx_row_shape_can_exclude_a_sheet(workspace: Path) -> None:
    """空串取值 = "这张表不逐行产出"（不是"用默认形态"）。"""
    bundle = _load(
        "xlsx",
        _row_config(row_shape={"变更点": "step_row", "键值": ""}),
        workspace,
    )
    assert len(_of_kind(bundle, "step")) == 2
    assert _of_kind(bundle, "kv") == []


def test_xlsx_row_shape_without_a_header_warns_instead_of_emitting(workspace: Path) -> None:
    """没有表头行就不逐行产出：按列序号取值会在列序变化时静默串位。

    ``header_scan_rows=0`` 让表头探测必然放弃——判据本身由
    ``test_detect_header_row_gives_up_instead_of_guessing`` 覆盖，这里测的是**取舍结果**。
    """
    bundle = _load("xlsx", _row_config(row_shape="row", header_scan_rows=0), workspace)
    assert _of_kind(bundle, "row_label") == []
    assert any("未识别出表头行" in item.message for item in bundle.warnings)


def test_xlsx_probe_counts_rows_when_rows_are_enabled(workspace: Path) -> None:
    """试接入的条数要与构建同量级，否则差值会归因到错误的地方。"""
    connector = default_registry().get("xlsx")
    without = connector.probe(_rooted(_row_config(), workspace))
    with_rows = connector.probe(_rooted(_row_config(row_shape="row"), workspace))
    assert without.item_count == 2, "未开逐行时条数 = 表数"
    assert with_rows.item_count == 3, "开了逐行时条数 = 行数"
    assert "3 张表按逐行形态接入" not in with_rows.detail
    assert "2 张表按逐行形态接入，可产出 3 条" in with_rows.detail


def test_row_shape_rejects_values_that_are_not_a_shape(workspace: Path) -> None:
    """两种形态之外的取值当场失败：静默退化成"形态名是一坨字典"没人看得出来。"""
    for bad in (123, ["row"], {"": "row"}):
        with pytest.raises(ConfigurationError):
            _load("xlsx", _row_config(row_shape=bad), workspace)


def test_shape_for_falls_back_to_the_wildcard() -> None:
    assert shape_for({"*": "row"}, "任意表") == "row"
    assert shape_for({"*": "row", "变更点": "step_row"}, "变更点") == "step_row"
    assert shape_for({"*": "row", "变更点": "step_row"}, "键值") == "row"
    assert shape_for({}, "任意表") == ""


# -- assets ----------------------------------------------------------------


def test_assets_parses_figure_numbers_and_sizes(workspace: Path) -> None:
    bundle = _load("assets", assets_config(), workspace)
    figures = {fact.payload["figure"]: fact.payload for fact in _of_kind(bundle, FIGURE_KIND)}
    assert sorted(figures) == ["3-1", "3-2"]
    assert figures["3-1"]["title"] == "登录流程"
    assert (figures["3-1"]["width_px"], figures["3-1"]["height_px"]) == (800, 600)
    assert (figures["3-2"]["width_px"], figures["3-2"]["height_px"]) == (320, 96)
    assert figures["3-1"]["media_type"] == "image/png"
    assert figures["3-2"]["media_type"] == "image/jpeg"


def test_assets_registers_asset_refs_with_digests(workspace: Path) -> None:
    bundle = _load("assets", assets_config(), workspace)
    assert len(bundle.assets) == 2
    for asset in bundle.assets:
        assert len(asset.sha256) == 64
        assert asset.size_bytes > 0
        assert (workspace / "shots" / asset.relative_path).is_file()


def test_assets_reports_non_conforming_names(workspace: Path) -> None:
    bundle = _load("assets", assets_config(), workspace)
    messages = [warning.message for warning in bundle.warnings]
    assert any("screenshot-final.png" in message for message in messages)
    assert len(_of_kind(bundle, FIGURE_KIND)) == 2


def test_assets_warns_when_size_cannot_be_read(workspace: Path) -> None:
    (workspace / "shots" / "图 4-1 架构.svg").write_text(
        '<svg width="10" height="10"/>', encoding="utf-8"
    )
    bundle = _load("assets", assets_config(), workspace)
    assert any("像素尺寸" in warning.message for warning in bundle.warnings)


def test_assets_probe_counts_matching_files(workspace: Path) -> None:
    cfg = assets_config().model_copy(update={"root": str(workspace / "shots")})
    result = default_registry().get("assets").probe(cfg)
    assert result.item_count == 2
    assert len(result.warnings) == 1


# -- 产出侧的装配纪律 ------------------------------------------------------


def _emitter(**overrides: object) -> FactEmitter:
    base: dict[str, object] = {"source_id": "s", "source_type": "openapi"}
    base.update(overrides)
    return FactEmitter(SourceConfig(**base))  # type: ignore[arg-type]


def test_binding_key_must_survive_the_field_map() -> None:
    """键名写错时当场失败——否则会得到空键，两条不同记录被折叠成一条。"""
    emitter = _emitter(
        bindings=[
            KindBinding(
                shape="operation", kind="endpoint", key=["scope"], field_map={"scope": "module"}
            )
        ]
    )
    with pytest.raises(ConfigurationError) as info:
        emitter.emit("operation", {"scope": "order", "path": "/x"}, locator="L1")
    assert info.value.where["key"] == ["scope"]
    assert "module" in info.value.where["available"]


def test_unbound_shape_is_reported_once_with_a_count() -> None:
    emitter = _emitter()
    for index in range(3):
        assert emitter.emit("operation", {"path": f"/{index}"}, locator="L") is None
    warnings = emitter.bundle().warnings
    assert len(warnings) == 1
    assert warnings[0].where["count"] == "3"


def test_emitted_fact_inherits_source_priority() -> None:
    emitter = _emitter(
        priority=5,
        bindings=[KindBinding(shape="operation", kind="endpoint", key=["path"], priority=2)],
    )
    fact = emitter.emit("operation", {"path": "/x"}, locator="L1")
    assert fact is not None
    assert fact.priority == 7


def test_bundle_keeps_source_identity() -> None:
    emitter = _emitter(
        source_id="spec_a",
        bindings=[KindBinding(shape="operation", kind="endpoint", key=["path"])],
    )
    emitter.emit("operation", {"path": "/x"}, locator="L1")
    bundle = emitter.bundle()
    assert bundle.source_id == "spec_a"
    assert bundle.source_type == "openapi"
    assert bundle.digest


def test_emit_without_binding_returns_none_and_does_not_raise() -> None:
    emitter = _emitter()
    assert emitter.emit("nope", {"a": 1}, locator="L") is None


def test_configuration_error_from_a_connector_surfaces_through_run_connector(
    workspace: Path,
) -> None:
    cfg = SourceConfig(
        source_id="tables",
        source_type="xlsx",
        root=str(workspace / "tables"),
        bindings=[KindBinding(shape="sheet", kind="sheet_ref", key=["missing_field"])],
    )
    with pytest.raises(EngineError) as info:
        run_connector(default_registry().get("xlsx"), cfg)
    assert info.value.code == "E_CONFIG_INVALID"
