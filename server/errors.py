"""错误码 → HTTP 状态映射与统一响应包（DD-13.3）。

为什么错误码不新立一套
----------------------
DD-13.3 的错误码表与内核 :class:`~docfidelity.contracts.errors.ErrorCode` 是**同一张表**。
服务端若另立 ``SERVER_ERROR_CODES``，就会出现"内核抛 E_PRECHECK_FAILED、服务端不认识它"
的裂缝，而裂缝表现为 500——把一条本可给出可操作动作的失败，降级成"服务器内部错误"。
所以本模块只做**映射**（code → status）与**包装**（异常 → 响应体），不定义新码。

``remediation`` 是给使用者看的动作清单，**必须原样透出**（DD-13.3：界面按此渲染
"可以做什么"，不得由前端推断）。本模块因此不做任何裁剪或改写。
"""

from __future__ import annotations

from typing import Any, Final
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from docfidelity.contracts.errors import EngineError, ErrorCode

__all__ = [
    "DEFAULT_STATUS",
    "ERROR_STATUS",
    "ApiFailure",
    "ErrorEnvelope",
    "envelope_of",
    "new_trace_id",
    "status_for",
]

#: 未登记错误码的兜底状态。
DEFAULT_STATUS: Final = 500

#: 错误码 → HTTP 状态。**与 DD-13.3 的错误码表逐条对应**。
#:
#: 取值理由（不是随手挑的）：
#:
#: * ``E_ARTIFACT_EXPIRED`` → **410 Gone**：资源确实存在过、确实过期了。用 404 会让界面
#:   把"过期了，去重跑"和"路径写错了"混为一谈，而这两件事的可操作动作完全不同。
#: * ``E_UPLOAD_TYPE`` → 415、``E_UPLOAD_SIZE`` → 413：这两条是网关/代理也会判的，
#:   用标准码才能让"到底是谁拒的"从状态码上直接看出来。
#: * 一族 ``E_*_INVALID`` / ``E_*_MISSING`` → 422：请求**语法**没问题，是内容不合法。
#:   用 400 会把"改请求形状"和"改内容"混在一起。
#: * 冲突族（``E_*_CONFLICT`` / ``E_VERSION_CONFLICT`` / ``E_PROJECT_NAME_TAKEN`` /
#:   ``E_STATE_INVALID`` / ``E_NOT_RESUMABLE``）→ 409：请求本身合法，是与当前资源状态
#:   冲突；客户端重发同样的请求不会成功，必须先改变状态。
ERROR_STATUS: Final[dict[str, int]] = {
    # 上传
    ErrorCode.UPLOAD_TYPE: 415,
    ErrorCode.UPLOAD_SIZE: 413,
    # 模板与模板包
    ErrorCode.TEMPLATE_UNREADABLE: 422,
    ErrorCode.PACKAGE_INCOMPLETE: 422,
    ErrorCode.VERSION_CONFLICT: 409,
    # 档案
    ErrorCode.PROFILE_TEMPLATE_MISSING: 422,
    ErrorCode.PROFILE_RULESET_MISSING: 422,
    ErrorCode.PROFILE_SOURCE_MISSING: 422,
    ErrorCode.PROFILE_INVALID: 422,
    # 档案包（zip）打不开 / 里面缺东西：请求形状合法、内容不合法 → 422，与一族
    # ``E_*_INVALID`` 同理由；用 400 会把"改请求形状"和"去要一份新的包"混在一起。
    ErrorCode.PROFILE_PACKAGE_INVALID: 422,
    # 领域模型
    ErrorCode.DOMAIN_KIND_UNKNOWN: 422,
    ErrorCode.DOMAIN_KEY_MISSING: 422,
    ErrorCode.DOMAIN_TYPE_MISMATCH: 422,
    ErrorCode.DOMAIN_MODEL_INVALID: 422,
    # 事实源
    ErrorCode.SOURCE_LOAD_FAILED: 422,
    ErrorCode.SOURCE_CONFIG_INVALID: 422,
    # 配置
    ErrorCode.CONFIG_INVALID: 422,
    # 项目
    ErrorCode.NOT_FOUND: 404,
    ErrorCode.PROJECT_NAME_TAKEN: 409,
    # 作业与阶段
    ErrorCode.JOB_CONFLICT: 409,
    ErrorCode.PRECHECK_FAILED: 422,
    ErrorCode.NOT_RESUMABLE: 409,
    ErrorCode.CHECKPOINT_MISSING: 409,
    ErrorCode.STATE_INVALID: 409,
    # 版本比对
    ErrorCode.NO_BASELINE: 409,
    # 门禁
    ErrorCode.REPORT_MISSING: 409,
    # 裁决（动作合法、缺伴生数据 → 422，与一族 E_*_MISSING 同理由）
    ErrorCode.WAIVER_REASON_REQUIRED: 422,
    # 模板入库待确认项（同构：动作缺伴生数据 → 422）
    ErrorCode.REVIEW_VALUE_REQUIRED: 422,
    # 模型（非致命：走规则兜底，构建仍然成功）
    ErrorCode.MODEL_UNAVAILABLE: 503,
    # 护栏（语法合法、内容被拒 → 422，与一族 E_*_INVALID 同理由）
    ErrorCode.GUARDRAIL_REJECTED: 422,
    # 构建阶段：产物**没有产出**（写盘护栏命中），与"写出来了但不合规"分开
    ErrorCode.RENDER_BLOCKED: 422,
    # 文件与安全
    ErrorCode.PATH_ESCAPE: 400,
    ErrorCode.CONFIRM_REQUIRED: 400,
    ErrorCode.ARTIFACT_EXPIRED: 410,
    ErrorCode.NO_SNAPSHOT: 409,
    # 契约
    ErrorCode.SCHEMA_INVALID: 422,
    ErrorCode.FORMAT_VERSION: 422,
    # 兜底
    ErrorCode.INTERNAL: 500,
}


