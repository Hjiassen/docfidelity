"""V2 材料、可编辑文档与可恢复生成运行。"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, File, Header, Query, Response, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field

from docfidelity.contracts.documents import DocumentDraft, OutlineSection, SectionDraft
from docfidelity.contracts.generation import GenerationRun, RunEvent
from docfidelity.contracts.materials import Identifier, SourceMaterial, Text

from .deps import AppState, state_of
from .uploads import read_bounded

router = APIRouter(prefix="/api/v2", tags=["documents"])
StateDep = Annotated[AppState, Depends(state_of)]
MatchHeader = Annotated[str | None, Header(alias="If-Match")]
KeyHeader = Annotated[str | None, Header(alias="Idempotency-Key", min_length=1, max_length=128)]


class MaterialRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=512)
    text: str = Field(min_length=1, max_length=4_000_000)
    markdown: bool = False


class DocumentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=512)
    template_id: Identifier
    template_version: int = Field(ge=1)
    material_ids: list[Identifier] = Field(min_length=1, max_length=100)
    mode: Literal["format_only", "organize"]
    instructions: str = Field(default="", max_length=20_000)
    variables: dict[str, Text] = Field(default_factory=dict, max_length=100)


class DocumentPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, min_length=1, max_length=512)
    mode: Literal["format_only", "organize"] | None = None
    instructions: str | None = Field(default=None, max_length=20_000)
    outline: list[OutlineSection] | None = Field(default=None, max_length=500)
    outline_confirmed: bool | None = None
    sections: list[SectionDraft] | None = Field(default=None, max_length=500)
    variables: dict[str, Text] | None = Field(default=None, max_length=100)


class RunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    section_id: Identifier | None = None


def _etag(response: Response, document: DocumentDraft) -> DocumentDraft:
    response.headers["ETag"] = f'"{document.revision}"'
    return document


@router.post("/materials", response_model=SourceMaterial, status_code=201)
def create_material(body: MaterialRequest, state: StateDep) -> SourceMaterial:
    return state.materials.accept(body.name, body.text.encode("utf-8"), markdown=body.markdown)


@router.post("/materials/upload", response_model=SourceMaterial, status_code=201)
def upload_material(file: Annotated[UploadFile, File()], state: StateDep) -> SourceMaterial:
    return state.materials.accept(
        file.filename or "",
        read_bounded(file.file, min(state.settings.max_upload_bytes, 32 * 1024 * 1024)),
    )


@router.get("/materials/{material_id}", response_model=SourceMaterial)
def get_material(material_id: str, state: StateDep) -> SourceMaterial:
    return state.materials.get(material_id)


@router.post("/documents", response_model=DocumentDraft, status_code=201)
def create_document(body: DocumentCreate, state: StateDep, response: Response) -> DocumentDraft:
    return _etag(response, state.documents.create(body.model_dump()))


@router.get("/documents", response_model=list[DocumentDraft])
def list_documents(state: StateDep) -> list[DocumentDraft]:
    return state.documents.list()


@router.get("/documents/{document_id}", response_model=DocumentDraft)
def get_document(document_id: str, state: StateDep, response: Response) -> DocumentDraft:
    return _etag(response, state.documents.get(document_id))


@router.get("/documents/{document_id}/revisions/{revision}", response_model=DocumentDraft)
def get_revision(
    document_id: str, revision: int, state: StateDep, response: Response
) -> DocumentDraft:
    return _etag(response, state.documents.get(document_id, revision))


@router.patch("/documents/{document_id}", response_model=DocumentDraft)
def patch_document(
    document_id: str,
    body: DocumentPatch,
    state: StateDep,
    response: Response,
    if_match: MatchHeader = None,
) -> DocumentDraft:
    return _etag(
        response, state.documents.patch(document_id, body.model_dump(exclude_unset=True), if_match)
    )


@router.post("/documents/{document_id}/outline-runs", response_model=GenerationRun, status_code=202)
def outline_run(
    document_id: str,
    body: RunRequest,
    state: StateDep,
    if_match: MatchHeader = None,
    idempotency_key: KeyHeader = None,
) -> GenerationRun:
    return state.document_runs.submit(
        document_id, "outline", if_match, idempotency_key, body.section_id
    )


@router.post("/documents/{document_id}/content-runs", response_model=GenerationRun, status_code=202)
def content_run(
    document_id: str,
    body: RunRequest,
    state: StateDep,
    if_match: MatchHeader = None,
    idempotency_key: KeyHeader = None,
) -> GenerationRun:
    return state.document_runs.submit(
        document_id, "content", if_match, idempotency_key, body.section_id
    )


@router.post("/documents/{document_id}/render-runs", response_model=GenerationRun, status_code=202)
def render_run(
    document_id: str,
    body: RunRequest,
    state: StateDep,
    if_match: MatchHeader = None,
    idempotency_key: KeyHeader = None,
) -> GenerationRun:
    return state.document_runs.submit(
        document_id, "render", if_match, idempotency_key, body.section_id
    )


@router.get("/runs/{run_id}", response_model=GenerationRun)
def get_run(run_id: str, state: StateDep) -> GenerationRun:
    return state.document_runs.get(run_id)


@router.get("/runs/{run_id}/events", response_model=list[RunEvent])
def get_events(
    run_id: str, state: StateDep, cursor: Annotated[int, Query(ge=0)] = 0
) -> list[RunEvent]:
    return state.document_runs.events(run_id, cursor)


@router.post("/runs/{run_id}/cancel", response_model=GenerationRun)
def cancel_run(run_id: str, state: StateDep) -> GenerationRun:
    return state.document_runs.cancel(run_id)


@router.get("/runs/{run_id}/artifacts", response_model=list[dict[str, Any]])
def get_artifacts(run_id: str, state: StateDep) -> list[dict[str, Any]]:
    return state.document_runs.artifacts(run_id)


@router.get("/runs/{run_id}/artifacts/{artifact_id}", response_class=FileResponse)
def download_artifact(run_id: str, artifact_id: str, state: StateDep) -> FileResponse:
    path, artifact = state.document_runs.download(run_id, artifact_id)
    return FileResponse(path, filename=artifact["name"], media_type=artifact["media_type"])
