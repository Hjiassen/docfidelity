"""稳定哈希与规范化序列化（DD-2.1 ``platform``）。

为什么不能直接用 ``hash()`` / ``json.dumps()``
----------------------------------------------
1. ``hash()`` 受 ``PYTHONHASHSEED`` 影响，同一进程重启后结果不同；且它不抗碰撞，
   不能用作指纹。
2. ``json.dumps`` 默认保留插入顺序并输出空格。同一份数据因为 Python 版本或构造
   顺序不同就可能序列化成两种字节，进而得到两个"摘要"——那正是 1.2 第 7 条
   "每次构建都报满屏假差异"的成因。

因此本模块提供**唯一**的确定性序列化：键排序、紧凑分隔符、不转义非 ASCII
（中文不进 ``\\uXXXX``，便于人工比对），并统一用 sha256。

分帧纪律
--------
所有把多个片段折成一个摘要的函数都必须**长度前缀分帧**。
``{"ab": "c"}`` 与 ``{"a": "bc"}`` 直接拼接会得到同一串字节——不长度前缀的话，
两种完全不同的结构会算出同一个指纹。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any, Final

_CHUNK_SIZE: Final = 1 << 20
"""流式读文件的块大小（1 MiB）。"""

_FRAME_BYTES: Final = 4
"""片段长度前缀的字节数（``4`` 足够表达 4 GiB 的单个片段）。"""


def sha256_hex(payload: bytes) -> str:
    """字节内容的 sha256 十六进制摘要。"""
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path | str) -> str:
    """文件内容的 sha256 摘要（流式读取，不整体载入内存）。"""
    hasher = hashlib.sha256()
    with open(path, "rb") as stream:
        while chunk := stream.read(_CHUNK_SIZE):
            hasher.update(chunk)
    return hasher.hexdigest()


def canonical_json(payload: Any) -> bytes:
    """确定性 JSON 序列化。

    ``sort_keys=True`` 保证字典顺序无关；``separators`` 去掉全部可选空白；
    ``ensure_ascii=False`` 让中文保持可读；``allow_nan=False`` 拒绝 NaN / Infinity
    ——它们在 JSON 里不是合法字面量，放进去会产生无法解析的产物。
    """
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def digest_payload(payload: Any) -> str:
    """任意可 JSON 化对象的确定性摘要。"""
    return sha256_hex(canonical_json(payload))


def _hasher_update_framed(hasher: Any, raw: bytes) -> None:
    hasher.update(len(raw).to_bytes(_FRAME_BYTES, "big"))
    hasher.update(raw)


def combine_digests(sections: Mapping[str, str]) -> str:
    """把分区摘要合成一个整体摘要，**与字典插入顺序无关**。

    用于模板指纹的整体摘要（DD-7.4）：分区摘要的顺序不该影响整体结果，
    否则"换个遍历顺序"就会被误判成模板变更。
    """
    hasher = hashlib.sha256()
    for key in sorted(sections):
        _hasher_update_framed(hasher, key.encode("utf-8"))
        _hasher_update_framed(hasher, sections[key].encode("utf-8"))
    return hasher.hexdigest()


def digest_entries(entries: Iterable[tuple[str, bytes]]) -> str:
    """把「相对路径 → 内容」集合折成摘要。

    路径统一成 POSIX 分隔符后再哈希：同一份模板包在 Windows 与 Linux 上必须
    得到同一个 ``package_hash``，否则跨平台产物无法比对。
    """
    hasher = hashlib.sha256()
    for name, payload in sorted(entries, key=lambda item: item[0]):
        _hasher_update_framed(hasher, name.replace("\\", "/").encode("utf-8"))
        _hasher_update_framed(hasher, payload)
    return hasher.hexdigest()


__all__ = [
    "canonical_json",
    "combine_digests",
    "digest_entries",
    "digest_payload",
    "sha256_file",
    "sha256_hex",
]
