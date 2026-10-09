"""标识生成（``p_`` / ``j_`` / ``u_`` / ``adj_`` / ``wv_`` 前缀 + 12 位十六进制）。

为什么单独一个模块
------------------
用例面（``services``）与裁决面（``adjudication``）都要生成 id，而后者被前者 import
——把生成器留在 ``services`` 里就形成环。id 的形状是**接口可见的事实**（它出现在响应体里、
出现在 URL 路径参数里，且必须 URL 安全），故两处必须同源：各写一份的表现是
"有的标识 12 位、有的 8 位"，而使用者会照着它写正则。
"""

from __future__ import annotations

from uuid import uuid4

__all__ = ["new_id"]


def new_id(prefix: str) -> str:
    """生成标识。``prefix`` 自带分隔符（如 ``"p_"``），此处不补。"""
    return f"{prefix}{uuid4().hex[:12]}"
