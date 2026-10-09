"""系统端点：设置、数据边界、模型连通性、保留清理（DD-13.7 / DD-13.2 末段）。

这个模块是"数据不出内网"在界面上的**唯一落点**（DD-13.7：「首屏可见」）。
所以两条纪律贯穿全文：

1. **响应里永不出现密钥**——不是"记得别放"，而是这里的响应模型就**没有**密钥字段
   （``GET /settings`` 由 :meth:`Services.settings_view` 显式挑字段返回）。
2. **``PUT /settings`` 不接受密钥**：密钥只从环境变量读（``DOCFIDELITY_MODEL_API_KEY``）。
   允许接口改密钥，就等于允许一次误操作把内网出口换成外部地址，而且不留痕。
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Annotated

from fastapi import APIRouter, Depends

from docfidelity.contracts.errors import ErrorCode

from ..errors import ApiFailure
from ..retention import RetentionPlan
from ..services import MUTABLE_SETTINGS_FIELDS, Services
from ..staging import RECLAIMABLE_NOTE
from .deps import services_of
from .schemas import (
    CleanupPlanOut,
    CleanupRequest,
    ExpiredResponse,
    ModelCapabilities,
    ModelHealth,
    RetainedObjectOut,
    SettingsPatch,
    SettingsView,
    StagingObjectOut,
    StagingPurgeOut,
    StagingPurgeRequest,
    StagingResponse,
)

__all__ = ["router"]

router = APIRouter(prefix="/api/v1", tags=["system"])

ServicesDep = Annotated[Services, Depends(services_of)]


@router.get("/settings", response_model=SettingsView, summary="读设置（含数据边界；永不返回密钥）")
def get_settings(services: ServicesDep) -> SettingsView:
    return SettingsView.model_validate(services.settings_view())


@router.put("/settings", response_model=SettingsView, summary="写设置（仅允许非密钥字段）")
def put_settings(body: SettingsPatch, services: ServicesDep) -> SettingsView:
    if not body.patch:
        # 空 patch 是请求形状问题（不是内容问题），用 ApiFailure 表示"只在 HTTP 层成立"。
        raise ApiFailure(
            ErrorCode.CONFIG_INVALID,
            "没有要修改的设置项",
            where={"mutable_fields": sorted(MUTABLE_SETTINGS_FIELDS)},
            remediation=[f"在 patch 里给出要改的字段，可选：{sorted(MUTABLE_SETTINGS_FIELDS)}"],
        )
    services.update_settings(body.patch)
    return SettingsView.model_validate(services.settings_view())


@router.get("/settings/model/health", response_model=ModelHealth, summary="模型连通性（只读探测）")
def model_health(services: ServicesDep) -> ModelHealth:
    return ModelHealth.model_validate(services.probe_model())


@router.get(
    "/settings/model/capabilities",
    response_model=ModelCapabilities,
    summary="模型能力现状：受限解码探测 + 基线评测（DD-12.4 / NFR-23）",
)
def model_capabilities(services: ServicesDep) -> ModelCapabilities:
    """回答"接上模型之后会怎么跑"，与 ``/settings/model/health`` 的"连得上吗"互补。

    DD-12.4 末段与风险清单第 5 条要求"受限解码探测结果对界面可见"：探测本身在
    ``pipeline.probe_grammar``（内核侧知道哪些后端算数），这里只把它与基线评测的
    加载状态一起下发。**服务端不复制这两种探测的实现**——复制一份的后果是
    界面显示的降级路径与实际执行的不一致。
    """
    return ModelCapabilities.model_validate(services.model_capabilities())


@router.get("/maintenance/expired", response_model=ExpiredResponse, summary="过期对象候选列表")
def list_expired(services: ServicesDep) -> ExpiredResponse:
    """``POST /maintenance/cleanup`` 要求"列出对象"，这一步就是那个列表的来源。"""
    items = [
        RetainedObjectOut(
            object_id=item.object_id,
            kind=item.kind,
            path=item.path,
            size_bytes=item.size_bytes,
            created_at=item.created_at,
        )
        for item in services.expired_now()
    ]
    return ExpiredResponse(expired=items, count=len(items))


@router.post(
    "/maintenance/cleanup",
    response_model=CleanupPlanOut,
    summary="一键清理（必须在请求体里逐个列出对象）",
)
def cleanup(body: CleanupRequest, services: ServicesDep) -> CleanupPlanOut:
    plan: RetentionPlan = services.cleanup(requested=body.objects)
    return CleanupPlanOut.model_validate(plan.as_dict())


# ---------------------------------------------- 导入暂存目录（独立于保留策略）
@router.get(
    "/maintenance/staging",
    response_model=StagingResponse,
    summary="模板导入暂存目录清单（含不可回收项及其原因）",
)
def list_staging(services: ServicesDep) -> StagingResponse:
    """``POST /maintenance/staging/purge`` 的候选列表。

    **判据不是"过期了没有"，而是"登记了没有"**（见 ``server.staging``）：暂存里带着
    "稍后重试解析"的承诺，未登记就删等于让那条承诺失效。所以这个端点拿不到
    ``RetentionPolicy``，它只看导入记录里有没有登记回执。
    """
    items = [StagingObjectOut.model_validate(asdict(item)) for item in services.staging_imports()]
    reclaimable = [item for item in items if item.reclaimable]
    return StagingResponse(
        imports=items,
        count=len(items),
        reclaimable_count=len(reclaimable),
        total_bytes=sum(item.size_bytes for item in items),
        reclaimable_bytes=sum(item.size_bytes for item in reclaimable),
        note=RECLAIMABLE_NOTE,
    )


@router.post(
    "/maintenance/staging/purge",
    response_model=StagingPurgeOut,
    summary="回收已登记的导入暂存目录（必须在请求体里逐个列出；不动模板库）",
)
def purge_staging(body: StagingPurgeRequest, services: ServicesDep) -> StagingPurgeOut:
    reclaimed = services.purge_staging(requested=body.objects)
    items = [StagingObjectOut.model_validate(asdict(item)) for item in reclaimed]
    return StagingPurgeOut(
        reclaimed=items,
        count=len(items),
        total_bytes=sum(item.size_bytes for item in items),
    )
