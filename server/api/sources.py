"""事实源端点（DD-13.2 的 ``projects/{pid}/sources/*``；DD-5.1）。

路径归本项目而不是 ``sources`` 顶层
----------------------------------
来源的标识只在**项目内**需要唯一（``Fact.source`` 引用的是本项目里的那一个），
故路径带着 ``{project_id}``。这不只是命名习惯：若把 ``source_id`` 做成全局键，
每个项目都得给自己的来源名加前缀，而"同一个来源被两个项目各声明一次"会变成冲突。

``probe`` 与 ``items`` 的分工写死在两处
--------------------------------------
``probe`` = "这条来源现在通不通"（**永不抛**，失败也是一次正常回答）；
``items`` = "通的话里面是什么"（**会抛**，读不出来就是一次失败请求）。
把两者合成一个端点看起来省事，代价是界面无法区分"连不通"和"连得通但内容是空的"
——而这两种状态要给的下一步动作完全不同。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from ..services import Services
from ..sources import DEFAULT_ITEM_LIMIT
from .deps import services_of
from .schemas_assets import (
    SourceCreateRequest,
    SourceCreateResponse,
    SourceItemsOut,
    SourceListResponse,
    SourceOut,
    SourceProbeOut,
)

__all__ = ["router"]

router = APIRouter(prefix="/api/v1", tags=["sources"])

ServicesDep = Annotated[Services, Depends(services_of)]


@router.get(
    "/projects/{project_id}/sources",
    response_model=SourceListResponse,
    summary="事实源列表",
)
def list_sources(project_id: str, services: ServicesDep) -> SourceListResponse:
    items = [SourceOut.of(item) for item in services.list_sources(project_id)]
    # ``known_types`` 一并给出：界面据此渲染"可选接入器"下拉，不必再发一次请求，
    # 也不必自己维护一份枚举（注册表可由 entry point 扩充，枚举必然落后）。
    return SourceListResponse(items=items, count=len(items), known_types=services.source_types())


@router.post(
    "/projects/{project_id}/sources",
    response_model=SourceCreateResponse,
    status_code=201,
    summary="新增事实源声明",
)
def add_source(
    project_id: str, body: SourceCreateRequest, services: ServicesDep
) -> SourceCreateResponse:
    """只做**配置期**校验（接入器认不认识、``root`` 解不解得开），不读文件内容。

    ``created=False`` 表示同 id 同配置已存在（幂等，什么也没改）——界面据此显示
    "已存在，未改动"而不是"新增成功"。
    """
    record, created = services.add_source(project_id, config=body.config, author=body.author)
    return SourceCreateResponse(source=SourceOut.of(record), created=created)


@router.delete(
    "/projects/{project_id}/sources/{source_id}",
    summary="删除来源声明",
)
def remove_source(project_id: str, source_id: str, services: ServicesDep) -> dict[str, str]:
    """**不影响历史产出**：事实包与追溯边里记的是 ``source_id``（一个字符串）。
    删掉的只是"以后不再接这一份"。"""
    services.remove_source(project_id, source_id)
    return {"removed": source_id}


@router.post(
    "/projects/{project_id}/sources/{source_id}/probe",
    response_model=SourceProbeOut,
    summary="试接入（不落盘，永不失败为 5xx）",
)
def probe_source(project_id: str, source_id: str, services: ServicesDep) -> SourceProbeOut:
    """探一次并把结果落成留痕（``last_probed_at`` / ``last_probe_ok`` / ``item_count``）。

    留痕**不参与判定**：来源能不能用只由"现在探一次"回答。返回 200 而
    ``ok=False`` 是正常结果，不是错误。
    """
    return SourceProbeOut.of(services.probe(project_id, source_id))


@router.get(
    "/projects/{project_id}/sources/{source_id}/items",
    response_model=SourceItemsOut,
    summary="条目浏览器",
)
def source_items(
    project_id: str,
    source_id: str,
    services: ServicesDep,
    limit: Annotated[int, Query(ge=1, le=2000, description="最多返回多少条")] = DEFAULT_ITEM_LIMIT,
) -> SourceItemsOut:
    """真接入一次取回条目。``truncated`` 如实标出"还有更多"——悄悄少给会让使用者
    以为来源里就这么多对象。"""
    return SourceItemsOut.of(services.source_items(project_id, source_id, limit=limit))
