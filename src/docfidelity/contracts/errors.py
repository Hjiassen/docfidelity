"""统一异常模型与错误码（DD-3.3 / DD-13.3）。

纪律
----
1. 内核任何失败都必须转成 :class:`EngineError`，不得抛裸 ``Exception``。
2. ``fatal=False`` 的失败**不得**中断阶段，只进裁决队列（NFR-26）——
   典型场景是模型服务不可用，此时必须走规则默认值继续构建。
"""

from __future__ import annotations

from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field


class ErrorCode:
    """错误码常量集合，与设计文档 DD-13.3 的错误码表逐条对应。

    命名规则：``E_<领域>_<语义>``。新增错误码必须同步更新本类与设计文档，
    否则前端无法给出可操作动作。
    """

    # -- 上传 --------------------------------------------------------------
    UPLOAD_TYPE: Final = "E_UPLOAD_TYPE"
    UPLOAD_SIZE: Final = "E_UPLOAD_SIZE"

    # -- 模板与模板包 ------------------------------------------------------
    TEMPLATE_UNREADABLE: Final = "E_TEMPLATE_UNREADABLE"
    PACKAGE_INCOMPLETE: Final = "E_PACKAGE_INCOMPLETE"
    VERSION_CONFLICT: Final = "E_VERSION_CONFLICT"

    # -- 文档类型档案 ------------------------------------------------------
    PROFILE_TEMPLATE_MISSING: Final = "E_PROFILE_TEMPLATE_MISSING"
    PROFILE_RULESET_MISSING: Final = "E_PROFILE_RULESET_MISSING"
    PROFILE_SOURCE_MISSING: Final = "E_PROFILE_SOURCE_MISSING"
    PROFILE_INVALID: Final = "E_PROFILE_INVALID"
    #: **档案包**（zip）本身不可用（DD-13.2 的 ``POST /profiles/import`` / 验收 45）：
    #: 不是 zip、条目路径越界（绝对路径 / ``..``）、缺 ``profile.json``、包内档案与
    #: 它引用的模板包对不上。
    #: 与 ``PROFILE_INVALID`` 的分工是**包 vs 本体**：那条说"你交上来的这份档案内容
    #: 不成立"，这条说"这个包我打不开 / 里面缺东西"。一族 E_*_INVALID 的第三例
    #: （前两例见 ``DOMAIN_MODEL_INVALID`` / ``SOURCE_CONFIG_INVALID``）。
    PROFILE_PACKAGE_INVALID: Final = "E_PROFILE_PACKAGE_INVALID"

    # -- 领域模型 ----------------------------------------------------------
    #: 档案引用了未登记的领域类型（``for_each`` / ``children[].for_each``）。
    #: 见 DD-6.1 与 DD-7.5 的双向校验（FR-M2-23）。
    DOMAIN_KIND_UNKNOWN: Final = "E_DOMAIN_KIND_UNKNOWN"
    #: 领域对象缺少标识键取值——接入阶段即失败（FR-M1-12）。
    #: 标识键是去重、追溯与版本比对的唯一句柄，缺了它对象无法被引用。
    DOMAIN_KEY_MISSING: Final = "E_DOMAIN_KEY_MISSING"
    #: 字段值与该领域模型声明的类型不符（DD-6.3）。
    DOMAIN_TYPE_MISMATCH: Final = "E_DOMAIN_TYPE_MISMATCH"
    #: **声明本身**不可用：``kind`` 缺失 / 含非法字符，或废弃未给理由。
    #: 与 :data:`DOMAIN_KIND_UNKNOWN` 的分工是**声明 vs 引用**：那条说"档案引用的
    #: 类型库里没有"，这条说"你交上来的这份声明不成立"。与 ``PROFILE_INVALID`` /
    #: ``SOURCE_CONFIG_INVALID`` 同构（一族 E_*_INVALID：请求语法没问题，内容不合法）。
    DOMAIN_MODEL_INVALID: Final = "E_DOMAIN_MODEL_INVALID"

    # -- 事实源 ------------------------------------------------------------
    SOURCE_LOAD_FAILED: Final = "E_SOURCE_LOAD_FAILED"
    #: 事实源配置本身不可用：``source_type`` 没有对应的接入器。
    #: 与 ``SOURCE_LOAD_FAILED`` 的分工是**配置期 vs 接入期**：本码在登记来源时
    #: 就能判出来（配置里写了一个不存在的接入器名），不依赖工作区里有没有那份文件；
    #: ``SOURCE_LOAD_FAILED`` 是配置没问题、真去读文件时失败了（带文件行定位）。
    #: 与 ``CONFIG_INVALID`` 的分工：后者是项目配置（文档族 / 路由 / 术语）不合规，
    #: 本码只针对单条事实源声明。
    SOURCE_CONFIG_INVALID: Final = "E_SOURCE_CONFIG_INVALID"

    # -- 配置 --------------------------------------------------------------
    CONFIG_INVALID: Final = "E_CONFIG_INVALID"

    # -- 项目（DD-13.2 的 projects 端点）-----------------------------------
    NOT_FOUND: Final = "E_NOT_FOUND"
    PROJECT_NAME_TAKEN: Final = "E_PROJECT_NAME_TAKEN"

    # -- 作业与阶段 --------------------------------------------------------
    JOB_CONFLICT: Final = "E_JOB_CONFLICT"
    PRECHECK_FAILED: Final = "E_PRECHECK_FAILED"
    NOT_RESUMABLE: Final = "E_NOT_RESUMABLE"
    CHECKPOINT_MISSING: Final = "E_CHECKPOINT_MISSING"
    #: 动作在当前作业状态下不合法（DD-13.2 的 cancel / retry 等端点）。
    #: 与 ``JOB_CONFLICT`` 的分工：``JOB_CONFLICT`` 是"同项目已有作业在跑"（资源冲突），
    #: 本码是"这个作业自己不在可执行该动作的状态"（状态冲突）。
    STATE_INVALID: Final = "E_STATE_INVALID"

    # -- 版本比对 ----------------------------------------------------------
    NO_BASELINE: Final = "E_NO_BASELINE"

    # -- 门禁 --------------------------------------------------------------
    REPORT_MISSING: Final = "E_REPORT_MISSING"

    # -- 裁决（DD-13.2 的 ``POST /findings/{fid}/decisions``）----------------
    #: 豁免未给理由。三种规范动作里**只有** ``waive`` 会改变门禁结论，
    #: 故也只有它被强制要求理由（FR-M7-06 / NFR-15）。
    #: 与 ``SCHEMA_INVALID`` 的分工：这不是"字段类型不对"，是"这个动作缺了它
    #: 语义上必需的伴生数据"——把它折成 422 校验错会让界面提示"字段必填"，
    #: 而使用者真正要理解的是"豁免必须留下理由"。
    WAIVER_REASON_REQUIRED: Final = "E_WAIVER_REASON_REQUIRED"

    # -- 模板入库待确认项（DD-7.1 第 4 步 / ``POST /templates/imports/{id}/decisions``）--
    #: 待确认项的规范动作是 ``accept``（接受解析结果）或 ``specify``（指定取值），
    #: **只有** ``specify`` 需要带取值；缺了它这一条就没有"确认成什么"的内容。
    #: 与 ``WAIVER_REASON_REQUIRED`` 同构（动作缺伴生数据 → 422），只是所属流程不同：
    #: 那条管门禁结论，这条管模板入库的解析结果确认。
    REVIEW_VALUE_REQUIRED: Final = "E_REVIEW_VALUE_REQUIRED"

    # -- 模型 --------------------------------------------------------------
    MODEL_UNAVAILABLE: Final = "E_MODEL_UNAVAILABLE"

    # -- 护栏（DD-12.5 / 需求 5.6）-----------------------------------------
    #: 指令被输入护栏拒绝：要求编造事实 / 修改模板原件 / 绕过门禁。
    #: 与 ``CONFIRM_REQUIRED`` 的分工：后者是"危险动作缺人工确认"（可确认后继续），
    #: 本码是"这件事**不允许**做"（确认了也不做）。
    GUARDRAIL_REJECTED: Final = "E_GUARDRAIL_REJECTED"

    # -- 构建阶段（docfidelity.builder 的参考构建包）------------------------
    #: 渲染**写盘护栏**命中，产物未落盘（DD-10.7）。
    #: 与"门禁判定为 blocked"的分工：后者是"产物写出来了但不合规"（报告的事），
    #: 本码是"产物根本没写出来"（阶段的事）——没有文件可交付，作业只能失败。
    RENDER_BLOCKED: Final = "E_RENDER_BLOCKED"

    # -- 文件与安全 --------------------------------------------------------
    PATH_ESCAPE: Final = "E_PATH_ESCAPE"
    CONFIRM_REQUIRED: Final = "E_CONFIRM_REQUIRED"
    ARTIFACT_EXPIRED: Final = "E_ARTIFACT_EXPIRED"
    NO_SNAPSHOT: Final = "E_NO_SNAPSHOT"

    # -- 契约 --------------------------------------------------------------
    SCHEMA_INVALID: Final = "E_SCHEMA_INVALID"
    FORMAT_VERSION: Final = "E_FORMAT_VERSION"

    # -- 兜底 --------------------------------------------------------------
    INTERNAL: Final = "E_INTERNAL"


