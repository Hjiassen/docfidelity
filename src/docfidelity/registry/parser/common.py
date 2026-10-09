"""入库解析阶段共用的 OOXML 文本读取助手。

放在一个模块里，是为了让"文本口径"只有一处：骨架识别、占位符检索、版块定位
都要读段落文本，如果各自实现，就会出现"骨架认得出这个标题、占位符认不出"
这种自相矛盾的结果。
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Final

from docfidelity.ooxml import DocxPackage, Element
from docfidelity.ooxml.doc import localname, paragraph_text, qn

#: 需要扫描文本的部件：正文 + 页眉页脚 + 脚注尾注（编号与占位符可能藏在这里）
SCANNABLE_PART_PREFIXES: Final[tuple[str, ...]] = (
    "word/document.xml",
    "word/header",
    "word/footer",
    "word/footnotes.xml",
    "word/endnotes.xml",
)

#: 页眉页脚部件名里的序号，用于稳定排序（header1 < header2 < header10）
_PART_ORDINAL_RE: Final = re.compile(r"(\d+)")


def element_text(element: Element) -> str:
    """元素的可见文本：拼接其下全部 ``w:t``。

    不能用 ``doc.text_of``：那是**叶子**元素的 ``.text``。对 ``w:p`` / ``w:tbl``
    这类容器，``.text`` 永远是空串——文字在更深层的 ``w:t`` 里。这个差别不会
    报错，只会让所有基于文本的判断静默失效（骨架识别不出标题、占位符扫不到），
    所以文本提取必须统一走本函数。
    """
    return "".join(node.text or "" for node in element.iter(qn("w:t")))


def iter_paragraphs(part_root: Element) -> Iterator[Element]:
    """按文档顺序产出段落元素。"""
    yield from part_root.iter(qn("w:p"))


def paragraph_texts(pkg: DocxPackage, part_name: str) -> list[str]:
    """某个部件里全部段落的纯文本（按文档顺序）。"""
    part = pkg.find_part(part_name)
    if part is None:
        return []
    return [paragraph_text(paragraph) for paragraph in iter_paragraphs(part.read_tree)]


def table_texts(part_root: Element) -> list[str]:
    """部件里全部表格的纯文本（每张表折成一个字符串）。"""
    return [element_text(element) for element in part_root.iter(qn("w:tbl"))]


def header_footer_parts(pkg: DocxPackage) -> list[str]:
    """按稳定顺序返回页眉页脚部件名。"""
    names = [
        name for name in pkg.part_names("all") if localname_of_part(name) in {"header", "footer"}
    ]
    return sorted(names, key=_part_sort_key)


def localname_of_part(path: str) -> str:
    """从部件路径取出不带序号的部件类别名，如 ``word/header2.xml`` -> ``header``。"""
    filename = path.rpartition("/")[2]
    stem = filename.rpartition(".")[0]
    return _PART_ORDINAL_RE.sub("", stem)


def _part_sort_key(path: str) -> tuple[str, int]:
    match = _PART_ORDINAL_RE.search(path.rpartition("/")[2])
    return (localname_of_part(path), int(match.group(1)) if match else 0)


def core_property(pkg: DocxPackage, tag: str) -> str | None:
    """读取 ``docProps/core.xml`` 中的某个属性（如 ``title`` / ``subject``）。

    这里不做命名空间前缀推断：核心属性部件的标签**没有前缀**（``<cp:coreProperties>``
    下直属 ``<dc:title>``），所以按局部名匹配即可。
    """
    part = pkg.find_part("docProps/core.xml")
    if part is None:
        return None
    for element in part.read_tree.iter():
        if localname(element) == tag:
            value = (element.text or "").strip()
            return value or None
    return None


def first_text_sample(pkg: DocxPackage, part_name: str, limit: int) -> str:
    """某部件前 ``limit`` 个段落的文本拼接，用于在封面 / 页眉里找编号与版本。"""
    return "\n".join(paragraph_texts(pkg, part_name)[:limit])


def document_order_locator(tag: str, ordinal: int) -> str:
    """**文档序**位置型定位符：``(//w:p)[3]`` = 整篇第 3 个 ``w:p``。

    括号是语义的一部分，不是排版习惯
    ---------------------------------
    ``//w:p[3]`` 与 ``(//w:p)[3]`` 在 XPath 1.0 里是两个东西：

    * ``//w:p[3]`` —— 谓词作用在**每个父节点的子节点集**上，含义是"**每个**父节点下的
      第 3 个 ``w:p``"。一份含表格的说明书会命中几十个节点（每个 ``w:tc`` 都算一个
      父节点），而调用方取的是文档序第一个——于是定位符指向哪里，完全取决于"谁的
      第 3 段先出现"。这不是"偶有偏差"：封面里的占位段落藏在表格单元格中时，
      它会精确地指向「概述」标题段。
    * ``(//w:p)[3]`` —— 括号先把 ``//w:p`` 求成一个**节点集**，谓词再按文档序作用在
      整个节点集上。它精确表达"整篇第 3 个段落"。

    产生侧只有一个口径
    ------------------
    本函数的 ``ordinal`` 必须来自 :func:`docfidelity.ooxml.doc.iter_elements` 的枚举
    序号（1 起）——它用的就是文档序，与本函数严格同构。任何"另算一遍序号"的实现
    都会在段落藏在 ``w:sdt`` / ``w:tc`` 里时与它错位。
    """
    return f"(//w:{tag})[{ordinal}]"


__all__ = [
    "SCANNABLE_PART_PREFIXES",
    "core_property",
    "document_order_locator",
    "element_text",
    "first_text_sample",
    "header_footer_parts",
    "iter_paragraphs",
    "localname_of_part",
    "paragraph_texts",
    "table_texts",
]
