"""领域模型库端点（``domain-models/*``；FR-M1-13 / FR-M2-23）。

为什么另起一个路由而不是塞进 ``profiles.py``
--------------------------------------------
档案与领域模型是两个不同读者要的东西：档案是"这份文档怎么写"，领域模型是"被文档化的
对象长什么样"。更重要的是**引用方向**——档案的 ``for_each`` 引用领域类型，
反过来不成立。两者放在同一个 ``tags`` 下，OpenAPI 里就会出现"补类型"和"改档案"
混在一张表里，而它们的补救动作完全不同（前者补库、后者改文件）。

``GET /domain-models/{kind}`` 与 ``GET /domain-models`` 的形状差别只在"一条还是一批"：
两者都返回 :class:`RegisteredDomainModelOut`，界面因此不必为详情页写第二套渲染。

**同步 ``def`` 而不是 ``async def``**：成本全在仓储读写，用例层是同步的
（与 ``profiles.py`` / ``templates.py`` 同一口径）。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from ..services import Services
from .deps import services_of
from .schemas_assets import (
    DomainModelDeprecateRequest,
    DomainModelLibraryResponse,
    DomainModelRegisterRequest,
    DomainModelRegistrationOut,
    RegisteredDomainModelOut,
)

__all__ = ["router"]

router = APIRouter(prefix="/api/v1", tags=["domain-models"])

ServicesDep = Annotated[Services, Depends(services_of)]


@router.get("/domain-models", response_model=DomainModelLibraryResponse, summary="领域模型列表")
def list_domain_models(
    services: ServicesDep,
    include_deprecated: Annotated[bool, Query(description="是否含已废弃类型")] = True,
) -> DomainModelLibraryResponse:
    """``active_count`` 是"**参与档案登记校验**的有几个"，不是总条数——见响应模型说明。"""
    items = [
        RegisteredDomainModelOut.of(item)
        for item in services.list_domain_models(include_deprecated=include_deprecated)
    ]
    return DomainModelLibraryResponse(
        items=items,
        count=len(items),
        active_count=len(services.domain_declarations()),
    )


@router.post(
    "/domain-models",
    response_model=DomainModelRegistrationOut,
    status_code=201,
    summary="登记领域模型声明",
)
def register_domain_model(
    body: DomainModelRegisterRequest, services: ServicesDep
) -> DomainModelRegistrationOut:
    """登记一份声明。同 ``kind`` 换内容即冲突（``E_VERSION_CONFLICT``），
    要覆盖得显式给 ``on_conflict=overwrite``——与模板登记、档案登记同一口径。

    ``kind`` 先校验再用：它同时是 URL 路径片段与 ``for_each`` 的引用词，
    含 ``:`` / ``/`` / ``\\`` 时直接 ``E_DOMAIN_MODEL_INVALID``（不做静默规整）。
    """
    return DomainModelRegistrationOut.of(
        services.register_domain_model(
            model=body.model, author=body.author, on_conflict=body.on_conflict
        )
    )


@router.get(
    "/domain-models/{kind}",
    response_model=RegisteredDomainModelOut,
    summary="领域模型详情（含声明本体）",
)
def domain_model_detail(kind: str, services: ServicesDep) -> RegisteredDomainModelOut:
    return RegisteredDomainModelOut.of(services.get_domain_model(kind))


@router.post(
    "/domain-models/{kind}/deprecate",
    response_model=RegisteredDomainModelOut,
    summary="废弃某类型（需理由）",
)
def deprecate_domain_model(
    kind: str,
    body: DomainModelDeprecateRequest,
    services: ServicesDep,
) -> RegisteredDomainModelOut:
    """**只标不删**：类型是已登记档案的引用键与历史产物的追溯前缀。

    废弃的后果是它**不再进注册表**（``Services.domain_declarations`` 只取未废弃的），
    故此后引用它的档案登记会被 ``E_DOMAIN_KIND_UNKNOWN`` 拦住。重复废弃是幂等的。
    """
    return RegisteredDomainModelOut.of(
        services.deprecate_domain_model(kind, reason=body.reason, operator=body.operator)
    )
