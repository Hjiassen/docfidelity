"""模板导入暂存目录的回收（``imports/<import_id>/``）。

为什么**不**并入保留策略
------------------------
``imports/`` 与 ``templates/`` 都不在保留策略里（见 ``settings.imports_root`` 与
``templates.py`` 的模块文档），理由是它们各自的"承诺"：

* ``templates/`` 存的是历史产物的**引用键**，按天数清掉会让"上个月的文档是怎么生成的"
  变成无法回答的问题；
* ``imports/`` 里带着**"稍后重试解析"的承诺**——一份没解析完的导入，它存在的意义就是
  "原件还在，随时能再试一次"。

所以本模块的判据不是"过期了没有"，而是**承诺兑现了没有**：

    一份导入的暂存目录可以回收，当且仅当它**已经登记进模板库**。

登记意味着原件与解析出的包都已经进了 ``templates/<tid>/<ver>/``，暂存目录此后只是一份
副本；而**未登记**的暂存目录一旦删掉，"再试一次"就真的没得试了。这条判据与
:func:`~server.retention.expires_at` 的"拿不到创建时间就不算过期"是同一种保守：
**判不出来时留下，让人看见。**

于是"清理只能回收已经过期的东西"那条纪律在这里换一个说法：

* 请求**必须逐个列出**要回收的暂存目录（空列表 = "把能删的都删了吧"，正是最危险的那次
  调用，拒 ``E_CONFIRM_REQUIRED``）；
* 列出的每一项都**必须已登记**，否则同样拒——先拒绝，再由界面把原因显示出来。

回收**不动**模板库
------------------
删掉 ``imports/<id>/`` 只回收暂存：``templates/<tid>/<ver>/`` 与仓储里的登记行都不动，
故已登记的模板照常可用、照常被历史产物引用。这条写在测试里（见 ``test_server_staging``），
因为"清了个暂存，模板却没退库"正是使用者会担心的事。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from docfidelity.contracts.errors import EngineError, ErrorCode

__all__ = [
    "NOT_REGISTERED_REASON",
    "RECLAIMABLE_NOTE",
    "StagingObject",
    "UNREADABLE_REASON",
    "plan_staging_purge",
    "reclaimability",
]

#: 一句话说清本模块的判据，**由服务端给**（界面不得自己归纳一条规则，硬规则 15）。
RECLAIMABLE_NOTE: Final = (
    "模板导入的暂存目录（imports/<导入标识>/）在**已登记进模板库之后**才可以回收："
    "登记意味着原件与解析出的包都已落到模板库，暂存只是一份副本；未登记的暂存仍带着"
    "「稍后重试解析」的承诺，删掉就真的没得试了。回收不动模板库与登记记录。"
)

#: 未登记的暂存为什么不能回收——逐条理由的原文也由服务端给。
NOT_REGISTERED_REASON: Final = "这份导入尚未登记进模板库，暂存里带着「稍后重试解析」的承诺"
UNREADABLE_REASON: Final = "读不到导入记录，无法确认这份导入是否已登记"


@dataclass(frozen=True)
class StagingObject:
    """一个模板导入暂存目录（``imports/<import_id>/``）。

    ``reclaimable`` 是**服务端的判据**，``blocked_reason`` 是它不能回收时的措辞；
    两者由 :func:`reclaimability` **一处**同时算出，不允许各写一遍——否则会出现
    "按钮是灰的、但界面说不出为什么"这种状态（硬规则 15 要堵的正是它）。
    """

    import_id: str
    path: str
    size_bytes: int = 0
    created_at: datetime | None = None
    #: 导入记录能否读到。读不到时既列不出版本，也无法确认登记状态。
    readable: bool = True
    registered: bool = False
    #: 已登记时的模板包引用 ``"<template_id>@<version>"``。
    template_ref: str | None = None
    reclaimable: bool = False
    blocked_reason: str | None = None


def reclaimability(*, readable: bool, registered: bool) -> tuple[bool, str | None]:
    """由"读得到吗 / 登记了吗"推出（可回收，不能回收的原文原因）。**唯一一处**。

    公开而不是私有：扫目录的那一侧（``server.templates``）要用它来填
    :attr:`StagingObject.reclaimable` 与 :attr:`StagingObject.blocked_reason`
    这一对，而"判据与措辞必须同时产出"这条约束只有靠**同一处实现**才守得住。
    """
    if not readable:
        return False, UNREADABLE_REASON
    if not registered:
        return False, NOT_REGISTERED_REASON
    return True, None


def plan_staging_purge(
    objects: Sequence[StagingObject],
    *,
    requested: Sequence[str],
) -> tuple[StagingObject, ...]:
    """校验回收请求并返回**按请求顺序**排好的待回收项。

    Raises
    ------
    EngineError
        ``E_CONFIRM_REQUIRED``：未列出对象，或列出的对象**尚不可回收**（未登记 / 记录
        读不到）。用同一个码是因为使用者看到的是同一个动作："这次请求被拦下了，理由在
        下面这条消息里"。
        ``E_NOT_FOUND``：列出了不存在的暂存目录。
    """
    if not requested:
        raise EngineError(
            ErrorCode.CONFIRM_REQUIRED,
            "回收暂存目录必须逐个列出要删除的导入标识",
            where={"requested": list(requested)},
            remediation=[
                "先 GET /api/v1/maintenance/staging 取暂存目录清单",
                "再把要回收的 import_id 逐个放进请求体",
                RECLAIMABLE_NOTE,
            ],
        )

    known = {item.import_id: item for item in objects}
    unknown = [item for item in requested if item not in known]
    if unknown:
        raise EngineError(
            ErrorCode.NOT_FOUND,
            f"回收请求引用了不存在的暂存目录：{sorted(unknown)}",
            where={"unknown": sorted(unknown)},
            remediation=["重新拉取清单；该目录可能已被上一轮回收"],
        )

    # 去重但保持顺序：同一项被列两次不该删两次（也不可能），语义是"按集合处理"。
    ordered = list(dict.fromkeys(requested))
    blocked = [item for item in ordered if not known[item].reclaimable]
    if blocked:
        raise EngineError(
            ErrorCode.CONFIRM_REQUIRED,
            "只有**已登记**的导入暂存目录可以回收，以下还不满足："
            + "、".join(f"{item}（{known[item].blocked_reason}）" for item in sorted(blocked)),
            where={
                "not_reclaimable": sorted(blocked),
                "reasons": {item: known[item].blocked_reason for item in sorted(blocked)},
            },
            remediation=[
                "先在 P05 上把该导入走完登记（POST /api/v1/templates/imports/{id}/register）",
                "或确认它是否还需要「稍后重试解析」——未登记的暂存不回收",
            ],
        )
    return tuple(known[item] for item in ordered)
