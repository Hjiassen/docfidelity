"""上传校验（DD-13.5 / 时序 15.1 的"上传校验(类型/大小/可解析/宏)"）。

四道闸门，顺序即语义
--------------------
========================  ====================================================
① 文件名安全             含分隔符/``..`` 一律拒（``E_PATH_ESCAPE``）
② 大小                   空文件与超限一律拒（``E_UPLOAD_SIZE``）
③ 类型                   后缀白名单 **且** 魔数相符（``E_UPLOAD_TYPE``）
④ 宏                     容器内含 ``vbaProject.bin`` 即拒（``E_UPLOAD_TYPE``）
========================  ====================================================

**后缀与魔数必须同时相符**：只信后缀可以被改名的任意文件骗过；只信魔数则
``.pdf`` 与 ``.zip`` 无法区分（两者都是容器）。两道一起才等价于"这确实是一个
我允许接收的东西"。

关于"可解析"这一档
------------------
DD-15.1 的四档校验里，"可解析"**不在本模块**：解析要在受限子进程里跑（防畸形文档
拖死 worker），而解析器是内核的（``ooxml``）。服务端按 DD-13.1 的硬约束**不得**
import ``ooxml``，所以本模块只做前四档中可静态完成的判定，"可解析"由流水线在
受限子进程里完成、以 ``StageOutcome`` 的形式回传（见 ``server.jobs``）。

本模块是纯函数：吃 :class:`UploadProbe`（文件名 + 大小 + 头部字节 + 成员名），
不碰磁盘、不碰请求对象——故可独立测试，也可在 worker 侧复用同一份判定。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
from io import BytesIO
from pathlib import PurePosixPath, PureWindowsPath
from typing import Final
from zipfile import BadZipFile, ZipFile

from docfidelity.contracts.errors import EngineError, ErrorCode

from .settings import ServerSettings

__all__ = [
    "MACRO_MEMBER",
    "MAGIC_BY_SUFFIX",
    "MIME_BY_SUFFIX",
    "UPLOAD_PURPOSES",
    "UploadProbe",
    "UploadReceipt",
    "probe_upload",
    "validate_upload",
    "zip_members",
]

#: 每种允许的后缀对应的魔数（可多选：zip 的空档案是 ``PK\x05\x06``）。
MAGIC_BY_SUFFIX: Final[dict[str, tuple[bytes, ...]]] = {
    ".docx": (b"PK\x03\x04",),
    ".zip": (b"PK\x03\x04", b"PK\x05\x06"),
    ".pdf": (b"%PDF-",),
}

#: 声明给界面的 MIME（仅用于展示与存储元数据，**不参与**上面的判定）。
MIME_BY_SUFFIX: Final[dict[str, str]] = {
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".zip": "application/zip",
    ".pdf": "application/pdf",
}

#: OOXML 容器内的宏工程路径。**出现即拒**：模板库里不该有可执行内容。
MACRO_MEMBER: Final = "word/vbaProject.bin"

#: 上传用途分类。**只用于留痕**，不参与任何判定——但它必须是个闭集：
#: 自由字符串会让"模板包"和"模板包 "（多个空格）在留痕里变成两类。
UPLOAD_PURPOSES: Final[frozenset[str]] = frozenset({"template", "profile", "reference"})

#: 判定魔数所需的最少字节数。
_HEAD_BYTES: Final = 8


@dataclass(frozen=True)
class UploadProbe:
    """上传件的静态事实（不含字节流本身）。

    ``digest`` 必须由**持有完整字节**的一方算好后传入，而不是在这里按 ``head`` 现算：
    只哈希头部 8 字节会让所有 ``.docx`` 得到同一个摘要（它们都以 ``PK\\x03\\x04`` 开头），
    于是"同一份文件重复入库"的判定会把两份完全不同的模板判成同一份。
    """

    filename: str
    size: int
    head: bytes
    digest: str
    members: tuple[str, ...] = field(default=())
    """容器成员名（仅 zip 类需要；非容器留空）。判定宏用。"""


@dataclass(frozen=True)
class UploadReceipt:
    """通过校验的回执。``sha256`` 用于后续"同一份文件重复入库"的判定与留痕。"""

    filename: str
    suffix: str
    size_bytes: int
    content_type: str
    digest: str


def _suffix_of(filename: str) -> str:
    return PurePosixPath(filename).suffix.lower()


def _check_filename(filename: str) -> None:
    """文件名安全。**拒绝**而不是清洗——把 ``../x.docx`` 改写成 ``x.docx``
    会掩盖一次越界尝试，而越界尝试本身是需要被看见的信号。"""
    if not filename or not filename.strip():
        raise EngineError(
            ErrorCode.UPLOAD_TYPE,
            "上传文件名为空",
            where={"filename": filename},
            remediation=["为上传的文件提供一个不含路径的文件名"],
        )
    if "\x00" in filename or any(char in filename for char in ("/", "\\")):
        raise EngineError(
            ErrorCode.PATH_ESCAPE,
            f"上传文件名含路径分隔符：{filename!r}",
            where={"filename": filename},
            remediation=["只提交文件名本身，路径由服务端决定"],
        )
    # Windows 与 POSIX 的父目录记号都要挡：部署形态可能跨平台，判定不能只认一种。
    if PurePosixPath(filename).name in {"", ".", ".."} or PureWindowsPath(filename).name in {
        "",
        ".",
        "..",
    }:
        raise EngineError(
            ErrorCode.PATH_ESCAPE,
            f"上传文件名是父目录记号：{filename!r}",
            where={"filename": filename},
            remediation=["只提交文件名本身，路径由服务端决定"],
        )


def _check_size(size: int, settings: ServerSettings) -> None:
    if size <= 0:
        raise EngineError(
            ErrorCode.UPLOAD_SIZE,
            "上传文件为空",
            where={"size_bytes": size},
            remediation=["重新选择一个非空文件"],
        )
    if size > settings.max_upload_bytes:
        raise EngineError(
            ErrorCode.UPLOAD_SIZE,
            f"上传文件超过上限：{size} > {settings.max_upload_bytes} 字节",
            where={"size_bytes": size, "max_upload_bytes": settings.max_upload_bytes},
            remediation=["压缩后再上传，或提高服务端的 max_upload_bytes"],
        )


def _check_type(probe: UploadProbe, settings: ServerSettings) -> tuple[str, str]:
    suffix = _suffix_of(probe.filename)
    if suffix not in settings.allowed_upload_suffixes or suffix not in MAGIC_BY_SUFFIX:
        raise EngineError(
            ErrorCode.UPLOAD_TYPE,
            f"不支持的文件类型：{probe.filename!r}",
            where={"suffix": suffix, "allowed": list(settings.allowed_upload_suffixes)},
            remediation=["改传 .docx 模板、.pdf 对照件或 .zip 模板包"],
        )
    accepted = MAGIC_BY_SUFFIX[suffix]
    if not any(probe.head.startswith(magic) for magic in accepted):
        raise EngineError(
            ErrorCode.UPLOAD_TYPE,
            f"文件内容与扩展名 {suffix} 不符",
            where={"suffix": suffix, "head": probe.head[:_HEAD_BYTES].hex()},
            remediation=["确认文件未被改名；重命名会同时改掉后缀与内容的对应关系"],
        )
    return suffix, MIME_BY_SUFFIX[suffix]


def _check_no_macro(probe: UploadProbe) -> None:
    if MACRO_MEMBER in probe.members:
        raise EngineError(
            ErrorCode.UPLOAD_TYPE,
            "文件包含宏工程，已拒绝",
            where={"member": MACRO_MEMBER, "filename": probe.filename},
            remediation=["另存为不含宏的 .docx 后重新上传"],
        )


def validate_upload(probe: UploadProbe, settings: ServerSettings) -> UploadReceipt:
    """按四道闸门校验一次上传，返回回执。

    Raises
    ------
    EngineError
        ``E_PATH_ESCAPE`` / ``E_UPLOAD_SIZE`` / ``E_UPLOAD_TYPE``。
    """
    _check_filename(probe.filename)
    _check_size(probe.size, settings)
    suffix, content_type = _check_type(probe, settings)
    _check_no_macro(probe)
    return UploadReceipt(
        filename=probe.filename,
        suffix=suffix,
        size_bytes=probe.size,
        content_type=content_type,
        digest=probe.digest,
    )


def probe_upload(filename: str, payload: bytes, *, members: tuple[str, ...] = ()) -> UploadProbe:
    """由内存字节构造探测结果（测试与"小文件直读"路径共用）。"""
    return UploadProbe(
        filename=filename,
        size=len(payload),
        head=payload[:_HEAD_BYTES],
        digest=sha256(payload).hexdigest(),
        members=members,
    )


def zip_members(payload: bytes) -> tuple[str, ...]:
    """读出容器的成员名（用于第④道宏闸门）；**非 zip 或已损坏 → 空元组**。

    为什么这里不抛"无法解析"：本模块只做**静态**判定，"可解析"是第③道之外的另一档，
    由受限子进程里的 ``ooxml`` 完成（见模块文档）。一个打不开的 zip 在那一档会被
    ``E_TEMPLATE_UNREADABLE`` 拒掉；若这里也抛，同一个问题就有两个错误码，
    而调用方无从判断该听谁的。

    读成员名而不是解压内容：只读中央目录，不落盘、不展开——恶意文档的
    "解压炸弹"发生在解压时，不在读名字时。
    """
    try:
        with ZipFile(BytesIO(payload)) as archive:
            return tuple(archive.namelist())
    except (BadZipFile, OSError, ValueError):
        return ()
