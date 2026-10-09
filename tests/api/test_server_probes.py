"""健康探针（DD-18.2）：``/healthz``（进程）与 ``/readyz``（就绪）。

两者的**处置相反**，这是本文件要守住的东西：存活失败 = 重启，就绪失败 = 摘流量。
所以除了"能回 200"，还要有"该 503 的时候真的 503"，以及"**不该**拖垮就绪的东西
没有进判定"（模型、示例装载）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from docfidelity.pipeline import StageRegistry
from server.main import _repository_readable, _workspace_writable, create_app
from server.settings import ServerSettings
from server.storage import MemoryRepository, Repository
from tests.fixtures.server_fixture import build_registry

pytestmark = pytest.mark.api


class UnreadableRepository(MemoryRepository):
    """读一次就炸的仓储：代表"库连不上"（连接串错、库被删、库结构比代码旧）。"""

    def list_projects(self, *, include_archived: bool = False) -> list[Any]:
        raise RuntimeError("database is locked")


def make_app(
    tmp_path: Path,
    *,
    stages: StageRegistry | None = None,
    repository: Repository | None = None,
    **settings: Any,
) -> FastAPI:
    """按给定姿态构造应用（``TestClient`` 只在这里包一次，生命周期交给调用方）。"""
    return create_app(
        settings=ServerSettings(_env_file=None, storage_root=tmp_path / "var", **settings),
        repository=repository,
        stages=build_registry() if stages is None else stages,
        start_worker=False,
    )


# --------------------------------------------------------------------- /healthz
def test_the_liveness_probe_only_says_the_process_is_alive(tmp_path: Path) -> None:
    """存活探针**不碰磁盘也不碰库**：它回答的问题只有"这个进程还在吗"。"""
    with TestClient(make_app(tmp_path)) as client:
        response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "alive"}


def test_the_liveness_probe_survives_an_unreadable_repository(tmp_path: Path) -> None:
    """库读不了时存活探针**仍须** 200。

    反过来（库坏了 ⇒ 存活探针挂掉 ⇒ 编排器重启全部实例）是这类探针最经典的误用：
    重启一百次也不会让数据库好起来，只会把一次抖动放大成一次全站重启。
    """
    app = make_app(tmp_path, repository=UnreadableRepository())

    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200
        assert client.get("/readyz").status_code == 503


# ---------------------------------------------------------------------- /readyz
def test_readiness_passes_when_the_service_can_accept_a_build(tmp_path: Path) -> None:
    with TestClient(make_app(tmp_path)) as client:
        response = client.get("/readyz")
    payload = response.json()

    assert response.status_code == 200
    assert payload["status"] == "ready"
    assert set(payload["checks"]) == {"repository", "workspace", "stages"}
    assert all(item["ok"] for item in payload["checks"].values())


def test_readiness_fails_when_no_stage_is_registered(tmp_path: Path) -> None:
    """阶段没注册 ⇒ ``POST /jobs`` 必然被拒（``E_PRECHECK_FAILED``），那就是不就绪。"""
    with TestClient(make_app(tmp_path, stages=StageRegistry())) as client:
        response = client.get("/readyz")

    assert response.status_code == 503
    assert response.json()["checks"]["stages"]["ok"] is False
    assert "render" in response.json()["checks"]["stages"]["detail"]


def test_readiness_fails_when_the_repository_cannot_be_read(tmp_path: Path) -> None:
    app = make_app(tmp_path, repository=UnreadableRepository())

    with TestClient(app) as client:
        response = client.get("/readyz")

    assert response.status_code == 503
    assert response.json()["checks"]["repository"]["ok"] is False
    assert "RuntimeError" in response.json()["checks"]["repository"]["detail"]


def test_readiness_fails_when_the_workspace_is_not_usable(tmp_path: Path) -> None:
    """工作区写不了 ⇒ 产物落不下。用"目录不存在"代表不可用（真造只读目录要改 ACL）。"""
    result = _workspace_writable(tmp_path / "no" / "such" / "dir")

    assert result["ok"] is False
    assert "Error" in str(result["detail"])


class _StateStub:
    """只为给 ``_repository_readable`` 递一个带 ``repository`` 的对象。"""

    def __init__(self, repository: Repository) -> None:
        self.repository = repository


def test_the_repository_probe_is_read_only() -> None:
    """探活不能写数据：写探测会在每次探针调用时留下记录，把审计与保留策略都搅浑。"""
    repository = MemoryRepository()
    before = repository.list_projects()

    result = _repository_readable(_StateStub(repository))  # type: ignore[arg-type]

    assert result["ok"] is True
    assert repository.list_projects() == before


# ------------------------------------------------------ 不进就绪判定的两样东西
def test_a_configured_model_does_not_gate_readiness(tmp_path: Path) -> None:
    """模型是**可选能力**：缺了退回确定性会话、产出依然完整。

    把它写进就绪判定，会让"模型端点一时连不上"变成"整站不健康"——而那个端点连不上
    时服务明明还能把文档做出来（NFR-13 的硬规则）。端点故意填一个连不上的地址：
    就绪探针若真去外呼，这条用例会慢下来甚至变红。
    """
    app = make_app(
        tmp_path,
        model_enabled=True,
        model_base_url="http://127.0.0.1:1/definitely-not-a-model",
        model_name="ghost",
    )

    with TestClient(app) as client:
        response = client.get("/readyz")

    assert response.status_code == 200
    assert response.json()["model_enabled"] is True


def test_a_missing_demo_directory_does_not_gate_readiness(tmp_path: Path) -> None:
    """示例装载是演示资产，不是服务能力。"""
    app = make_app(
        tmp_path,
        seed_demo_templates=True,
        demo_templates_dir=tmp_path / "no" / "such" / "templates",
    )

    with TestClient(app) as client:
        assert client.get("/readyz").status_code == 200
        assert client.get("/health").json()["demo_seed"] == {
            "skipped": False,
            "seeded": [],
            "errors": [],
        }


# ----------------------------------------------------------------------- /health
def test_health_reports_where_the_worker_lives(tmp_path: Path) -> None:
    """``worker_running`` 与 ``worker_mode`` 是两件事。

    "本进程有没有在消费作业" ≠ "这个部署有没有消费者"：另起 ``df-worker`` 时本进程关掉
    线程，作业照样有人跑。只看前者的部署方会以为自己忘了开 worker，而去把线程打开——
    那才是真正的问题（两个消费者抢同一队列）。
    """
    external = create_app(
        settings=ServerSettings(_env_file=None, storage_root=tmp_path / "external"),
        stages=build_registry(),
        start_worker=False,
    )
    in_process = create_app(
        settings=ServerSettings(_env_file=None, storage_root=tmp_path / "in-process"),
        stages=build_registry(),
        start_worker=True,
    )

    assert external.state.worker is None
    assert in_process.state.worker is not None

    with TestClient(external) as client:
        payload = client.get("/health").json()
    assert payload["worker_mode"] == "external"
    assert payload["worker_running"] is False


def test_health_omits_the_seed_report_when_seeding_is_off(tmp_path: Path) -> None:
    """没配装载就不出现这个字段：一个恒为 ``null`` 的字段会被读成"装载失败了"。"""
    with TestClient(make_app(tmp_path)) as client:
        assert client.get("/health").json()["demo_seed"] is None
