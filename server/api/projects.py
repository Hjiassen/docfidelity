"""项目与配置端点（DD-13.2）。

处理函数一律用 **同步** ``def``：服务层是同步的（读文件、算摘要），FastAPI 会把同步
处理函数丢进线程池——若写成 ``async def``，这些阻塞调用会占住事件循环，
把"某个项目在渲染"变成"所有接口都变慢"。唯一的例外是 SSE 端点（``jobs.py``），
它本质是异步流。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from docfidelity.contracts.config import ProjectConfig

from ..demo import create_demo_project, initialize_demo_project
from ..services import Services
from .deps import services_of
from .schemas import (
    ConfigPutResponse,
    ConfigVersionOut,
    PrecheckReportOut,
    ProjectCreate,
    ProjectOut,
    ProjectPatch,
    ValidateRequest,
)
from .schemas_project import DriftReportOut, ProjectOverviewOut

__all__ = ["router"]

router = APIRouter(prefix="/api/v1/projects", tags=["projects"])

ServicesDep = Annotated[Services, Depends(services_of)]


@router.get("", response_model=list[ProjectOut], summary="项目列表")
def list_projects(
    services: ServicesDep,
    include_archived: Annotated[bool, Query()] = False,
) -> list[ProjectOut]:
    return [
        ProjectOut.of(item) for item in services.list_projects(include_archived=include_archived)
    ]


@router.post("", response_model=ProjectOut, status_code=201, summary="新建项目")
def create_project(body: ProjectCreate, services: ServicesDep) -> ProjectOut:
    project = (
        create_demo_project(services, name=body.name)
        if body.demo
        else services.create_project(name=body.name)
    )
    return ProjectOut.of(project)


@router.get("/{project_id}", response_model=ProjectOut, summary="项目详情")
def get_project(project_id: str, services: ServicesDep) -> ProjectOut:
    return ProjectOut.of(services.get_project(project_id))


@router.patch("/{project_id}", response_model=ProjectOut, summary="改名 / 归档（幂等）")
def patch_project(project_id: str, body: ProjectPatch, services: ServicesDep) -> ProjectOut:
    services.get_project(project_id)
    if body.name is not None:
        services.rename_project(project_id, body.name)
    if body.archived is not None:
        services.set_archived(project_id, archived=body.archived)
    return ProjectOut.of(services.get_project(project_id))


@router.delete("/{project_id}", response_model=ProjectOut, summary="归档项目（不物理删除）")
def archive_project(project_id: str, services: ServicesDep) -> ProjectOut:
    # 归档而不是删除：产物、作业历史与裁决留痕都要留着（可追溯性）。
    return ProjectOut.of(services.set_archived(project_id, archived=True))


@router.post(
    "/{project_id}/demo-init", response_model=ProjectConfig, summary="初始化演示项目（幂等）"
)
def demo_init(project_id: str, services: ServicesDep) -> ProjectConfig:
    return initialize_demo_project(services, project_id)


@router.get(
    "/{project_id}/overview",
    response_model=ProjectOverviewOut,
    summary="项目总览（P02：一屏看清状态与下一步）",
)
def project_overview(project_id: str, services: ServicesDep) -> ProjectOverviewOut:
    """总览的数据来自四处，但**只在本端点暴露一个形状**。

    "最近作业是哪一个""三步引导打没打勾""待办按什么排"都是业务结论，界面照抄；
    界面若自己拉四份列表拼，每加一张卡片就会多一处口径（NFR-22）。

    ``precheck`` 一并返回：总览页要用它决定"发起构建"是否可用、以及显示"哪些检查这一轮
    没做"。它取自与 ``POST /config/validate`` **同一个** ``Services.precheck``，
    故两页永远不会给出相反的结论。
    """
    overview = services.project_overview(project_id)
    return ProjectOverviewOut.of(overview, overview.precheck)


@router.get(
    "/{project_id}/drift",
    response_model=DriftReportOut,
    summary="漂移清单（P14：哪些文档可能过期）",
)
def drift_report(project_id: str, services: ServicesDep) -> DriftReportOut:
    return DriftReportOut.of(services.drift_report(project_id))


@router.get("/{project_id}/config", response_model=ProjectConfig, summary="读配置（最新版本）")
def get_config(project_id: str, services: ServicesDep) -> ProjectConfig:
    return services.get_config(project_id)


@router.put(
    "/{project_id}/config", response_model=ConfigPutResponse, summary="写配置（生成新版本）"
)
def put_config(
    project_id: str,
    body: ProjectConfig,
    services: ServicesDep,
    author: Annotated[str | None, Query()] = None,
) -> ConfigPutResponse:
    entry = services.put_config(project_id, body, author=author)
    return ConfigPutResponse(project_id=project_id, version=entry.version, author=entry.author)


@router.get(
    "/{project_id}/config/versions",
    response_model=list[ConfigVersionOut],
    summary="配置版本留痕",
)
def list_config_versions(project_id: str, services: ServicesDep) -> list[ConfigVersionOut]:
    return [ConfigVersionOut.of(entry) for entry in services.list_config_versions(project_id)]


@router.get(
    "/{project_id}/config/versions/{version}",
    response_model=ProjectConfig,
    summary="读某一版配置（P07 的「变更留痕」）",
)
def get_config_version(project_id: str, version: int, services: ServicesDep) -> ProjectConfig:
    """读历史版本，返回的形状与"读最新"完全一致（就是 ``ProjectConfig`` 本身）。

    这样界面拿到的永远是同一份契约：以它为底编辑再保存就是**回滚**（DD-13.6 的口径：
    回滚 = 以历史版本为内容再写一版），而不是另一条专用通道。
    """
    return services.config_version(project_id, version)


@router.post(
    "/{project_id}/config/validate",
    response_model=PrecheckReportOut,
    summary="配置校验（能不能发起构建；P08 的「为什么不能发起」）",
)
def validate_config(
    project_id: str,
    services: ServicesDep,
    body: ValidateRequest | None = None,
) -> PrecheckReportOut:
    """前置检查，判据与 ``POST /projects/{pid}/jobs`` **共用** ``server.precheck``。

    分开成两个端点是因为两个消费者要的是同一份结论的**两种用法**：发起构建要据此拒绝
    （不合即 422），P08 页面要据此渲染"只差哪一项"（永远 200，结论写在 ``ok`` 里）。
    两处各写一份判据的症状很具体——界面说"可以发起"、提交却被拒，或者反过来：
    按钮被置灰而服务端其实会放行。

    ``body`` 可省（省则检查配置里全部启用文档）：这是一个**只读的诊断**接口，
    强制带请求体只会让"我就想看一眼这个项目能不能发起"变成一次两段的调用。
    """
    report = services.precheck(project_id, doc_keys=None if body is None else body.doc_keys)
    return PrecheckReportOut.of(report)