class EngineError(Exception):
    """内核统一异常。

    Parameters
    ----------
    code:
        机器可读错误码，取值见 :class:`ErrorCode`。
    message:
        面向使用者的中文说明。
    where:
        结构化定位信息（阶段 / 文件 / 行 / 单元 id 等）。
    remediation:
        可执行动作清单。界面按此渲染"可以做什么"，**不得**由前端自行推断。
    fatal:
        ``False`` 表示可降级继续。**不得**用它中断阶段。
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        where: dict[str, Any] | None = None,
        remediation: list[str] | None = None,
        fatal: bool = True,
    ) -> None:
        super().__init__(message)
        self.code: str = code
        self.message: str = message
        self.where: dict[str, Any] = dict(where or {})
        self.remediation: list[str] = list(remediation or [])
        self.fatal: bool = fatal

    def to_info(self) -> EngineErrorInfo:
        """转换为可持久化快照。"""
        return EngineErrorInfo(
            code=self.code,
            message=self.message,
            where=self.where,
            remediation=self.remediation,
            fatal=self.fatal,
        )

    def __str__(self) -> str:
        return f"[{self.code}] {self.message}"

    def __repr__(self) -> str:
        return f"EngineError(code={self.code!r}, message={self.message!r}, fatal={self.fatal!r})"


class DegradableError(EngineError):
    """可降级错误：构建不因它失败，只进裁决队列。

    例：模型服务不可用 —— 走规则默认值并在产物中标记"待确认"（NFR-26 / 验收 39）。
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        where: dict[str, Any] | None = None,
        remediation: list[str] | None = None,
    ) -> None:
        super().__init__(code, message, where=where, remediation=remediation, fatal=False)


