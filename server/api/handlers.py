"""异常处理器：把三类失败统一成同一份响应包（DD-13.3）。

三类失败
--------
====================  ==========================================================
``EngineError``       内核失败（含全部 ``E_*`` 业务码）→ 按 :data:`ERROR_STATUS` 定状态
``ApiFailure``        只在 HTTP 层成立的失败（404 项目、409 名称占用…）
``RequestValidationError``  请求体/路径/查询不合形状 → 422（FastAPI 的默认行为，
                      这里只为**换成同一份响应包**，否则前端要处理两种错误结构）
====================  ==========================================================

另外两条兜底：路由级 404/405 与未预期异常。后者的关键处理是
**不要把原始异常信息当作 ``message`` 透出去**——那是给使用者看的字段，
而 ``str(exc)`` 是给开发者看的（可能含路径、栈片段）。原始信息进日志，响应只给
``trace_id``，两者靠同一个 id 串起来。
"""

from __future__ import annotations

import logging
from typing import Final

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from docfidelity.contracts.errors import EngineError, ErrorCode

from ..errors import ApiFailure, envelope_of, new_trace_id, status_for

__all__ = [
    "TRACE_ATTRIBUTE",
    "TRACE_HEADER",
    "register",
    "trace_id_of",
]

#: 响应头 / 请求上下文中承载 trace id 的键名。
TRACE_HEADER: Final = "X-Trace-Id"
TRACE_ATTRIBUTE: Final = "docfidelity_trace_id"

_LOGGER = logging.getLogger(__name__)


def trace_id_of(request: Request) -> str:
    """取本次请求的 trace id；中间件未跑到（如异常处理器先于它执行）时现生成一个。

    现生成而不是回空串：一个没有 trace id 的错误响应，等于把"这条错误在日志里
    是哪条"这个问题丢回给使用者。
    """
    existing = getattr(request.state, TRACE_ATTRIBUTE, None)
    return existing if isinstance(existing, str) and existing else new_trace_id()


def _respond(request: Request, error: EngineError, status_code: int) -> JSONResponse:
    trace_id = trace_id_of(request)
    return JSONResponse(
        status_code=status_code,
        content=envelope_of(error, trace_id).model_dump(mode="json"),
        headers={TRACE_HEADER: trace_id},
    )


async def _engine_error(request: Request, exc: Exception) -> JSONResponse:
    error = exc if isinstance(exc, EngineError) else _internal(exc)
    return _respond(request, error, status_for(error.code))


async def _api_failure(request: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, ApiFailure):
        return _respond(request, _internal(exc), status_for(ErrorCode.INTERNAL))
    return _respond(request, exc.error, exc.status_code)


def _validation_details(exc: Exception) -> list[dict[str, object]]:
    """把 pydantic 的校验错误裁成"只含定位信息"的形状。

    ``errors()`` 会带上 ``url`` / ``input`` / ``ctx``：前者是 pydantic 文档链接，
    后两者是**未校验的原始请求体片段与异常对象**。三者都不该出现在响应里
    （DD-13.4 的响应包只放"哪儿的什么字段错了"）。
    刻意不用 ``errors(include_url=False)``：那个参数在不同 FastAPI 版本上有无不一，
    手写裁剪反而更稳。
    """
    if not isinstance(exc, RequestValidationError):
        return []
    details: list[dict[str, object]] = []
    for item in exc.errors():
        entry = dict(item)
        for dropped in ("url", "input", "ctx"):
            entry.pop(dropped, None)
        details.append(entry)
    return details


async def _validation_error(request: Request, exc: Exception) -> JSONResponse:
    error = EngineError(
        ErrorCode.SCHEMA_INVALID,
        "请求体不符合接口形状",
        where={"errors": jsonable_encoder(_validation_details(exc))},
        remediation=["按 OpenAPI 描述修正字段名与类型", "未知字段会被拒绝，不要多传"],
    )
    return _respond(request, error, 422)


async def _http_error(request: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, StarletteHTTPException):
        return _respond(request, _internal(exc), status_for(ErrorCode.INTERNAL))
    if exc.status_code == 404:
        error = EngineError(
            ErrorCode.NOT_FOUND,
            "请求的路径不存在",
            where={"path": request.url.path, "method": request.method},
            remediation=["核对路径；接口清单见 /openapi.json"],
        )
    else:
        error = EngineError(
            ErrorCode.INTERNAL,
            f"HTTP {exc.status_code}",
            where={"path": request.url.path, "method": request.method},
            remediation=["核对请求方法与路径"],
        )
    return _respond(request, error, exc.status_code)


async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
    trace_id = trace_id_of(request)
    # 原始异常只进日志：它是给开发者看的，不是给使用者看的（模块文档末段）。
    _LOGGER.exception("未处理异常 trace_id=%s path=%s", trace_id, request.url.path)
    error = EngineError(
        ErrorCode.INTERNAL,
        "服务端内部错误",
        where={"path": request.url.path},
        remediation=[f"把 trace_id（{trace_id}）提供给维护者以定位日志"],
    )
    return _respond(request, error, status_for(ErrorCode.INTERNAL))


def _internal(exc: Exception) -> EngineError:
    return EngineError(
        ErrorCode.INTERNAL,
        "服务端内部错误",
        where={"exception": type(exc).__name__},
        remediation=["把响应头里的 trace_id 提供给维护者"],
    )


def register(app: FastAPI) -> None:
    """挂载全部处理器。**在装配路由前后均可调用**（FastAPI 允许后挂）。"""
    app.add_exception_handler(EngineError, _engine_error)
    app.add_exception_handler(ApiFailure, _api_failure)
    app.add_exception_handler(RequestValidationError, _validation_error)
    app.add_exception_handler(StarletteHTTPException, _http_error)
    app.add_exception_handler(Exception, _unhandled)
