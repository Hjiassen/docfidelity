"""上传端点（DD-13.5 / DD-15.1 的上传校验四档）。

DD-13.2 的原始清单里上传是挂在 ``POST /templates/imports``（multipart）上的。
本轮把**收件**与**入库**拆开：``POST /uploads`` 只负责"这份文件能不能收"
（四道闸门 + 落盘 + 登记），入库是下一轮 ``templates/imports`` 的事。
拆开的理由不是设计偏好，是**顺序**：入库要解析模板包，而在受限子进程里跑 ``ooxml``
的那一步需要工作区、需要许可、失败要能定位到"是文件坏还是包不完整"。
把它和"收件"捆在一个请求里，一次上传失败就分不清该报 ``E_UPLOAD_TYPE``
还是 ``E_TEMPLATE_UNREADABLE``——前者改文件，后者改打包方式。

**同步 ``def`` 而不是 ``async def``**：本端点的成本在磁盘 I/O（收件、写盘），
而 ``services.accept_upload`` 是同步的。写成 ``async def`` 会让这些阻塞调用占住
事件循环（与 ``projects.py`` 同一口径，理由见该模块文档）。

分块读并**边读边计量**，而不是先整份读进内存再判定大小：DD-13.5 要求上传限制
挡住"拖死 worker 的畸形文件"，而"读完了才说太大"恰恰已经把它读进来了。
"""

from __future__ import annotations

from typing import Annotated, BinaryIO, Final

from fastapi import APIRouter, Depends, File, Query, UploadFile

from docfidelity.contracts.errors import EngineError, ErrorCode

from ..services import Services
from ..uploads import UPLOAD_PURPOSES
from .deps import services_of
from .schemas import UploadOut

__all__ = ["read_bounded", "router"]

router = APIRouter(prefix="/api/v1", tags=["uploads"])

ServicesDep = Annotated[Services, Depends(services_of)]

#: 分块大小（1 MiB）。取 1 MiB 而不是 64 KiB：模板包通常在个位数 MiB，
#: 过小的分块只是把同样的字节拆成更多次系统调用，检测粒度并不会更准。
_CHUNK_BYTES: Final = 1024 * 1024


def read_bounded(stream: BinaryIO, limit: int) -> bytes:
    """读入流，**一旦超过上限立刻拒绝**（不把整份畸形文件读进内存）。

    ``POST /uploads`` 与 ``POST /templates/imports`` 共用它：两条路径都收 multipart，
    而"读完了才说太大"恰恰已经把畸形文件读进来了（DD-13.5）。公开它而不是各写一份，
    是为了让"上限判据"只有一处。
    """
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = stream.read(_CHUNK_BYTES)
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            raise EngineError(
                ErrorCode.UPLOAD_SIZE,
                f"上传文件超过上限：> {limit} 字节",
                where={"max_upload_bytes": limit},
                remediation=["压缩后再上传，或提高服务端的 max_upload_bytes"],
            )
        chunks.append(chunk)
    return b"".join(chunks)


@router.post("/uploads", response_model=UploadOut, status_code=201, summary="上传（仅收件）")
def create_upload(
    file: Annotated[UploadFile, File(description="待收件的文件")],
    services: ServicesDep,
    purpose: Annotated[str, Query(description=f"取值：{sorted(UPLOAD_PURPOSES)}")] = "template",
) -> UploadOut:
    # ``file.filename`` 可能是 ``None``（客户端没给文件名）；传空串让校验层以
    # ``E_UPLOAD_TYPE`` 明确拒绝——不要在这里另造一条"缺文件名"的错误路径，
    # 那样同一个问题会有两个错误码。
    payload = read_bounded(file.file, services.settings.max_upload_bytes)
    record = services.accept_upload(filename=file.filename or "", payload=payload, purpose=purpose)
    return UploadOut.of(record)


@router.get("/uploads", response_model=list[UploadOut], summary="上传件列表")
def list_uploads(services: ServicesDep) -> list[UploadOut]:
    return [UploadOut.of(item) for item in services.list_uploads()]
