"""前端静态托管（DD-18.1「前端静态资源由同一进程托管」）。

本文件的重点不是"能不能返回 index.html"，而是**回退的边界**：把"找不到"一律换成
``index.html`` 会让 ``GET /api/v1/typo`` 变成一个 HTTP 200 的 HTML 页面，前端拿到一坨
HTML 去 ``JSON.parse``，报的错与真正的原因隔着十万八千里。所以每一条"该回退"的用例
都配了一条"**不该**回退"的对立用例。
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from docfidelity.pipeline import StageRegistry
from server.main import create_app
from server.settings import ServerSettings

pytestmark = pytest.mark.api

INDEX_MARKER = "docfidelity-spa-index"

#: 浏览器地址栏导航带的 ``Accept``。回退判据之一就是它——没有它，``curl /settings``
#: 也会拿到一个 200 的首页，而那个请求问的是"这个文件在不在"。
NAVIGATE = {"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}


def build_dist(base: Path) -> Path:
    """造一份最小前端产物。"""
    dist = base / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text(
        f"<!doctype html><html><body>{INDEX_MARKER}</body></html>", encoding="utf-8"
    )
    (dist / "assets" / "app.js").write_text("console.log('app')", encoding="utf-8")
    return dist


@pytest.fixture
def serving(tmp_path: Path) -> Iterator[TestClient]:
    """起了静态托管的 app（带 lifespan：``TestClient`` 只有进 ``with`` 才会跑它）。"""
    dist = build_dist(tmp_path)
    app = create_app(
        settings=ServerSettings(
            _env_file=None,
            storage_root=tmp_path / "var",
            static_dir=dist,
        ),
        stages=StageRegistry(),
        start_worker=False,
    )
    with TestClient(app) as client:
        yield client


@pytest.fixture
def api_only(tmp_path: Path) -> Iterator[TestClient]:
    """没有前端产物的 app：同一份代码，只提供接口。"""
    app = create_app(
        settings=ServerSettings(
            _env_file=None,
            storage_root=tmp_path / "var",
            static_dir=tmp_path / "nowhere",
        ),
        stages=StageRegistry(),
        start_worker=False,
    )
    with TestClient(app) as client:
        yield client


# ------------------------------------------------------------------ 该回退的三条
def test_the_index_is_served_from_the_root(serving: TestClient) -> None:
    response = serving.get("/", headers=NAVIGATE)

    assert response.status_code == 200
    assert INDEX_MARKER in response.text
    assert response.headers["content-type"].startswith("text/html")


def test_an_asset_is_served_as_itself(serving: TestClient) -> None:
    """静态资源必须原样送回。把它换成 HTML，浏览器会拿 HTML 去执行。"""
    response = serving.get("/assets/app.js", headers=NAVIGATE)

    assert response.status_code == 200
    assert response.text == "console.log('app')"
    assert "javascript" in response.headers["content-type"]


def test_a_client_route_falls_back_to_the_index(serving: TestClient) -> None:
    """``/settings`` 是**客户端**路由：直接刷新时磁盘上没有这个文件，但它是一个页面。"""
    response = serving.get("/settings", headers=NAVIGATE)

    assert response.status_code == 200
    assert INDEX_MARKER in response.text


# ---------------------------------------------------------------- 不该回退的四条
def test_a_missing_api_path_is_not_dressed_up_as_a_page(serving: TestClient) -> None:
    """最要紧的一条对立断言：接口的 404 必须还是 404，且是 JSON。"""
    response = serving.get("/api/v1/nope", headers=NAVIGATE)

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/json")
    assert response.json()["code"] == "E_NOT_FOUND"


def test_a_missing_asset_stays_a_404(serving: TestClient) -> None:
    """末段带点的路径是在找静态文件，不是在导航——换成 HTML 会让 404 变成语法错误。"""
    response = serving.get("/assets/missing.js", headers=NAVIGATE)

    assert response.status_code == 404


def test_a_request_that_is_not_a_navigation_does_not_get_the_index(
    serving: TestClient,
) -> None:
    """``curl /settings``（``Accept: */*``）问的是"这个文件在不在"，答一个首页是答非所问。"""
    assert serving.get("/settings").status_code == 404


def test_a_post_is_never_answered_with_a_page(serving: TestClient) -> None:
    """写操作不该拿到一个页面。

    405 而不是 404 是可以的，而且更准确：``StaticFiles`` 对非 ``GET``/``HEAD`` 直接
    拒绝，方法不对本就该说 405。**要守的是"它不是 200 的 HTML"**。
    """
    response = serving.post("/settings", headers=NAVIGATE)

    assert response.status_code in (404, 405)
    assert "text/html" not in response.headers["content-type"]


# ------------------------------------------------------------------ 挂载顺序与形态
def test_the_mount_is_last_so_api_routes_still_win(serving: TestClient) -> None:
    """``Mount("/")`` 匹配一切 ⇒ 它必须排在最后。

    用一条**真接口**来证（``/api/v1/settings`` 走的是已注册的路由，而不是静态回退）：
    这条断言失败时，界面上的"读设置"会拿到一个 HTML 页面。
    """
    response = serving.get("/api/v1/settings", headers=NAVIGATE)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert response.json()["data_boundary"]["storage_path"]


def test_the_mount_really_is_the_last_route(serving: TestClient) -> None:
    """结构断言，与上一条互为对照：顺序是靠代码里 `attach` 的位置保证的。"""
    app = serving.app

    assert app.routes[-1].name == "web"


def test_without_build_output_only_the_api_is_served(api_only: TestClient) -> None:
    """没有前端产物时**不挂**静态目录：``GET /`` 照常是一个 JSON 404。

    挂一个空目录会让它回一个"目录里没有 index.html"的 404，而线索是"前端还没构建"——
    那句话代码说不出来，但"没有这个路径"至少不会让人以为产物存在。
    """
    response = api_only.get("/", headers=NAVIGATE)

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/json")
    assert all(route.name != "web" for route in api_only.app.routes)


def test_the_probes_are_not_shadowed_by_the_mount(serving: TestClient) -> None:
    """探针在挂载之前注册，故它们不会被页面回退吃掉（探针回 HTML 等于没有探针）。"""
    assert serving.get("/healthz", headers=NAVIGATE).json() == {"status": "alive"}
    assert serving.get("/readyz", headers=NAVIGATE).status_code == 503  # 空注册表 ⇒ 未就绪
