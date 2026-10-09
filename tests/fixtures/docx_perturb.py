"""易变标识扰动与部件改写：构造"只改了一处"的对照件。

从 ``docx_builder`` 拆出的第三块（NFR-09 行数上限），内容未改。它是归一化与指纹两类
测试的**共用输入生成器**——两套扰动写法会得到两个结论，而其中一个必然是错的，所以
这里只有这一份实现，谁也不许另写。
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path

from lxml import etree

from .docx_parts import (
    DCTERMS_PREFIX,
    R_PREFIX,
    W_PREFIX,
    parse,
    read_parts,
    serialize,
    write_parts,
)

_TOC_NAME_RE = re.compile(r"^_Toc\d+$")


def rewrite_part(
    source: Path,
    dest: Path,
    part_name: str,
    mutate: Callable[[etree._Element], None],
) -> Path:
    """改写包内某个 XML 部件并另存，用于构造"模板被改了一处"的对照件。

    只改指定部件、其余部件字节照搬，是为了让"改了什么"完全可控：测试要能说清
    "这一处改动只该影响某个分区"，而任何顺带产生的差异都会让断言失去意义。
    """
    parts = read_parts(source)
    root = parse(parts[part_name])
    mutate(root)
    parts[part_name] = serialize(root)
    write_parts(dest, parts)
    return dest


def shuffle_volatile_identifiers(source: Path, dest: Path) -> Path:
    """只扰动易变标识，模拟"同一份模板被 Word 重新保存过"。

    扰动项与 ``ooxml.normalize.VOLATILE_RULES`` 一一对应：rsid、``_Toc`` 书签名
    **及其引用侧**（``w:hyperlink/@w:anchor`` 与 PAGEREF 域代码）、书签 id、
    部件关系 ID、``w:docId``、核心属性时间戳。

    **书签 start/end 的配对是结构，不是易变标识。** 只改 ``bookmarkEnd`` 会把配对
    打断，那是文档损坏；归一化有义务把它保留下来（``normalize`` 的"不省略匿名差异"），
    所以这里必须成对地改。这条纪律是踩出来的：先前只改 end 的版本能让"归一化
    没生效"看起来像测试写错了。

    归一化与指纹的测试共用本函数——两套扰动写法会得到两个结论，而其中一个必然是错的。
    """
    parts = read_parts(source)
    document = parse(parts["word/document.xml"])
    _perturb_rsids(document)
    _perturb_bookmarks(document)
    parts["word/document.xml"] = serialize(document)

    _perturb_relationship_ids(parts)

    if "word/settings.xml" in parts:
        settings = parse(parts["word/settings.xml"])
        doc_id = etree.SubElement(settings, f"{W_PREFIX}docId")
        doc_id.set(f"{W_PREFIX}val", "FFFFFFFF")
        parts["word/settings.xml"] = serialize(settings)

    if "docProps/core.xml" in parts:
        core = parse(parts["docProps/core.xml"])
        for element in core.iter():
            if element.tag in {f"{{{DCTERMS_PREFIX}}}modified", f"{{{DCTERMS_PREFIX}}}created"}:
                element.text = "2099-12-31T23:59:59Z"
        parts["docProps/core.xml"] = serialize(core)

    write_parts(dest, parts)
    return dest


def _perturb_rsids(root: etree._Element) -> None:
    for element in root.iter():
        for name in list(element.attrib):
            if name.startswith(W_PREFIX) and "rsid" in name:
                element.set(name, "00FF00FF")


def _perturb_bookmarks(root: etree._Element) -> None:
    """书签名与引用侧、书签 id 成对扰动。"""
    names = [
        element.get(f"{W_PREFIX}name")
        for element in root.iter(f"{W_PREFIX}bookmarkStart")
        if element.get(f"{W_PREFIX}name")
    ]
    rename = {
        name: f"_Toc{90000000 + index}"
        for index, name in enumerate(names)
        if name is not None and _TOC_NAME_RE.match(name)
    }
    id_map = {
        element.get(f"{W_PREFIX}id"): str(9000 + index)
        for index, element in enumerate(root.iter(f"{W_PREFIX}bookmarkStart"))
        if element.get(f"{W_PREFIX}id") is not None
    }
    for element in root.iter(f"{W_PREFIX}bookmarkStart", f"{W_PREFIX}bookmarkEnd"):
        identifier = element.get(f"{W_PREFIX}id")
        if identifier in id_map:
            element.set(f"{W_PREFIX}id", id_map[identifier])
    for element in root.iter(f"{W_PREFIX}bookmarkStart"):
        current = element.get(f"{W_PREFIX}name")
        if current in rename:
            element.set(f"{W_PREFIX}name", rename[current])
    if not rename:
        return
    for element in root.iter(f"{W_PREFIX}hyperlink"):
        anchor = element.get(f"{W_PREFIX}anchor")
        if anchor in rename:
            element.set(f"{W_PREFIX}anchor", rename[anchor])
    for element in root.iter(f"{W_PREFIX}instrText"):
        text = element.text
        if not text:
            continue
        for old, new in rename.items():
            text = text.replace(old, new)
        element.text = text


def _perturb_relationship_ids(parts: dict[str, bytes]) -> None:
    """把每个关系文件里的 Id 倒序重排，并同步改写归属部件里的引用。

    倒序而不是"统一改成新值"：关系 Id 是**引用键**，只改定义侧会让包变成
    一堆指不到目标的引用——那是损坏。倒序是一个真正的置换，既有变化又保持自洽。
    """
    for rels_path in [name for name in parts if name.endswith(".rels")]:
        rels = parse(parts[rels_path])
        identifiers = [element.get("Id") for element in rels if element.get("Id")]
        if len(identifiers) < 2:
            continue
        mapping = dict(zip(identifiers, reversed(identifiers), strict=True))
        for element in rels:
            current = element.get("Id")
            if current in mapping:
                element.set("Id", mapping[current])
        parts[rels_path] = serialize(rels)

        owner = _rels_owner(rels_path)
        if owner is None or owner not in parts:
            continue
        root = parse(parts[owner])
        for element in root.iter():
            for name, value in list(element.attrib.items()):
                if name.startswith(R_PREFIX) and value in mapping:
                    element.set(name, mapping[value])
        parts[owner] = serialize(root)


def _rels_owner(rels_path: str) -> str | None:
    """关系文件路径 → 归属部件路径；包级关系返回 ``None``（没有归属部件）。"""
    if rels_path == "_rels/.rels":
        return None
    directory, _, filename = rels_path.rpartition("/")
    if not directory.endswith("_rels") or not filename.endswith(".rels"):
        return None
    owner_directory = directory[: -len("_rels")].rstrip("/")
    owner_name = filename[: -len(".rels")]
    return f"{owner_directory}/{owner_name}" if owner_directory else owner_name


__all__ = [
    "rewrite_part",
    "shuffle_volatile_identifiers",
]
