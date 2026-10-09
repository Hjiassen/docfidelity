"""保留策略与清理计划（DD-13.2 的 ``POST /maintenance/cleanup``、DD-13.7 的产物保留）。

一条容易被做坏的纪律
--------------------
DD-13.2 给这个端点写的是"**一键清理（需列出对象）**"。把括号里那半句落到实现上，
就是本模块存在的全部理由：

* 请求**必须逐个列出**要删的对象。空列表 = "把过期的一起删掉吧"，那正是最危险的那次
  调用，必须拒（``E_CONFIRM_REQUIRED``）。
* 列出的每个对象都**必须已在过期集合内**。否则"清理"就成了绕过正常删除流程的后门——
  而绕过流程的删除，审计上看起来和清理一模一样。

于是"清理"的语义被收敛成一句话：**清理只能回收已经过期的东西**。想提前删除某个上传件，
那是另一件事（`DELETE`），不在本模块。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Final, Literal

from docfidelity.contracts.config import RetentionPolicy
from docfidelity.contracts.errors import EngineError, ErrorCode

__all__ = [
    "DEFAULT_RETENTION",
    "RetentionPlan",
    "RetainedObject",
    "RetentionKind",
    "expired_objects",
    "expires_at",
    "plan_cleanup",
]

RetentionKind = Literal["upload", "workspace", "artifact"]

#: 未配置时的默认保留策略（与 ``RetentionPolicy`` 的默认值一致）。
DEFAULT_RETENTION: Final[RetentionPolicy] = RetentionPolicy()

#: 每类保留天数取自策略的哪个字段。**类名 → 字段**的映射写在一处，
#: 否则"上传件按产物天数算"这种错配会在三个地方各写一遍、各错一遍。
_DAYS_FIELD: Final[dict[str, str]] = {
    "upload": "uploads_days",
    "workspace": "workspace_days",
    "artifact": "artifacts_days",
}


@dataclass(frozen=True)
class RetainedObject:
    """一个受保留策略管辖的对象（上传件 / 工作区目录 / 产物）。"""

    object_id: str
    kind: RetentionKind
    path: str
    size_bytes: int = 0
    created_at: datetime | None = None


@dataclass(frozen=True)
class RetentionPlan:
    """一次清理的授权集合。**按类分组**：三类过期速度不同，混在一起就说不清删了什么。"""

    uploads: tuple[RetainedObject, ...] = field(default=())
    workspace: tuple[RetainedObject, ...] = field(default=())
    artifacts: tuple[RetainedObject, ...] = field(default=())

    def objects(self) -> tuple[RetainedObject, ...]:
        return (*self.uploads, *self.workspace, *self.artifacts)

    def object_ids(self) -> tuple[str, ...]:
        return tuple(item.object_id for item in self.objects())

    @property
    def total_bytes(self) -> int:
        return sum(item.size_bytes for item in self.objects())

    def as_dict(self) -> dict[str, object]:
        """API 响应体。``total_bytes`` 单独给出，避免前端自己求和（前端不做业务计算）。"""

        def _dump(items: Sequence[RetainedObject]) -> list[dict[str, object]]:
            return [
                {
                    "object_id": item.object_id,
                    "kind": item.kind,
                    "path": item.path,
                    "size_bytes": item.size_bytes,
                    "created_at": item.created_at.isoformat() if item.created_at else None,
                }
                for item in items
            ]

        return {
            "uploads": _dump(self.uploads),
            "workspace": _dump(self.workspace),
            "artifacts": _dump(self.artifacts),
            "count": len(self.objects()),
            "total_bytes": self.total_bytes,
        }


def expires_at(obj: RetainedObject, policy: RetentionPolicy = DEFAULT_RETENTION) -> datetime | None:
    """``created_at`` + 该类保留天数。无创建时间时返回 ``None``（**不当作已过期**）。

    这一档很重要：拿不到创建时间的对象若被当作"早就过期了"，一次清理就会把
    时间戳损坏的**全部**产物删掉。保守做法是留着，让人看见。
    """
    if obj.created_at is None:
        return None
    days = getattr(policy, _DAYS_FIELD[obj.kind])
    return obj.created_at + timedelta(days=int(days))


def expired_objects(
    objects: Sequence[RetainedObject],
    *,
    policy: RetentionPolicy = DEFAULT_RETENTION,
    now: datetime,
) -> list[RetainedObject]:
    """按策略算出已过期的对象（供界面列出候选）。"""
    expired: list[RetainedObject] = []
    for obj in objects:
        moment = expires_at(obj, policy)
        if moment is not None and moment <= now:
            expired.append(obj)
    return expired


def _require_explicit_list(requested: Sequence[str]) -> None:
    if not requested:
        raise EngineError(
            ErrorCode.CONFIRM_REQUIRED,
            "清理必须逐个列出要删除的对象",
            where={"requested": list(requested)},
            remediation=[
                "先 GET /api/v1/maintenance/expired 取候选列表",
                "再把要删除的 object_id 逐个放进请求体",
            ],
        )


def plan_cleanup(
    objects: Sequence[RetainedObject],
    *,
    requested: Sequence[str],
    policy: RetentionPolicy = DEFAULT_RETENTION,
    now: datetime,
) -> RetentionPlan:
    """校验请求并产出清理计划。

    Raises
    ------
    EngineError
        ``E_CONFIRM_REQUIRED``：未列出对象，或列出的对象**尚未过期**。
        ``E_NOT_FOUND``：列出了不存在的对象。
    """
    _require_explicit_list(requested)

    known = {obj.object_id: obj for obj in objects}
    unknown = [item for item in requested if item not in known]
    if unknown:
        raise EngineError(
            ErrorCode.NOT_FOUND,
            f"清理请求引用了不存在的对象：{sorted(unknown)}",
            where={"unknown": sorted(unknown)},
            remediation=["重新拉取候选列表；对象可能已被上一轮清理回收"],
        )

    expired_ids = {obj.object_id for obj in expired_objects(objects, policy=policy, now=now)}
    # 重复的 id 去重但保持顺序：同一对象被列两次不该删两次（也不可能），
    # 但也不该静默接受——去重后再校验，语义是"按集合处理"。
    ordered = list(dict.fromkeys(requested))
    not_expired = [item for item in ordered if item not in expired_ids]
    if not_expired:
        raise EngineError(
            ErrorCode.CONFIRM_REQUIRED,
            f"清理只处理已过期对象，以下对象尚未过期：{sorted(not_expired)}",
            where={"not_expired": sorted(not_expired)},
            remediation=[
                "从候选列表中选择真正已过期的对象",
                "若需提前删除，请走该对象所属资源的删除接口（不走清理）",
            ],
        )

    buckets: dict[str, list[RetainedObject]] = {"upload": [], "workspace": [], "artifact": []}
    for object_id in ordered:
        obj = known[object_id]
        buckets[obj.kind].append(obj)
    return RetentionPlan(
        uploads=tuple(buckets["upload"]),
        workspace=tuple(buckets["workspace"]),
        artifacts=tuple(buckets["artifact"]),
    )
