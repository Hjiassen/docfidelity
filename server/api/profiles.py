"""文档类型档案端点（DD-13.2 的 ``profiles/*``；FR-M2-23~27）。

为什么 ``/profiles/{pid}/export`` 声明在 ``/profiles/{pid}`` **之前**
----------------------------------------------------------------
两者都只有三段路径、都以 ``{profile_id}`` 打头，区别在第三段是字面量 ``export``
还是变量。FastAPI 按**声明顺序**匹配，故字面量必须先声明——否则
``GET /profiles/api/export`` 会被 ``/profiles/{pid}`` 抢先匹配、把 ``export``
当成一个档案标识，报出一句"找不到档案：export"这种让人一头雾水的 404。
带版本的那两条用 ``/versions/{version}`` 而不是 ``/{version}`` 也是为了这一点。

**同步 ``def`` 而不是 ``async def``**：成本在磁盘 I/O（写档案本体、读回、打 zip）
与仓储读写，用例层是同步的（与 ``templates.py`` 同一口径）。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, File, Query, Response, UploadFile

from ..profiles import DEFAULT_PROFILE_VERSION
from ..services import Services
from .deps import services_of
from .files import zip_response
from .schemas_assets import (
    ProfileDeprecateRequest,
    ProfileDetailOut,
    ProfileImportOut,
    ProfileLibraryResponse,
    ProfileRegisterRequest,
    ProfileRegistrationOut,
    RegisteredProfileOut,
)
from .uploads import read_bounded

__all__ = ["router"]

router = APIRouter(prefix="/api/v1", tags=["profiles"])

ServicesDep = Annotated[Services, Depends(services_of)]


@router.get("/profiles", response_model=ProfileLibraryResponse, summary="档案列表")
def list_profiles(
    services: ServicesDep,
    doc_type: Annotated[str | None, Query(description="只列该文档类型的档案")] = None,
    include_deprecated: Annotated[bool, Query(description="是否含已废弃版本")] = True,
) -> ProfileLibraryResponse:
    items = [
        RegisteredProfileOut.of(item)
        for item in services.list_profiles(doc_type=doc_type, include_deprecated=include_deprecated)
    ]
    return ProfileLibraryResponse(items=items, count=len(items))


@router.post(
    "/profiles",
    response_model=ProfileRegistrationOut,
    status_code=201,
    summary="登记档案版本",
)
def register_profile(body: ProfileRegisterRequest, services: ServicesDep) -> ProfileRegistrationOut:
    """登记一个档案版本。

    合并了 P05 的"导入 + 登记"两步：档案是人写的一份 JSON，没有"解析"这一步可分批，
    故不走 multipart（设计 DD-13.2 的 ``profiles/import`` 是给**档案包**（zip）用的，
    那是另一条路径；本端点收的是**已解开的**档案本体）。

    ``known_template_refs`` 与 ``domain_models`` 由这里读库后传入用例层，而不是让用例层
    自己去查："谁去读仓储"只有一处（与 ``precheck`` 的交接口径一致，硬规则 22）。

    ``domain_models`` 取 ``services.domain_declarations()``——**已废弃的类型不入册**。
    这个投影刻意留在这里而不是进用例层：本层读了两处库，一眼能看出"进校验的是哪些"。
    """
    refs = [item.ref for item in services.list_templates()]
    domain_models = services.domain_declarations()
    return ProfileRegistrationOut.of(
        services.register_profile(
            document=body.profile,
            version=body.version or DEFAULT_PROFILE_VERSION,
            author=body.author,
            on_conflict=body.on_conflict,
            known_template_refs=refs,
            domain_models=domain_models,
        )
    )


@router.post(
    "/profiles/import",
    response_model=ProfileImportOut,
    status_code=201,
    summary="导入档案包（zip，FR-M2-25 / 验收 45）",
)
def import_profile_package(
    file: Annotated[UploadFile, File(description="档案包的 zip 字节（导出端产物）")],
    services: ServicesDep,
    version: Annotated[str | None, Query(description="留空取包内自报的版本")] = None,
    on_conflict: Annotated[
        str, Query(description="同 id@version 已存在且内容不同时：abort / overwrite")
    ] = "abort",
    author: Annotated[str | None, Query(description="导入人；只落留痕")] = None,
) -> ProfileImportOut:
    """一步导入：包内的模板包先登记，档案本体随后登记（顺序不可颠倒，见用例层）。

    收的是 **multipart 文件**而不是裸 body，与 ``POST /uploads`` 同一形态——
    浏览器侧的 ``FormData`` 不用为这一个端点另写一套，而"两边各写一套上传"
    正是这类接口最常出现分歧的地方。

    包内的模板包与档案本体**共用一次调用**，故不收 ``domain_models``：领域模型是
    **本库**的状态，不是包里的东西（包只带档案与它引用的版式）。服务端从库里读好
    再交给用例层——与 ``POST /profiles`` 同一句口径（硬规则 22）。
    """
    payload = read_bounded(file.file, services.settings.max_upload_bytes)
    return ProfileImportOut.of(
        services.import_profile_package(
            payload=payload,
            version=version,
            on_conflict=on_conflict,
            author=author,
            domain_models=services.domain_declarations(),
        )
    )


@router.get(
    "/profiles/{profile_id}/export",
    summary="导出档案包（zip，FR-M2-25）",
)
def export_profile(
    profile_id: str,
    services: ServicesDep,
    version: Annotated[str | None, Query(description="留空取最新一版")] = None,
) -> Response:
    filename, payload = services.export_profile(profile_id, version)
    return zip_response(filename, payload)


@router.get(
    "/profiles/{profile_id}",
    response_model=ProfileDetailOut,
    summary="档案详情（含本体与版本列表）",
)
def profile_detail(
    profile_id: str,
    services: ServicesDep,
    version: Annotated[str | None, Query(description="留空取最新一版")] = None,
) -> ProfileDetailOut:
    entry = services.get_profile(profile_id, version)
    return ProfileDetailOut(
        entry=RegisteredProfileOut.of(entry),
        document=services.profile_document(profile_id, entry.version),
        versions=[
            RegisteredProfileOut.of(item) for item in services.list_profiles(profile_id=profile_id)
        ],
    )


@router.post(
    "/profiles/{profile_id}/versions/{version}/deprecate",
    response_model=RegisteredProfileOut,
    summary="废弃某版本（需理由）",
)
def deprecate_profile(
    profile_id: str,
    version: str,
    body: ProfileDeprecateRequest,
    services: ServicesDep,
) -> RegisteredProfileOut:
    """**只标不删**：档案版本是历史产物的引用键。重复废弃是幂等的。"""
    return RegisteredProfileOut.of(
        services.deprecate_profile(profile_id, version, reason=body.reason, operator=body.operator)
    )
