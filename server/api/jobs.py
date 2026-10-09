"""作业端点（DD-13.2）——含 SSE 事件流与产物取件。

SSE 是唯一用 ``async def`` 的处理函数
-------------------------------------
其余端点都是同步的（服务层阻塞在文件 I/O 上，让 FastAPI 丢进线程池即可）。
SSE 不同：它是**长连接**，必须由事件循环持有，否则一个订阅就永久占掉一个线程池
名额，几个浏览器标签页就能把线程池吃光。

``Last-Event-ID`` 由客户端回传（DD-13.4 / DD-11.6）。本模块只负责解析并转交
:func:`~server.sse.parse_last_event_id`——非法值的判定口径在那边，不在这里重写。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated, Final

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from fastapi.responses import FileResponse, StreamingResponse

from docfidelity.contracts.job import Artifact, Job, JobStatus
from docfidelity.contracts.qa import Report

from ..services import RETRY_MODES, Services
from ..sse import job_stream_settled, parse_last_event_id, stream_events
from .deps import services_of
from .files import zip_response
from .schemas import JobDetail, JobListResponse, ResumeRequest, SubmitBuild
from .schemas_project import (
    ArtifactCompareOut,
    ArtifactCompareRequest,
    ProjectArtifactsOut,
)

__all__ = ["router"]

router = APIRouter(prefix="/api/v1", tags=["jobs"])

ServicesDep = Annotated[Services, Depends(services_of)]

#: SSE 轮询间隔（秒）。2 次/秒足够；不能用默认的 0.05——那是给测试用的节奏，
#: 长连接上意味着每 50ms 一次仓储查询。
_STREAM_POLL_SECONDS: Final = 0.5


@router.post("/projects/{project_id}/jobs", response_model=Job, status_code=201, summary="发起构建")
def submit_build(project_id: str, body: SubmitBuild, services: ServicesDep) -> Job:
    return services.submit_build(project_id, doc_keys=body.doc_keys, author=body.author)


@router.get("/projects/{project_id}/jobs", response_model=JobListResponse, summary="项目作业列表")
def list_project_jobs(
    project_id: str,
    services: ServicesDep,
    status: Annotated[JobStatus | None, Query()] = None,
) -> JobListResponse:
    items = services.list_jobs(project_id=project_id, status=status)
    return JobListResponse(items=items, count=len(items))


@router.get("/jobs", response_model=JobListResponse, summary="作业列表（可按状态过滤）")
def list_jobs(
    services: ServicesDep,
    project_id: Annotated[str | None, Query()] = None,
    status: Annotated[JobStatus | None, Query()] = None,
) -> JobListResponse:
    items = services.list_jobs(project_id=project_id, status=status)
    return JobListResponse(items=items, count=len(items))


@router.get("/jobs/{job_id}", response_model=JobDetail, summary="作业详情（含子作业矩阵）")
def get_job(job_id: str, services: ServicesDep) -> JobDetail:
    return JobDetail(
        job=services.get_job(job_id),
        stages=services.job_stages(job_id),
        children=services.job_children(job_id),
        progress=services.job_progress(job_id),
        resume_plan=list(services.resume_plan(job_id)),
    )


@router.post("/jobs/{job_id}/cancel", response_model=Job, summary="取消（幂等）")
def cancel_job(job_id: str, services: ServicesDep) -> Job:
    return services.cancel(job_id)


@router.post("/jobs/{job_id}/retry", response_model=Job, summary="重试本阶段 / 从头重跑")
def retry_job(
    job_id: str,
    services: ServicesDep,
    mode: Annotated[str, Query(description=f"取值：{sorted(RETRY_MODES)}")] = "stage",
) -> Job:
    return services.retry(job_id, mode=mode)


@router.post("/jobs/{job_id}/resume", response_model=Job, summary="从指定阶段续跑")
def resume_job(job_id: str, body: ResumeRequest, services: ServicesDep) -> Job:
    return services.resume(job_id, from_stage=body.from_stage)


@router.post("/jobs/{job_id}/rerun-failed", response_model=list[Job], summary="仅重跑失败子作业")
def rerun_failed(job_id: str, services: ServicesDep) -> list[Job]:
    return services.rerun_failed(job_id)


@router.get("/jobs/{job_id}/events", summary="SSE 事件流（支持 Last-Event-ID 续推）")
async def stream_job_events(
    job_id: str,
    request: Request,
    services: ServicesDep,
    last_event_id_header: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
) -> StreamingResponse:
    services.get_job(job_id)  # 不存在即 404，不要把"作业写错了"变成一个空流
    cursor = parse_last_event_id(last_event_id_header)

    def settled() -> bool:
        return job_stream_settled(services.runner.events, services.get_job(job_id))

    async def _frames() -> AsyncIterator[str]:
        async for frame in stream_events(
            log=services.runner.events,
            job_id=job_id,
            last_event_id=cursor,
            is_settled=settled,
            heartbeat_seconds=services.settings.sse_heartbeat_seconds,
            poll_interval=_STREAM_POLL_SECONDS,
        ):
            yield frame

    return StreamingResponse(
        _frames(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            # 反向代理缓冲会把 SSE 变成"等流结束后一次性吐出"（DD-13.4 的断线回放就没意义了）。
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/jobs/{job_id}/report", response_model=Report, summary="门禁报告")
def get_report(job_id: str, services: ServicesDep) -> Report:
    return services.get_report(job_id)


@router.get("/jobs/{job_id}/artifacts", response_model=list[Artifact], summary="产物列表")
def list_artifacts(job_id: str, services: ServicesDep) -> list[Artifact]:
    return services.list_artifacts(job_id)


@router.get(
    "/projects/{project_id}/artifacts",
    response_model=ProjectArtifactsOut,
    summary="项目产物（P11：按编排作业 / 子作业 / 文档分组）",
)
def project_artifacts(project_id: str, services: ServicesDep) -> ProjectArtifactsOut:
    """分组、排序、条目数与总字节都由服务端算。

    ``count`` / ``total_bytes`` 一并返回，是为了让"当前占用"那一处不出现界面侧求和
    （NFR-22）——一处求和会引出第二处，最后每个卡片都在自己算。
    """
    return ProjectArtifactsOut.of(services.project_artifacts(project_id))


@router.post(
    "/artifacts/compare",
    response_model=ArtifactCompareOut,
    summary="比对两份 docx 产物（P11 的「版本对比」）",
)
def compare_artifacts(body: ArtifactCompareRequest, services: ServicesDep) -> ArtifactCompareOut:
    """用 **POST** 而不是 ``GET /artifacts/compare?a=&b=``：这条请求要读两份文档包并做
    一次归一化比对，不是无副作用的轻量读；更重要的是它的入参是两个**对象标识**加一个
    分辨率，写成查询串会让"哪个是基准"变成位置约定的问题。

    判据（含"两份一样吗"）全在服务端：``equal`` 照抄，界面不按三个差异集合再推一遍。
    """
    return ArtifactCompareOut.of(
        services.artifact_compare(
            baseline_id=body.baseline_id,
            candidate_id=body.candidate_id,
            mode=body.mode,
        )
    )


@router.get("/jobs/{job_id}/artifacts/package", summary="整包下载（P11 的「整套下载」）")
def download_artifact_package(job_id: str, services: ServicesDep) -> Response:
    """把该作业 ``artifacts/`` 下的全部产物打成一个 zip。

    只含**产物**：工作区里的 ``inputs/`` / ``checkpoints/`` 与事件日志不进包——
    那是复现材料，走"复现包"那条路（``kind=repro_package``），两者的读者不同。
    """
    filename, payload = services.artifact_package(job_id)
    return zip_response(filename, payload)


@router.get("/artifacts/{artifact_id}/download", summary="下载产物")
def download_artifact(artifact_id: str, services: ServicesDep) -> FileResponse:
    path = services.artifact_path(artifact_id)
    return FileResponse(path, filename=path.name)