class FormatVersionError(EngineError):
    """格式版本不兼容（主版本不同，拒绝加载）。"""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        where: dict[str, Any] | None = None,
        remediation: list[str] | None = None,
    ) -> None:
        super().__init__(code, message, where=where, remediation=remediation, fatal=True)


class ConfigurationError(EngineError):
    """配置校验未通过。"""

    def __init__(
        self,
        message: str,
        *,
        where: dict[str, Any] | None = None,
        remediation: list[str] | None = None,
    ) -> None:
        super().__init__(
            ErrorCode.CONFIG_INVALID, message, where=where, remediation=remediation, fatal=True
        )


class SchemaValidationError(EngineError):
    """契约 schema 校验未通过。"""

    def __init__(
        self,
        message: str,
        *,
        where: dict[str, Any] | None = None,
        remediation: list[str] | None = None,
    ) -> None:
        super().__init__(
            ErrorCode.SCHEMA_INVALID, message, where=where, remediation=remediation, fatal=True
        )


class EngineErrorInfo(BaseModel):
    """可持久化的错误快照。

    落库位置：``stages.error``。与 :class:`EngineError` 分离的原因是把**异常**
    （控制流）与**快照**（数据）分开——快照可以直接进 JSON 响应与数据库，
    异常对象不行。
    """

    model_config = ConfigDict(extra="forbid")

    code: str = Field(description="错误码，取值见 ErrorCode")
    message: str = Field(description="面向使用者的中文说明")
    where: dict[str, Any] = Field(default_factory=dict, description="结构化定位信息")
    remediation: list[str] = Field(default_factory=list, description="可执行动作清单")
    fatal: bool = Field(default=True, description="False 表示可降级继续，不作为失败终态")

    @classmethod
    def from_error(cls, error: EngineError) -> EngineErrorInfo:
        return EngineErrorInfo(
            code=error.code,
            message=error.message,
            where=dict(error.where),
            remediation=list(error.remediation),
            fatal=error.fatal,
        )


__all__ = [
    "ConfigurationError",
    "DegradableError",
    "EngineError",
    "EngineErrorInfo",
    "ErrorCode",
    "FormatVersionError",
    "SchemaValidationError",
]
