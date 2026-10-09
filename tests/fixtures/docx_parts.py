"""docx 夹具的公共底座：命名空间常量与包部件读写。

为什么单独一个模块
------------------
只有一个理由：NFR-09 的单文件 800 行上限。``docx_builder`` 一度 939 行，同时承担
文档本体构造、编号契约、易变标识扰动、部件读写四件事。拆的是**文件**，不是**口径**：
这些原语在仓库里仍然只有这一份实现，其余夹具模块一律 ``from .docx_parts import``，
不许各自再包一层 ``zipfile.ZipFile``——那正是历轮踩过的"两套写法必然漂移"。

``W`` / ``R`` 等短名是**刻意**的：它们在本模块与调用方里出现的次数是几百次，
按 OOXML 前缀缩写命名（而非 ``WORDPROCESSINGML_NS``）才能让 XPath 那类长表达式可读。
"""

from __future__ import annotations

import zipfile
from pathlib import Path

from lxml import etree

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
CT = "http://schemas.openxmlformats.org/package/2006/content-types"
WP = "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
A = "http://schemas.openxmlformats.org/drawingml/2006/main"
PIC = "http://schemas.openxmlformats.org/drawingml/2006/picture"
DCTERMS = "http://purl.org/dc/terms/"

NSMAP = {"w": W, "r": R, "wp": WP, "a": A, "pic": PIC, "ct": CT, "pkgrel": PKG_REL}

W_PREFIX = f"{{{W}}}"
R_PREFIX = f"{{{R}}}"
CT_PREFIX = f"{{{CT}}}"
PKG_REL_PREFIX = f"{{{PKG_REL}}}"
DCTERMS_PREFIX = f"{{{DCTERMS}}}"


def read_parts(path: Path) -> dict[str, bytes]:
    """读整个包成 ``{部件名: 字节}``（目录条目跳过）。"""
    with zipfile.ZipFile(path) as archive:
        return {
            info.filename: archive.read(info.filename)
            for info in archive.infolist()
            if not info.is_dir()
        }


def write_parts(path: Path, parts: dict[str, bytes]) -> None:
    """按给定顺序整包写出。"""
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, payload in parts.items():
            archive.writestr(name, payload)


def parse(payload: bytes) -> etree._Element:
    """部件字节 → 元素；命名空间前缀由调用方自带（不做前缀改写）。"""
    return etree.fromstring(payload)


def serialize(root: etree._Element) -> bytes:
    """元素 → 部件字节。带 XML 声明与 ``standalone="yes"``，与 Word 的产物同形。"""
    return etree.tostring(root, encoding="UTF-8", xml_declaration=True, standalone=True)


__all__ = [
    "A",
    "CT",
    "CT_PREFIX",
    "DCTERMS",
    "DCTERMS_PREFIX",
    "NSMAP",
    "PIC",
    "PKG_REL",
    "PKG_REL_PREFIX",
    "R",
    "R_PREFIX",
    "W",
    "WP",
    "W_PREFIX",
    "parse",
    "read_parts",
    "serialize",
    "write_parts",
]
