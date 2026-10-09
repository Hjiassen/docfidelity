"""图片尺寸的头解析（零第三方依赖）。

PNG 的 ``IHDR`` 与 JPEG 的 ``SOFn`` 段里就写着像素尺寸，读文件头即可，不需要解码
整张图。为此引入图像库，等于给"接入一个图片目录"背上一整套图像栈——内核的依赖表是
刻意收窄的（``pyproject.toml`` 的 coredeps 只有四个包），能自解析就不加依赖。

拿不到尺寸返回 ``None``，**不返回 (0, 0)**：``(0, 0)`` 会一路漂到渲染层被当成
"图片没有大小"，而真相是"这个格式我们不认识"。
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

PNG_SIGNATURE: Final = b"\x89PNG\r\n\x1a\n"

#: 头解析最多读取的字节数。JPEG 的元数据段可能很长，但 SOF 通常在很前面。
HEAD_BYTES: Final = 256 * 1024

_JPEG_STANDALONE: Final = frozenset({0x01, 0xD8})
_SOF_MARKERS: Final = frozenset(
    {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
)


def png_size(payload: bytes) -> tuple[int, int] | None:
    """从 PNG 字节读 (宽, 高)；不是 PNG 或结构不完整返回 ``None``。"""
    if not payload.startswith(PNG_SIGNATURE) or len(payload) < 24:
        return None
    if payload[12:16] != b"IHDR":
        return None
    width = int.from_bytes(payload[16:20], "big")
    height = int.from_bytes(payload[20:24], "big")
    if width <= 0 or height <= 0:
        return None
    return width, height


def jpeg_size(payload: bytes) -> tuple[int, int] | None:
    """从 JPEG 字节读 (宽, 高)；走到扫描数据前还没找到 SOFn 则返回 ``None``。"""
    if not payload.startswith(b"\xff\xd8"):
        return None
    index = 2
    total = len(payload)
    while index + 4 <= total:
        if payload[index] != 0xFF:
            index += 1
            continue
        marker = payload[index + 1]
        if marker in _JPEG_STANDALONE or 0xD0 <= marker <= 0xD7:
            index += 2
            continue
        if marker == 0xDA:  # 扫描数据开始，之后不再有头段
            return None
        segment_length = int.from_bytes(payload[index + 2 : index + 4], "big")
        if segment_length < 2:
            return None
        if marker in _SOF_MARKERS:
            if index + 9 > total:
                return None
            height = int.from_bytes(payload[index + 5 : index + 7], "big")
            width = int.from_bytes(payload[index + 7 : index + 9], "big")
            if width <= 0 or height <= 0:
                return None
            return width, height
        index += 2 + segment_length
    return None


def image_size(path: Path | str) -> tuple[int, int] | None:
    """读文件头得到 (宽, 高)。不认识的格式返回 ``None``。"""
    target = Path(path)
    with open(target, "rb") as stream:
        payload = stream.read(HEAD_BYTES)
    if payload.startswith(PNG_SIGNATURE):
        return png_size(payload)
    if payload.startswith(b"\xff\xd8"):
        return jpeg_size(payload)
    return None


__all__ = ["HEAD_BYTES", "PNG_SIGNATURE", "image_size", "jpeg_size", "png_size"]
