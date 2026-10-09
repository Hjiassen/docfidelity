"""二进制响应的构造（模板包 / 档案包的 zip 导出）。

为什么有个单独的小模块
----------------------
两条导出路径（``GET /templates/{tid}/package`` 与 ``GET /profiles/{pid}/export``）
必须给出**同一种** ``Content-Disposition``。文件名来自用户的资产标识，可能含中文，
而"中文文件名怎么放进 HTTP 头"这件事只有一个正确做法（RFC 5987 的 ``filename*``）——
在两条路径上各写一遍，迟早有一条退回朴素写法，而它的症状是"某些浏览器保存下来
文件名变成乱码"这种只在特定环境下复现的问题。

``filename`` 与 ``filename*`` 两个都给
------------------------------------
老浏览器只认 ``filename``，新浏览器优先 ``filename*``。给老的那个时把非 ASCII 与
引号、反斜杠一律换成 ``_``——头字段里出现裸的非 ASCII 字节不是"能显示"，是**非法**，
而非法头字段的后果由中间的代理决定，不由我们决定。
"""

from __future__ import annotations

from urllib.parse import quote

from fastapi import Response

__all__ = ["ZIP_MEDIA_TYPE", "zip_response"]

ZIP_MEDIA_TYPE = "application/zip"


def content_disposition(filename: str) -> str:
    """``attachment`` 头，同时给出 ``filename`` 与 RFC 5987 的 ``filename*``。"""
    ascii_name = "".join(
        ch if ch.isascii() and ch not in '"\\' and ch >= " " else "_" for ch in filename
    )
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename, safe='')}"


def zip_response(filename: str, payload: bytes) -> Response:
    """把一个 zip 字节串当附件返回。

    ``Content-Length`` 交给 ``Response`` 自己算：手工给长度与 ``payload`` 不一致时，
    客户端会挂在那里等剩下的字节——一个只在慢网络上现形的 bug。
    """
    return Response(
        content=payload,
        media_type=ZIP_MEDIA_TYPE,
        headers={"Content-Disposition": content_disposition(filename)},
    )
