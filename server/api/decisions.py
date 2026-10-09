"""裁决端点（DD-13.2 的 ``findings/{fid}/decisions`` 与 ``inbox``）。

两条路径为什么同属一个模块
--------------------------
它们描述的是**同一件事的写入面与读取面**：``/inbox`` 列出"还有谁要裁决"，
``/findings/{fid}/decisions`` 把一条裁决写回去，写完立刻在 ``/inbox`` 里消失。
分到两个文件里时，"待办判据"就有了两个落点（列表一处、写回一处），而它们的
分歧表现是最难查的一种——"点了按钮，徽标数字不变"。

路径参数为什么仍叫 ``fid``
--------------------------
DD-13.2 把这条路径写作 ``/findings/{fid}/decisions``，而裁决对象其实是**待裁决项**
（门禁判定 ``Finding.finding_id`` 或决策项 ``Decision.decision_id``，见
:mod:`server.adjudication`）。**路径形状属于 Stable 契约**（DD-13.2 末段），
故保留原样不改名；语义在文档里说清即可，改路径反而是一次破坏性变更。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from ..adjudication import AdjudicationResult
from ..services import Services
from .deps import services_of
from .schemas import (
    AdjudicationRequest,
    AdjudicationResponse,
    InboxItemOut,
    InboxResponse,
)

__all__ = ["router"]

router = APIRouter(prefix="/api/v1", tags=["decisions"])

ServicesDep = Annotated[Services, Depends(services_of)]


def _response_of(result: AdjudicationResult) -> AdjudicationResponse:
    report = result.report
    return AdjudicationResponse(
        adjudication=result.adjudication,
        finding=result.finding,
        waiver=result.waiver,
        report_id=report.report_id,
        job_id=result.job.job_id,
        job_status=result.job.status,
        verdict=report.verdict,
        open_findings=len(report.open_findings()),
        remaining_subjects=len(result.remaining),
        advance=result.advance,
        note=result.note,
    )


@router.post(
    "/findings/{fid}/decisions",
    response_model=AdjudicationResponse,
    summary="提交裁决（接受 / 改配置 / 豁免）",
)
def submit_decision(
    fid: str,
    body: AdjudicationRequest,
    services: ServicesDep,
) -> AdjudicationResponse:
    """> ``fid`` = 待裁决项标识，取自 ``GET /inbox`` 的 ``subject_id``
    > 或报告里某条判定的 ``finding_id``。

    ``action=waive`` 必须带 ``reason``（否则 ``E_WAIVER_REASON_REQUIRED``）：
    只有它会改变门禁结论，也只有它需要留下"谁以什么理由放行的"（FR-M7-06 / NFR-15）。
    """
    result = services.adjudicate(
        fid, action=body.action, operator=body.operator, reason=body.reason
    )
    return _response_of(result)


@router.get("/inbox", response_model=InboxResponse, summary="裁决待办聚合（P12）")
def list_inbox(
    services: ServicesDep,
    project_id: Annotated[str | None, Query(description="只列该项目的待办")] = None,
) -> InboxResponse:
    """跨作业聚合待裁决项。

    判据是"**没有**裁决留痕"（见 :mod:`server.adjudication`）：已办结的项不再出现，
    所以这个响应天然是"还剩谁要看"，而不是"历史上出现过多少问题"。
    """
    items = [InboxItemOut.of(item) for item in services.inbox(project_id=project_id)]
    return InboxResponse(items=items, count=len(items))
