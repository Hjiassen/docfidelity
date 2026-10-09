"""前端静态资源托管（DD-18.1 的「前端静态资源由同一进程托管」）。

为什么是"同源托管"而不是"另起一个 nginx"
----------------------------------------
前端的接口调用全走**相对路径**（``/api/v1/...``，见 ``web/vite.config.ts`` 的说明）。
同源托管让"不引入 CORS"这条从开发期一路成立到生产：一旦分成两个来源，后端就得为
浏览器开后门，而那个头一旦加上，就再也没人敢删。

本模块只做三件事
----------------
1. 把构建产物目录挂到根路径（**必须挂在所有 API 路由之后**，见 :func:`attach`）。
2. 前端路由（``/settings``、``/jobs/<id>`` 这类**客户端**路由）直接刷新时回 ``index.html``。
3. 其余路径照常 404。

第 3 条是唯一容易做错的地方，也是本模块存在的理由：把"找不到"一律回 ``index.html``
会让 ``GET /api/v1/typo`` 变成一个 **HTTP 200 的 HTML 页面**——前端拿到一坨 HTML 去
``JSON.parse``，报的错与真正的原因（路径写错了）隔着十万八千里。故回退有三道闸门：
只对 ``GET``/``HEAD``、只对**不带 API 前缀**的路径、只对**看起来是一次页面导航**
的请求（末段不含点、且 ``Accept`` 要 HTML）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

from fastapi import FastAPI
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import Response
from starlette.staticfiles import StaticFiles
from starlette.types import Scope

__all__ = ["API_PATH_PREFIX", "INDEX_FILENAME", "SpaStaticFiles", "attach"]

#: 接口路径前缀。回退**永不**跨过它——跨过去就是把接口的 404 说成"页面存在"。
API_PATH_PREFIX: Final = "/api/"

#: 前端单页的入口文件名（Vite 的产物固定叫这个）。
INDEX_FILENAME: Final = "index.html"

#: 允许回退到 ``index.html`` 的方法。``POST /whatever`` 不该拿到一个页面。
_PAGE_METHODS: Final[frozenset[str]] = frozenset({"GET", "HEAD"})

#: 回退时额外排除的路径前缀。它们要么是接口（已注册为路由，正常不会走到这里），
#: 要么是 FastAPI 自带的文档页——把它们变成"前端首页"会让人以为文档页被删了。
_RESERVED_PREFIXES: Final[tuple[str, ...]] = (
    API_PATH_PREFIX,
    "/openapi.json",
    "/docs",
    "/redoc",
    "/health",
    "/healthz",
    "/readyz",
)


class SpaStaticFiles(StaticFiles):
    """``StaticFiles`` + 单页回退。

    继承而不是另写一个 ASGI 应用：静态文件的服务、``Content-Type``、``ETag``、
    Range 请求这些都不是本模块要解决的问题，重写一遍只会多出一份会漂移的实现。
    """

    def __init__(self, *, directory: Path | str, index: str = INDEX_FILENAME) -> None:
        super().__init__(directory=str(directory), html=True)
        self._index = index

    async def get_response(self, path: str, scope: Scope) -> Response:
        try:
            response = await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            if exc.status_code != 404 or not self._may_fall_back(path, scope):
                raise
            return await super().get_response(self._index, scope)
        # ``html=True`` 时目录缺 ``index.html`` 会回 404，而不是抛异常——两条路都要管。
        if getattr(response, "status_code", 200) == 404 and self._may_fall_back(path, scope):
            return await super().get_response(self._index, scope)
        return response

    @staticmethod
    def _may_fall_back(path: str, scope: Scope) -> bool:
        """这次未命中是否该当作"客户端路由"?（三道闸门的实现，见模块文档）"""
        if scope.get("method") not in _PAGE_METHODS:
            return False
        # ``StaticFiles`` 交给我们的 path 是**平台相关**的：它内部走
        # ``os.path.normpath(os.path.join("/", path))``，于是在 Windows 上
        # ``/api/v1/nope`` 会以 ``api\\v1\\nope`` 的形态进来，前缀比较当场失配
        # ——症状是"Linux/CI 全绿，Windows 上接口的 404 变成 HTML 200"。
        # 判据一律先归一成 POSIX 分隔符再比。
        resolved = "/" + path.replace("\\", "/").lstrip("/")
        if resolved.startswith(_RESERVED_PREFIXES):
            return False
        # 末段带点 = 在找一个静态文件（``.js`` / ``.css`` / ``.map``）。把它换成 HTML，
        # 浏览器会拿 HTML 去执行，报的是语法错误而不是 404。
        if "." in resolved.rsplit("/", 1)[-1]:
            return False
        return SpaStaticFiles._accepts_html(scope)

    @staticmethod
    def _accepts_html(scope: Scope) -> bool:
        """``Accept`` 里要有 html。

        单独判它是为了放过 ``curl`` / 探针 / ``<img src>`` 这类**不是页面导航**的请求：
        它们问"这个文件在不在"，回一个 200 的首页是答非所问（而且会让"部署漏了产物"
        在监控上看起来一切正常）。浏览器地址栏导航一定带 ``text/html``。
        """
        for key, value in scope.get("headers") or ():
            if key == b"accept":
                return b"text/html" in value.lower()
        return False


def attach(app: FastAPI, directory: Path | str) -> None:
    """把前端目录挂到 ``/``。**必须在全部 API 路由注册之后调用。**

    Starlette 按 ``app.routes`` 的顺序匹配，而 ``Mount("/")`` 匹配一切——先挂它，
    后面注册的接口就永远轮不上。这条顺序约束由 :mod:`server.main` 的调用位置保证，
    并由 ``tests/api/test_server_static.py`` 钉住（那里有一条"接口仍然可达"的用例）。
    """
    app.mount("/", SpaStaticFiles(directory=directory), name="web")
