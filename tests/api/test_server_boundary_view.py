"""数据边界的**数据面**（DD-13.7 / NFR-21 / 验收 30 / TASK-P0-21）。

界面上的「数据边界条」「三条可核对事实」「部署形态与数据来源」三块渲染的每一个字段都
来自服务端。这里要守的是**字段清单本身**：字段名改动不会带来任何编译期报错，界面只是
少显示一行——而"少显示一行"正是硬规则 11 要挡的形状（缺能力不得渲染成没有）。

因此本文件把三块的字段钉住：`GET /settings` 或 `GET /health` 少一个键，这里当场变红。
其中两条是**对立断言**（比"字段在不在"更要紧）：

* `external_calls` 必须由「总开关 + 端点」**推出**，不得另立一个开关——另立就允许
  "声称无外发、实际有"这种自相矛盾的状态；
* `demo_seed` 在**没有配置装载**时必须是 `None` 而不是"装了 0 份"——后者读起来像失败。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server.main import create_app
from server.settings import ServerSettings
from tests.fixtures.server_fixture import build_registry

pytestmark = pytest.mark.api

#: 数据边界条 / 三条事实 / 部署形态卡渲染的字段（少一个就会少一行，而不报错）。
BOUNDARY_FIELDS = frozenset(
    {"deployment", "model_enabled", "model_base_url", "external_calls", "storage_path"}
)


def make_app(
    tmp_path: Path,
    *,
    start_worker: bool | None = False,
    settings_start_worker: bool = True,
    **settings: Any,
) -> FastAPI:
    """按给定姿态构造应用。

    两个 ``start_worker`` 是**两个层级**，不能合成一个：``settings_start_worker`` 是配置
    （容器形态走的就是这条，compose 不传 argv 开关），``start_worker`` 是 ``create_app``
    的显式参数（**压过**配置）。缺省把显式参数给成 ``False`` 只是让用例不去起后台线程
    ——它与后台线程赛跑，而这里要断言的是"探针报了什么"，不是"线程跑没跑"。
    """
    return create_app(
        settings=ServerSettings(
            _env_file=None,
            storage_root=tmp_path / "var",
            start_worker=settings_start_worker,
            **settings,
        ),
        repository=None,
        stages=build_registry(),
        start_worker=start_worker,
    )


def _keys(node: Any, *, depth: int = 0) -> set[str]:
    """递归收集 JSON 里的**全部**键名（密钥检测用；只找键、不看值）。"""
    found: set[str] = set()
    if depth > 8:
        return found
    if isinstance(node, dict):
        for key, value in node.items():
            found.add(str(key).lower())
            found |= _keys(value, depth=depth + 1)
    elif isinstance(node, list):
        for item in node:
            found |= _keys(item, depth=depth + 1)
    return found


# ---------------------------------------------------------------- 数据边界字段面
def test_the_boundary_view_carries_every_field_the_bar_renders(tmp_path: Path) -> None:
    with TestClient(make_app(tmp_path)) as client:
        payload = client.get("/api/v1/settings").json()

    boundary = payload["data_boundary"]
    assert set(boundary) >= BOUNDARY_FIELDS
    # 值也要能在不补默认值的前提下渲染：缺了就给不出"落在哪"这个答案。
    assert boundary["storage_path"]
    assert boundary["deployment"] in {"local", "intranet", "public"}


@pytest.mark.parametrize(
    ("enabled", "base_url", "expected"),
    [
        (False, None, False),
        (False, "http://127.0.0.1:11434/v1", False),
        (True, None, False),
        (True, "http://127.0.0.1:11434/v1", True),
    ],
)
def test_the_egress_flag_is_derived_not_declared(
    tmp_path: Path, enabled: bool, base_url: str | None, expected: bool
) -> None:
    """``external_calls`` = 总开关 **且** 端点；四种组合逐一钉住，界面不自己算这份结论。"""
    app = make_app(tmp_path, model_enabled=enabled, model_base_url=base_url)

    with TestClient(app) as client:
        boundary = client.get("/api/v1/settings").json()["data_boundary"]

    assert boundary["external_calls"] is expected
    assert boundary["external_calls"] == bool(enabled and base_url)


def test_the_settings_response_never_carries_a_secret(tmp_path: Path) -> None:
    """加密钥之后整份响应里不得出现密钥类键名（DD-13.7：「永不回密钥」）。"""
    app = make_app(
        tmp_path,
        model_enabled=True,
        model_base_url="http://127.0.0.1:11434/v1",
        model_name="local-27b",
        model_api_key="sk-should-never-leave",  # type: ignore[arg-type]
    )

    with TestClient(app) as client:
        response = client.get("/api/v1/settings")

    assert "sk-should-never-leave" not in response.text
    present = _keys(response.json())
    assert not {key for key in present if "api_key" in key or "secret" in key or "token" in key}


def test_the_posture_is_reported_as_an_effective_value(tmp_path: Path) -> None:
    """确定性姿态报**生效值**：`office_enabled` 取三级合并结果，`office_reason` 给成因。

    报开关值（用户刚写的那个）看不出"配了没生效"，而那正是这一组字段存在的理由
    （DD-16.3）。故这里断言的是**与派生方法一致**，而不是与传入的开关一致。
    """
    app = make_app(tmp_path, deterministic_mode=True)

    with TestClient(app) as client:
        posture = client.get("/api/v1/settings").json()["deterministic"]

    assert set(posture) == {"mode", "office_enabled", "office_reason"}
    assert posture["mode"] is True
    # 确定性姿态压住了 Office ⇒ 生效值为假，且带着"是姿态压的"这条成因。
    assert posture["office_enabled"] is False
    assert posture["office_reason"]


def test_the_retention_policy_is_visible_so_it_can_be_checked(tmp_path: Path) -> None:
    """保留策略三档都要在响应里（设置页那张表逐行渲染它们）。"""
    app = make_app(
        tmp_path,
        retention_uploads_days=7,
        retention_workspace_days=8,
        retention_artifacts_days=9,
    )

    with TestClient(app) as client:
        policy = client.get("/api/v1/settings").json()["retention"]

    assert (policy["uploads_days"], policy["workspace_days"], policy["artifacts_days"]) == (7, 8, 9)


# ------------------------------------------------------------ 部署形态（/health）
def test_health_reports_the_deployment_shape(tmp_path: Path) -> None:
    """两块事实必须能读到：谁在消费作业、首启装载装了什么。"""
    with TestClient(make_app(tmp_path)) as client:
        payload = client.get("/health").json()

    assert payload["worker_mode"] in {"in-process", "external"}
    assert "demo_seed" in payload
    assert isinstance(payload["storage_root"], str) and payload["storage_root"]


def test_worker_mode_flips_with_the_in_process_worker(tmp_path: Path) -> None:
    """两处开关的三个组合——`external` 只说明**本进程不消费**，不等于"外部进程健在"。

    第三条是判据本身：显式参数**压过**配置（`_should_start_worker`）。反过来会让
    compose 里那行 `DOCFIDELITY_START_WORKER=false` 变成"配了但没生效"的来源之一。
    """
    cases: list[tuple[bool | None, bool, str]] = [
        (None, True, "in-process"),  # 配置开着、没人反对
        (None, False, "external"),  # 配置关掉（容器形态）
        (True, False, "in-process"),  # 显式压过配置
    ]
    for index, (explicit, configured, expected) in enumerate(cases):
        app = make_app(
            tmp_path / f"case{index}",
            start_worker=explicit,
            settings_start_worker=configured,
        )
        with TestClient(app) as client:
            assert (
                client.get("/health").json()["worker_mode"] == expected
            ), f"explicit={explicit} configured={configured}"


def test_an_unconfigured_seed_is_absent_not_zero(tmp_path: Path) -> None:
    """没配装载 ⇒ `demo_seed` 为 `None`。

    若改成"装了 0 份"，界面上会与"配了但一份都没装成"长得一样——那是两种完全不同的
    状态（前者什么都不用做，后者要去查目录与日志）。
    """
    with TestClient(make_app(tmp_path)) as client:
        payload = client.get("/health").json()

    assert payload["demo_seed"] is None


def test_a_configured_seed_reports_its_outcome(tmp_path: Path) -> None:
    """配了装载就要给出三个字段：跳过了没有 / 装了什么 / 失败了什么。

    这里用一个**空目录**：不装任何东西不会失败，于是形状被单独钉住（真装载的端到端
    在 `tests/integration/test_one_click.py`，那份要解真 docx，慢）。
    """
    empty = tmp_path / "demo"
    empty.mkdir()
    app = make_app(tmp_path, seed_demo_templates=True, demo_templates_dir=empty)

    with TestClient(app) as client:
        seed = client.get("/health").json()["demo_seed"]

    assert seed is not None
    assert set(seed) == {"skipped", "seeded", "errors"}
    assert seed["skipped"] is False
    assert seed["seeded"] == []
    assert seed["errors"] == []
