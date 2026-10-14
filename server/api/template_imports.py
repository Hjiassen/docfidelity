"""V2 格式导入、不可变版本与 DOCX 下载。"""

from __future__ import annotations

import shutil
from typing import Annotated, Any

from fastapi import APIRouter, Depends, FastAPI, File, Header, Request, Response, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.background import BackgroundTask

from docfidelity.contracts.errors import EngineError, ErrorCode
from docfidelity.contracts.imports import TemplateSelection, TemplateVersion
from docfidelity.contracts.ir import ContentIR

from ..template_imports import ImportSnapshot, TemplateDetail, TemplateImports
from .deps import state_of
from .uploads import read_bounded

router = APIRouter(prefix="/api/v2", tags=["effective-templates"])


def imports_of(request: Request) -> TemplateImports:
    return state_of(request).template_imports


ServiceDep = Annotated[TemplateImports, Depends(imports_of)]
MatchHeader = Annotated[str | None, Header(alias="If-Match")]


class FinalizeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=200)


class RenderRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    content: ContentIR
    variables: dict[str, str] = Field(default_factory=dict)
    assets: dict[str, str] = Field(default_factory=dict, description="图片逻辑名到 base64 位图")


def _etag(response: Response, snapshot: ImportSnapshot) -> ImportSnapshot:
    response.headers["ETag"] = f'"{snapshot.revision}"'
    return snapshot


@router.post("/template-imports", response_model=ImportSnapshot, status_code=201)
def create_import(
    file: Annotated[UploadFile, File()], service: ServiceDep, response: Response
) -> ImportSnapshot:
    payload = read_bounded(file.file, service.settings.max_upload_bytes)
    return _etag(response, service.accept(file.filename or "", payload))


@router.get("/template-imports/{import_id}", response_model=ImportSnapshot)
def get_import(import_id: str, service: ServiceDep, response: Response) -> ImportSnapshot:
    return _etag(response, service.get(import_id))


@router.put("/template-imports/{import_id}/selection", response_model=ImportSnapshot)
def save_selection(
    import_id: str,
    selection: TemplateSelection,
    service: ServiceDep,
    response: Response,
    if_match: MatchHeader = None,
) -> ImportSnapshot:
    return _etag(response, service.save(import_id, selection, if_match))


@router.post(
    "/template-imports/{import_id}/finalize", response_model=TemplateVersion, status_code=201
)
def finalize_import(
    import_id: str, body: FinalizeRequest, service: ServiceDep, if_match: MatchHeader = None
) -> TemplateVersion:
    return service.finalize(import_id, if_match, body.name)


@router.get("/templates", response_model=list[TemplateDetail])
def list_templates(service: ServiceDep) -> list[TemplateDetail]:
    return service.list_templates()


@router.get("/templates/{template_id}", response_model=TemplateDetail)
def template(template_id: str, service: ServiceDep) -> TemplateDetail:
    return service.template(template_id)


@router.get("/templates/{template_id}/versions/{version}", response_model=TemplateVersion)
def template_version(template_id: str, version: int, service: ServiceDep) -> TemplateVersion:
    return service.version(template_id, version)


async def bounded_render_request(request: Request) -> RenderRequest:
    """读取真实请求流时限量；不依赖可伪造的 Content-Length。"""
    limit = state_of(request).settings.max_upload_bytes
    payload = bytearray()
    async for chunk in request.stream():
        payload.extend(chunk)
        if len(payload) > limit:
            raise EngineError(ErrorCode.UPLOAD_SIZE, "渲染请求总量超限")
    try:
        return RenderRequest.model_validate_json(payload)
    except ValidationError as exc:
        raise EngineError(ErrorCode.SCHEMA_INVALID, "渲染请求格式不合法") from exc


def register_schema(app: FastAPI) -> None:
    """流量限额依赖手动解析请求，显式登记标准 OpenAPI 组件供前端生成类型。"""
    original = app.openapi

    def effective_openapi() -> dict[str, Any]:
        result = original()
        schema = RenderRequest.model_json_schema(ref_template="#/components/schemas/{model}")
        components = result.setdefault("components", {}).setdefault("schemas", {})
        components.update(schema.pop("$defs", {}))
        components["RenderRequest"] = schema
        return result

    app.openapi = effective_openapi  # type: ignore[method-assign]


@router.post(
    "/templates/{template_id}/versions/{version}/render",
    response_class=FileResponse,
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {"schema": {"$ref": "#/components/schemas/RenderRequest"}}
            },
        }
    },
    responses={
        200: {
            "content": {
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document": {
                    "schema": {"type": "string", "format": "binary"}
                }
            }
        }
    },
)
def render(
    template_id: str,
    version: int,
    service: ServiceDep,
    body: Annotated[RenderRequest, Depends(bounded_render_request)],
) -> FileResponse:
    output = service.render(template_id, version, body.content, body.variables, body.assets)
    return FileResponse(
        output,
        filename=f"{template_id}-{version}.docx",
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        background=BackgroundTask(shutil.rmtree, output.parent, ignore_errors=True),
    )
