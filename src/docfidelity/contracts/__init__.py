"""契约层 —— 依赖之根。

本模块是**对外承诺的唯一载体**（需求 10.6）：全部对外数据模型与格式规范在此定义，
只依赖 ``pydantic``，被内核所有模块依赖；反向依赖一律禁止（见 ``.importlinter``）。

子模块
------
base        统一基类 ``FormatModel`` 与格式版本策略
errors      统一异常 ``EngineError`` 与错误码
template    模板包契约（manifest / styles / page_setup / numbering / outline / blocks / placeholders / fingerprint）
ir          内容中间表示 ``ContentIR`` 与内容单元
domain      领域模型 ``DomainModel`` / ``DomainObject`` / ``FieldSchema``
profile     文档类型档案 ``DocTypeProfile``
fact        事实包 ``FactBundle`` / ``FactSnapshot``
job         作业、阶段、事件、决策、模型调用留痕
qa          门禁：规则、判定、豁免、报告、追溯边
config      项目配置：文档条目、分页策略、术语表

.. note::
   本包内**不 re-export 子模块符号**。使用方显式 ``from docfidelity.contracts.ir import ContentIR``，
   以免依赖面被隐式放大。
"""

from .base import DEFAULT_FORMAT_VERSION, FORMAT_CURRENT, FormatModel
from .errors import DegradableError, EngineError, EngineErrorInfo, ErrorCode

__all__ = [
    "DEFAULT_FORMAT_VERSION",
    "FORMAT_CURRENT",
    "DegradableError",
    "EngineError",
    "EngineErrorInfo",
    "ErrorCode",
    "FormatModel",
]
