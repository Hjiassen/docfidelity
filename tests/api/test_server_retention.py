"""保留策略与清理计划（DD-13.2 的 ``POST /maintenance/cleanup``）。

本模块的全部理由可以压成一句话：**清理只能回收已经过期的东西**。
故这里有三组用例，分别对应把这句话做坏的三种方式：

1. 空列表被当成"把过期的都删了吧"（最危险的那次调用）→ 必须拒；
2. 未过期对象被列进来（清理变成绕过删除流程的后门）→ 必须拒；
3. 拿不到创建时间的对象被当作"早就过期了"（一次清理删光时间戳损坏的全部产物）
   → 必须**留着**。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from docfidelity.contracts.config import RetentionPolicy
from docfidelity.contracts.errors import EngineError, ErrorCode
from server.retention import (
    DEFAULT_RETENTION,
    RetainedObject,
    RetentionPlan,
    expired_objects,
    expires_at,
    plan_cleanup,
)

pytestmark = pytest.mark.api

NOW = datetime(2026, 9, 22, 9, 0, 0, tzinfo=timezone.utc)
POLICY = RetentionPolicy(uploads_days=7, workspace_days=30, artifacts_days=180)


def obj(
    object_id: str,
    *,
    kind: str = "upload",
    age_days: float = 0.0,
    size: int = 0,
) -> RetainedObject:
    return RetainedObject(
        object_id=object_id,
        kind=kind,  # type: ignore[arg-type]
        path=f"var/{object_id}",
        size_bytes=size,
        created_at=None if age_days is None else NOW - timedelta(days=age_days),
    )


def expect_error(code: str, **kwargs: object) -> EngineError:
    with pytest.raises(EngineError) as caught:
        plan_cleanup(**kwargs)  # type: ignore[arg-type]
    assert caught.value.code == code, caught.value.message
    return caught.value


# ------------------------------------------------------------------ 到期时刻
def test_expires_at_uses_the_days_of_its_own_kind() -> None:
    """三类过期速度不同；把三类混成一个天数会让其中一类总是过长或过短。"""
    assert expires_at(obj("up", kind="upload"), POLICY) == NOW + timedelta(days=7)
    assert expires_at(obj("ws", kind="workspace"), POLICY) == NOW + timedelta(days=30)
    assert expires_at(obj("ar", kind="artifact"), POLICY) == NOW + timedelta(days=180)


def test_expires_at_is_none_without_a_creation_time() -> None:
    """拿不到创建时间 → 不算过期（**不当作"早就过期了"**）。"""
    assert expires_at(obj("x", age_days=None), POLICY) is None


def test_default_policy_is_the_contract_default() -> None:
    assert RetentionPolicy() == DEFAULT_RETENTION


# ------------------------------------------------------------------ 过期集合
def test_expired_objects_respects_the_per_kind_window() -> None:
    items = [
        obj("upload-old", kind="upload", age_days=8),
        obj("upload-fresh", kind="upload", age_days=6),
        obj("artifact-old", kind="artifact", age_days=181),
        obj("artifact-fresh", kind="artifact", age_days=179),
    ]
    expired = {item.object_id for item in expired_objects(items, policy=POLICY, now=NOW)}
    assert expired == {"upload-old", "artifact-old"}


def test_boundary_is_inclusive() -> None:
    """正好到期算过期（``<=``）：否则"第 7 天"永远清不掉，规则看起来永远差一天。"""
    exactly = obj("edge", kind="upload", age_days=7)
    assert [item.object_id for item in expired_objects([exactly], policy=POLICY, now=NOW)] == [
        "edge"
    ]


def test_objects_without_created_at_are_never_expired() -> None:
    undated = obj("undated", age_days=None)
    assert expired_objects([undated], policy=POLICY, now=NOW) == []


# ------------------------------------------------------------------ 计划校验
def test_empty_request_is_rejected() -> None:
    """空列表 = "把过期的一起删掉吧"，那正是最危险的那次调用。"""
    error = expect_error(
        ErrorCode.CONFIRM_REQUIRED, objects=[obj("a")], requested=[], policy=POLICY, now=NOW
    )
    assert "expired" in error.remediation[0]


def test_unknown_object_is_rejected() -> None:
    error = expect_error(
        ErrorCode.NOT_FOUND,
        objects=[obj("a")],
        requested=["ghost"],
        policy=POLICY,
        now=NOW,
    )
    assert error.where["unknown"] == ["ghost"]


def test_not_yet_expired_object_is_rejected() -> None:
    """清理不是绕过删除流程的后门：未过期的对象一律拒（即使用户显式列出）。"""
    fresh = obj("fresh", kind="upload", age_days=1)
    error = expect_error(
        ErrorCode.CONFIRM_REQUIRED,
        objects=[fresh],
        requested=["fresh"],
        policy=POLICY,
        now=NOW,
    )
    assert error.where["not_expired"] == ["fresh"]


def test_partially_valid_request_is_rejected_as_a_whole() -> None:
    """**全有或全无**：不允许"删掉能删的那一半"。半执行的清理无法向使用者解释。"""
    old = obj("old", kind="upload", age_days=9)
    fresh = obj("fresh", kind="upload", age_days=1)
    expect_error(
        ErrorCode.CONFIRM_REQUIRED,
        objects=[old, fresh],
        requested=["old", "fresh"],
        policy=POLICY,
        now=NOW,
    )


def test_unknown_is_checked_before_expiry() -> None:
    """先报"这个对象不存在"，再报"这个还没过期"——否则用户会去找一个不存在的对象调保留期。"""
    expect_error(
        ErrorCode.NOT_FOUND,
        objects=[obj("old", kind="upload", age_days=9)],
        requested=["ghost"],
        policy=POLICY,
        now=NOW,
    )


# -------------------------------------------------------------------- 计划
def test_plan_groups_by_kind_and_keeps_request_order() -> None:
    items = [
        obj("u1", kind="upload", age_days=9, size=10),
        obj("w1", kind="workspace", age_days=40, size=20),
        obj("a1", kind="artifact", age_days=200, size=30),
    ]
    # 请求顺序刻意与 kinds 的顺序（upload/workspace/artifact）交叉。
    plan = plan_cleanup(items, requested=["a1", "u1", "w1"], policy=POLICY, now=NOW)
    assert [item.object_id for item in plan.uploads] == ["u1"]
    assert [item.object_id for item in plan.workspace] == ["w1"]
    assert [item.object_id for item in plan.artifacts] == ["a1"]
    assert plan.object_ids() == ("u1", "w1", "a1")
    assert plan.total_bytes == 60


def test_duplicate_ids_are_deduplicated_not_double_counted() -> None:
    items = [obj("u1", kind="upload", age_days=9, size=10)]
    plan = plan_cleanup(items, requested=["u1", "u1"], policy=POLICY, now=NOW)
    assert plan.object_ids() == ("u1",)
    assert plan.total_bytes == 10


def test_plan_as_dict_is_the_api_shape() -> None:
    items = [obj("u1", kind="upload", age_days=9, size=10)]
    payload = plan_cleanup(items, requested=["u1"], policy=POLICY, now=NOW).as_dict()
    assert set(payload) == {"uploads", "workspace", "artifacts", "count", "total_bytes"}
    assert payload["count"] == 1
    assert payload["total_bytes"] == 10
    assert payload["uploads"][0]["object_id"] == "u1"
    # ``created_at`` 必须已转成字符串：响应体里塞一个 datetime 会在序列化时才炸。
    assert isinstance(payload["uploads"][0]["created_at"], str)


def test_plan_as_dict_handles_a_missing_created_at() -> None:
    """``created_at`` 为空的条目也要能表达。

    本路径不经 ``plan_cleanup``（无创建时间的对象永远不会过期，故进不了计划），
    所以直接构造计划来验形状——``as_dict`` 是**公开的响应形状**，不该只有一条
    路径能走到它。
    """
    plan = RetentionPlan(
        uploads=(RetainedObject(object_id="u1", kind="upload", path="var/u1", size_bytes=5),)
    )
    payload = plan.as_dict()
    assert payload["uploads"][0]["created_at"] is None
    assert payload["count"] == 1
    assert payload["total_bytes"] == 5


def test_empty_request_never_yields_an_empty_plan() -> None:
    """空计划的唯一合法来源是"没有待清理项"，绝不是"请求为空"。"""
    with pytest.raises(EngineError) as caught:
        plan_cleanup([], requested=[], policy=POLICY, now=NOW)
    assert caught.value.code == ErrorCode.CONFIRM_REQUIRED
