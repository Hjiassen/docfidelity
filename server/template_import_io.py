"""V2 不可信上传、素材边界与跨进程事务锁。"""

from __future__ import annotations

import base64
import binascii
import errno
import os
import stat
import sys
import threading
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from io import BytesIO
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import BinaryIO
from zipfile import BadZipFile, ZipFile

from PIL import Image, UnidentifiedImageError

from docfidelity.contracts.errors import EngineError, ErrorCode

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl

_THREAD_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()
MAX_ZIP_MEMBERS = 4096
MAX_ZIP_MEMBER_BYTES = 64 * 1024 * 1024
MAX_ZIP_EXPANDED_BYTES = 256 * 1024 * 1024
MAX_ZIP_RATIO = 1000
MAX_ASSET_BYTES = 16 * 1024 * 1024
MAX_IMAGE_PIXELS = 40_000_000


def _verify_image(payload: bytes) -> None:
    """验证实际位图容器并完整解码，不能只相信文件名与魔数。"""
    with Image.open(BytesIO(payload)) as image:
        if image.format not in {"PNG", "JPEG", "GIF", "BMP", "TIFF", "WEBP"}:
            raise ValueError("图片类型不支持")
        if image.width * image.height > MAX_IMAGE_PIXELS:
            raise ValueError("图片像素数量超限")
        image.verify()
    with Image.open(BytesIO(payload)) as image:
        image.load()


def logical_path(name: str) -> str:
    """只允许规范相对 POSIX 路径，拒绝平台相关路径语法。"""
    posix, windows = PurePosixPath(name), PureWindowsPath(name)
    if (
        not name
        or "\x00" in name
        or "\\" in name
        or ":" in name
        or posix.is_absolute()
        or windows.drive
        or windows.root
        or any(part in {"", ".", ".."} for part in name.split("/"))
    ):
        raise EngineError(ErrorCode.PATH_ESCAPE, "逻辑路径不安全")
    return posix.as_posix()


def validate_docx_zip(payload: bytes) -> tuple[str, ...]:
    """在解析前验证 ZIP 全部成员与实际展开量，不把内容解压到磁盘。"""
    try:
        with ZipFile(BytesIO(payload)) as archive:
            infos = archive.infolist()
            if len(infos) > MAX_ZIP_MEMBERS:
                raise EngineError(ErrorCode.UPLOAD_SIZE, "ZIP 成员数量超限")
            seen: set[str] = set()
            total = 0
            for info in infos:
                # ZipInfo 在 Windows 会改写反斜杠、截断 NUL；安全检查须看原始名称。
                raw_name = info.orig_filename
                name = logical_path(raw_name.rstrip("/") if info.is_dir() else raw_name)
                canonical = name.casefold()
                if canonical in seen:
                    raise EngineError(ErrorCode.UPLOAD_TYPE, "ZIP 包含重复成员")
                seen.add(canonical)
                if stat.S_ISLNK(info.external_attr >> 16):
                    raise EngineError(ErrorCode.PATH_ESCAPE, "ZIP 不允许符号链接")
                if (
                    info.flag_bits & 1
                    or canonical.endswith("vbaproject.bin")
                    or canonical.endswith("vbadata.xml")
                ):
                    raise EngineError(ErrorCode.UPLOAD_TYPE, "DOCX 含宏或加密成员")
                total += info.file_size
                if (
                    info.file_size > MAX_ZIP_MEMBER_BYTES
                    or total > MAX_ZIP_EXPANDED_BYTES
                    or info.file_size > max(info.compress_size, 1) * MAX_ZIP_RATIO
                ):
                    raise EngineError(ErrorCode.UPLOAD_SIZE, "ZIP 展开量或压缩比超限")
            actual_total = 0
            for info in infos:
                actual_member = 0
                content_types = bytearray()
                with archive.open(info) as stream:
                    while chunk := stream.read(1024 * 1024):
                        actual_member += len(chunk)
                        actual_total += len(chunk)
                        if (
                            actual_member > MAX_ZIP_MEMBER_BYTES
                            or actual_total > MAX_ZIP_EXPANDED_BYTES
                        ):
                            raise EngineError(ErrorCode.UPLOAD_SIZE, "ZIP 实际展开量超限")
                        if info.filename == "[Content_Types].xml":
                            content_types.extend(chunk)
                if (
                    b"macroenabled" in content_types.lower()
                    or b"vbaproject" in content_types.lower()
                ):
                    raise EngineError(ErrorCode.UPLOAD_TYPE, "DOCX 含宏内容类型")
            names = tuple(info.filename for info in infos)
            if not {"[Content_Types].xml", "word/document.xml", "_rels/.rels"}.issubset(names):
                raise EngineError(ErrorCode.UPLOAD_TYPE, "ZIP 不是 DOCX 文档")
            return names
    except (BadZipFile, OSError, ValueError, RuntimeError, NotImplementedError) as exc:
        raise EngineError(ErrorCode.TEMPLATE_UNREADABLE, "DOCX ZIP 不可读取") from exc


def write_image_assets(directory: Path, assets: Mapping[str, str]) -> None:
    """验证 base64 和实际位图后落盘，逻辑名不允许引用本机文件。"""
    total = 0
    for name, encoded in assets.items():
        relative = logical_path(name)
        try:
            payload = base64.b64decode(encoded, validate=True)
            total += len(payload)
            if total > MAX_ASSET_BYTES:
                raise EngineError(ErrorCode.UPLOAD_SIZE, "图片素材总量超限")
            _verify_image(payload)
        except (
            binascii.Error,
            ValueError,
            OSError,
            UnidentifiedImageError,
            Image.DecompressionBombError,
        ) as exc:
            raise EngineError(ErrorCode.UPLOAD_TYPE, "图片素材不是有效 base64 位图") from exc
        target = directory / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)


def _try_lock(stream: BinaryIO) -> bool:
    try:
        if sys.platform == "win32":
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError as exc:
        if exc.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
            raise
        return False


@contextmanager
def import_lock(path: Path) -> Iterator[None]:
    """线程锁配合系统文件锁；进程崩溃由操作系统释放锁。"""
    with _LOCKS_GUARD:
        local = _THREAD_LOCKS.setdefault(str(path.resolve()), threading.RLock())
    with local:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a+b") as stream:
                if stream.seek(0, os.SEEK_END) == 0:
                    stream.write(b"\0")
                    stream.flush()
                deadline = time.monotonic() + 30
                while not _try_lock(stream):
                    if time.monotonic() >= deadline:
                        raise EngineError(ErrorCode.STATE_INVALID, "导入事务锁等待超时")
                    time.sleep(0.02)
                try:
                    yield
                finally:
                    if sys.platform == "win32":
                        stream.seek(0)
                        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        except OSError as exc:
            raise EngineError(ErrorCode.INTERNAL, "导入事务文件操作失败") from exc
