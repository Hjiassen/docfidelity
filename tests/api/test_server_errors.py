"""错误码 → HTTP 状态映射与统一响应包（DD-13.3 / DD-13.4）。

本文件的**核心断言是一条完备性检查**：``ErrorCode`` 里声明的每一个码，都必须在
``ERROR_STATUS`` 里有映射。

为什么它值得单独一个用例：``status_for`` 在**错误处理路径**上被调用，它自己绝不能抛。
于是"未登记的码"不会报错，只会静默降级成 500 —— 一条本可给出可操作动作的失败，
变成了"服务器内部错误"，而且没有任何迹象表明映射缺了一行。运行期兜底 + 编译期完备，
两条都要有，这条用例就是后者。
"""

from __future__ import annotations

import pytest

from docfidelity.contracts.errors import EngineError, ErrorCode
from server.errors import (
    DEFAULT_STATUS,
    ERROR_STATUS,
    ApiFailure,
    envelope_of,
    new_trace_id,
    status_for,
)

pytestmark = pytest.mark.api

#: HTTP 状态里本服务可能用到的取值集合。**刻意是闭集**：出现 200 或 302 意味着
#: 某个错误码被映射成了成功响应——那比映射缺失更糟，因为客户端不会走错误分支。
ALLOWED_STATUSES = frozenset({400, 404, 409, 410, 413, 415, 422, 500, 503})


def _declared_codes() -> dict[str, str]:
    """``ErrorCode`` 上声明的全部码（``Final`` 注解的字符串常量）。"""
    return {
        name: value
        for name, value in vars(ErrorCode).items()
        if not name.startswith("_") and isinstance(value, str)
    }


def test_every_declared_code_has_a_status_mapping() -> None:
    """完备性：声明了但没映射的码会静默变 500（见模块文档）。"""
    declared = _declared_codes()
    assert declared, "没有读到任何错误码常量；ErrorCode 的形状可能变了"
    missing = sorted(set(declared.values()) - set(ERROR_STATUS))
    assert not missing, f"这些错误码没有 HTTP 状态映射，会静默降级成 500：{missing}"


def test_no_mapping_points_at_an_undeclared_code() -> None:
    """反向：映射表里不得出现已废弃 / 拼错的码（否则它永远是死条目）。"""
    declared = set(_declared_codes().values())
    orphans = sorted(set(ERROR_STATUS) - declared)
    assert not orphans, f"这些映射指向了未声明的错误码：{orphans}"


def test_all_statuses_are_from_the_closed_set() -> None:
    unexpected = sorted(
        {code: status for code, status in ERROR_STATUS.items() if status not in ALLOWED_STATUSES}
    )
    assert not unexpected, f"出现了预期外的状态码（尤其不能是 2xx）：{unexpected}"


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        (ErrorCode.UPLOAD_TYPE, 415),
        (ErrorCode.UPLOAD_SIZE, 413),
        (ErrorCode.UPLOAD_TYPE, 415),
        (ErrorCode.NOT_FOUND, 404),
        (ErrorCode.ARTIFACT_EXPIRED, 410),
        (ErrorCode.PATH_ESCAPE, 400),
        (ErrorCode.CONFIRM_REQUIRED, 400),
        (ErrorCode.SCHEMA_INVALID, 422),
        (ErrorCode.PRECHECK_FAILED, 422),
        (ErrorCode.STATE_INVALID, 409),
        (ErrorCode.NOT_RESUMABLE, 409),
        (ErrorCode.PROJECT_NAME_TAKEN, 409),
        (ErrorCode.MODEL_UNAVAILABLE, 503),
        (ErrorCode.INTERNAL, 500),
    ],
)
def test_documented_status_values(code: str, expected: int) -> None:
    """DD-13.3 里逐条写过取值的那些码，取值不得漂移。"""
    assert status_for(code) == expected


def test_unknown_code_falls_back_instead_of_raising() -> None:
    """未登记的码**不抛**：错误处理路径自己再抛，就把有诊断价值的失败换成无信息的 500。"""
    assert status_for("E_完全没听说过的码") == DEFAULT_STATUS == 500


def test_trace_id_is_prefixed_and_unique() -> None:
    ids = {new_trace_id() for _ in range(200)}
    assert all(item.startswith("tr_") for item in ids)
    assert all(len(item) == len("tr_") + 12 for item in ids)
    assert len(ids) == 200, "trace id 出现重复，跨请求就不再能串起日志"


def test_envelope_passes_remediation_through_untouched() -> None:
    """``remediation`` 是界面渲按钮的依据，服务端不得裁剪或改写（DD-13.3）。"""
    error = EngineError(
        ErrorCode.PRECHECK_FAILED,
        "缺少模板包引用",
        where={"doc_key": "detail"},
        remediation=["选择模板包", "或先入库"],
    )
    envelope = envelope_of(error, "tr_test")
    assert envelope.code == ErrorCode.PRECHECK_FAILED
    assert envelope.message == "缺少模板包引用"
    assert envelope.where == {"doc_key": "detail"}
    assert envelope.remediation == ["选择模板包", "或先入库"]
    assert envelope.trace_id == "tr_test"


def test_envelope_where_is_a_copy_not_the_same_dict() -> None:
    """响应包与内核异常**不共享** ``where``：共享意味着改响应会改到原始异常。"""
    where = {"doc_key": "detail"}
    error = EngineError(ErrorCode.PRECHECK_FAILED, "缺少引用", where=where)
    envelope = envelope_of(error, "tr_test")
    envelope.where["doc_key"] = "被改了"
    assert where == {"doc_key": "detail"}


def test_envelope_is_json_serialisable_without_secrets() -> None:
    """响应包必须能直接序列化（异常对象、路径对象混进去就会在响应阶段炸）。"""
    error = EngineError(
        ErrorCode.INTERNAL, "内部错误", where={"path": "/tmp/x"}, remediation=["重试"]
    )
    payload = envelope_of(error, "tr_test").model_dump(mode="json")
    assert set(payload) == {"code", "message", "where", "remediation", "trace_id"}
    assert isinstance(payload["remediation"], list)


def test_api_failure_derives_status_from_its_code() -> None:
    """``ApiFailure`` 不自己挑状态码——否则同一码在两处有两个状态。"""
    failure = ApiFailure(ErrorCode.NOT_FOUND, "项目不存在", where={"project_id": "p_1"})
    assert failure.code == ErrorCode.NOT_FOUND
    assert failure.status_code == 404
    assert isinstance(failure.error, EngineError)
    assert failure.envelope("tr_x").code == ErrorCode.NOT_FOUND


def test_api_failure_defaults_where_and_remediation_to_empty() -> None:
    failure = ApiFailure(ErrorCode.CONFIG_INVALID, "没有要改的项")
    assert failure.error.where == {}
    assert failure.error.remediation == []


def test_mapping_covers_more_than_one_status_class() -> None:
    """防止"整张表被简化成一个 400"这种退化：至少覆盖 4xx 的多个族与 5xx。"""
    statuses = set(ERROR_STATUS.values())
    assert len({item // 100 for item in statuses}) == 2, "状态码只剩一个类别了"
    assert {400, 404, 409, 410, 413, 415, 422} <= statuses
    assert {500, 503} <= statuses