def status_for(code: str) -> int:
    """错误码 → HTTP 状态。未登记的码按 500 处理。

    不抛异常的原因：这个函数在**错误处理路径上**被调用，它自己再抛就会把一条有诊断
    价值的失败（带 code / where / remediation）换成一个无信息的 500。未登记本身是
    实现缺陷，由 ``tests/unit/test_server_errors.py`` 的完备性用例负责抓——
    运行期只保证"绝不因为映射缺失而丢掉原始错误"。
    """
    return ERROR_STATUS.get(code, DEFAULT_STATUS)


def new_trace_id() -> str:
    """生成 trace id（``tr_`` + 12 位十六进制）。

    用随机而非计数器：trace id 要能跨进程/跨重启唯一地串起一条请求的日志，
    而计数器在重启后会重复。它是**元数据**，不参与任何判定，故不受"内核禁 random
    参与逻辑"的约束（``server`` 也不在内核内）。
    """
    return f"tr_{uuid4().hex[:12]}"


class ErrorEnvelope(BaseModel):
    """统一错误响应体（DD-13.3 的 JSON 结构）。"""

    model_config = ConfigDict(extra="forbid")

    code: str = Field(description="错误码，取值见 contracts.errors.ErrorCode")
    message: str = Field(description="面向使用者的中文说明")
    where: dict[str, Any] = Field(default_factory=dict, description="结构化定位信息")
    remediation: list[str] = Field(default_factory=list, description="可执行动作清单")
    trace_id: str = Field(description="用于串日志的追踪标识")


def envelope_of(error: EngineError, trace_id: str) -> ErrorEnvelope:
    """内核异常 → 响应体。**不做裁剪**，``remediation`` 原样透出。"""
    return ErrorEnvelope(
        code=error.code,
        message=error.message,
        where=dict(error.where),
        remediation=list(error.remediation),
        trace_id=trace_id,
    )


class ApiFailure(Exception):
    """服务端自己的失败。

    ``EngineError`` 覆盖内核失败；本类覆盖**只在 HTTP 层成立**的失败（如"请求体里的
    项目 id 不存在"）。两者都经同一个响应包出去，故界面只需处理一种错误结构。
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        where: dict[str, Any] | None = None,
        remediation: list[str] | None = None,
    ) -> None:
        super().__init__(message)
        self.error: EngineError = EngineError(code, message, where=where, remediation=remediation)

    @property
    def code(self) -> str:
        return self.error.code

    @property
    def status_code(self) -> int:
        return status_for(self.error.code)

    def envelope(self, trace_id: str) -> ErrorEnvelope:
        return envelope_of(self.error, trace_id)
