"""易变标识归一化：指纹、版本 diff 与 golden 比对**共用**的唯一实现（DD-4.7）。

为什么必须只有一处实现
----------------------
1.2 第 7 条问题的根因就是"刷新域之后比对"走了两套口径：一边按归一化后的文本比，
一边按原始字节比，于是每次刷新都报出一堆假差异。只要有两个实现，就一定会漂移。
因此 :func:`normalize_volatile` 是全内核唯一入口——指纹、``SnapshotDiff``、
golden 快照三条链路都调它。

两条纪律
--------
* **不修改入参包**：所有处理都在元素的深拷贝上做。归一化只服务于"算摘要 / 做比对"，
  产物文件必须保留原件的易变标识（否则 Word 打开后目录、修订、交叉引用会失去关联）。
* **不省略匿名差异**：无法识别的关系引用原样保留，而不是替换成占位符——把没认出来的
  东西悄悄抹平，会让"两个完全不同的文档算出同一指纹"。
"""

from __future__ import annotations

import hashlib
import re
import xml.etree.ElementTree as ET
from collections.abc import Iterable
from typing import Final

from docfidelity.contracts.errors import EngineError

from .doc import Element, deep_copy, localname, namespace_of, qn, serialize
from .package import DocxPackage, rels_path_for

#: 易变标识处理表（DD-4.7 原文）。它同时是**文档**与**覆盖率测试的依据**：
#: ``tests/unit/test_ooxml_normalize.py`` 会断言第二列的每个动作都有对应处理分支。
#:
#: 对 ``name->_Toc*`` 的一处细化：目录书签**及其全部引用侧**（``w:hyperlink/@w:anchor``、
#: ``PAGEREF`` 域代码）一起归一化。只改定义侧的话，Word 一重排书签，指纹照样变，
#: 产物目录也会指向不存在的书签。
VOLATILE_RULES: Final[tuple[tuple[str, str], ...]] = (
    ("w:rsid*", "drop"),
    ("w:bookmarkStart[@w:name~='_Toc\\d+']", "name->_Toc*"),
    ("w:bookmarkEnd[@w:id]", "renumber"),
    ("r:id", "remap_by_role"),
    ("w:docId", "drop"),
    ("cp:coreProperties/dcterms:modified|created", "drop"),
    ("w:szCs|w:sz@w:val in sectPr", "keep"),
)

#: 已实现的动作集合。新增规则必须同时新增分支，否则覆盖率测试失败。
HANDLED_ACTIONS: Final[frozenset[str]] = frozenset(
    {"drop", "name->_Toc*", "renumber", "remap_by_role", "keep"}
)

_W_NS: Final = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_R_NS: Final = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_DCTERMS_NS: Final = "http://purl.org/dc/terms/"
_MC_NS: Final = "http://schemas.openxmlformats.org/markup-compatibility/2006"

_TOC_BOOKMARK_RE: Final = re.compile(r"^_Toc\d+$")

#: ``w:docId`` 所属的部件：只出现在设置部件里。限定部件可避免误删同名的业务元素。
_SETTINGS_PARTS: Final = ("word/settings.xml",)

#: 核心属性部件里的易变字段。
_CORE_PART: Final = "docProps/core.xml"


def normalized_parts(pkg: DocxPackage) -> dict[str, bytes]:
    """各部件**归一化后**的序列化字节（``normalize_volatile`` 的分解形式）。

    把逐部件结果暴露出来，是为了让"分区 / 行级比对"（``qa.compare``）不必再实现一遍
    易变标识处理——本模块文档第 1 条说的正是这种漂移：两个实现迟早各说各话，
    而症状是"刷新域之后每次都报一堆假差异"。返回顺序按部件名升序，与
    :func:`normalize_volatile` 的拼接顺序一致。
    """
    rel_alias = _build_relationship_alias(pkg)
    payloads: dict[str, bytes] = {}
    for name in sorted(pkg.part_names("xml")):
        part = pkg.find_part(name)
        if part is None:
            continue
        try:
            tree = deep_copy(part.read_tree)
        except EngineError:
            # 非 XML 内容（少数模板会带奇怪扩展名）：跳过而不是让比对整体失败
            continue
        _apply_volatile_rules(name, tree, rel_alias)
        payloads[name] = serialize(tree)
    return payloads


def normalize_volatile(pkg: DocxPackage) -> bytes:
    """归一化后序列化整个包，作为指纹与比对的一致输入。

    输出是各部件归一化字节的确定性拼接（部件名升序，带长度前缀），因此
    "同一模板两次解析得到同一摘要"与"部件顺序变化不影响摘要"同时成立。
    """
    return b"".join(
        name.encode("utf-8") + b"\x00" + payload for name, payload in normalized_parts(pkg).items()
    )


def normalized_digest(pkg: DocxPackage) -> str:
    """归一化摘要（sha256 十六进制）。"""
    return hashlib.sha256(normalize_volatile(pkg)).hexdigest()


def normalized_element(element: Element) -> str:
    """XML 元素结构归一化：忽略修订标识、命名空间前缀与属性书写顺序。"""
    tree = deep_copy(element)
    _drop_rsids(tree)
    _drop_elements(tree, "rsid", namespaces={_W_NS})
    _normalize_compatibility_names(tree)
    return ET.canonicalize(serialize(tree).decode("utf-8"), rewrite_prefixes=True, strip_text=True)


def _normalize_compatibility_names(tree: Element) -> None:
    """兼容性属性中的前缀列表也有语义，先解析成 URI 再归一化。"""
    prefix_lists = {"Ignorable", "MustUnderstand"}
    qualified_lists = {"ProcessContent", "PreserveElements", "PreserveAttributes"}
    for element in tree.iter():
        for name, value in list(element.attrib.items()):
            local = localname_of_attribute(name)
            is_choice = element.tag == f"{{{_MC_NS}}}Choice" and name == "Requires"
            if not is_choice and (
                namespace_of_attribute(name) != _MC_NS
                or local not in prefix_lists | qualified_lists
            ):
                continue
            tokens: list[str] = []
            for token in value.split():
                prefix, separator, suffix = token.partition(":")
                uri = element.nsmap.get(prefix)
                if uri is None:
                    tokens.append(token)
                else:
                    tokens.append(f"{{{uri}}}{suffix if separator else ''}")
            element.set(name, " ".join(sorted(tokens)))


def digest_of(pkg: DocxPackage, part_names: Iterable[str]) -> str:
    """指定部件集合的归一化摘要，供分区指纹使用（DD-7.4）。

    分区摘要必须走同一套归一化逻辑，否则"样式分区没变、指纹却变了"会指向错误结论。
    """
    rel_alias = _build_relationship_alias(pkg)
    hasher = hashlib.sha256()
    for name in sorted(part_names):
        part = pkg.find_part(name)
        if part is None:
            continue
        tree = deep_copy(part.read_tree)
        _apply_volatile_rules(name, tree, rel_alias)
        hasher.update(name.encode("utf-8"))
        hasher.update(b"\x00")
        hasher.update(serialize(tree))
    return hasher.hexdigest()


# ------------------------------------------------------------------ 规则实现
def _apply_volatile_rules(
    part_name: str, tree: Element, rel_alias: dict[tuple[str, str], str]
) -> None:
    owner = owner_of_rels_path(part_name)
    _drop_rsids(tree)
    _renumber_bookmarks(tree)
    if owner is None:
        _remap_relationship_attributes(part_name, tree, rel_alias)
    else:
        _alias_relationship_ids(owner, tree, rel_alias)
    if part_name in _SETTINGS_PARTS:
        _drop_elements(tree, "docId")
    if part_name == _CORE_PART:
        _drop_elements(tree, "modified", namespaces={_DCTERMS_NS})
        _drop_elements(tree, "created", namespaces={_DCTERMS_NS})
    # ("w:szCs|w:sz@w:val in sectPr", "keep") —— 显式保留，不做任何处理。
    # 之所以把它写进规则表而不是省略，是为了挡住"顺手把它也归一化掉"的改动：
    # 节里缺失的 sz 会影响分节符的字体继承，抹掉它会让两份不同的版式算出同一指纹。


def _drop_rsids(tree: Element) -> None:
    """删除 ``w:rsid*`` 属性与 ``w:rsids`` 元素。

    rsid 是 Word 每次编辑生成的修订会话标识，同一内容每次保存都不同——
    它是"同一文档反复构建产物不一致"的最主要来源。
    """
    for element in tree.iter():
        for name in [key for key in element.attrib if _is_rsid(key)]:
            del element.attrib[name]
    for unwanted in [node for node in tree.iter() if node.tag == qn("w:rsids")]:
        parent = unwanted.getparent()
        if parent is not None:
            parent.remove(unwanted)


def _is_rsid(name: str) -> bool:
    return namespace_of_attribute(name) == _W_NS and localname_of_attribute(name).startswith("rsid")


def namespace_of_attribute(name: str) -> str:
    """属性的命名空间 URI；无命名空间返回空串。"""
    if not name.startswith("{"):
        return ""
    return name[1:].partition("}")[0]


def localname_of_attribute(name: str) -> str:
    """属性的局部名。"""
    return name.rpartition("}")[2]


def _renumber_bookmarks(tree: Element) -> None:
    """书签重编号，并把 ``_Toc123456`` 这类目录书签名归一化为 ``_Toc{n}``。

    目录书签 ID 由 Word 在每次刷新域时重新分配，是跨构建 diff 的第二大噪音源；
    名字里的数字本身没有语义，只有"第几个目录项"这一信息需要保留。

    **引用侧必须一起归一化**：``w:hyperlink/@w:anchor`` 与 ``PAGEREF`` 域代码里
    都写着书签名。只改定义侧不改引用侧的话，Word 一重排书签，指纹照样变——
    而且产物的目录会指向不存在的书签。两类引用统一走同一张映射表。
    """
    next_id = 1
    next_toc = 1
    id_map: dict[str, str] = {}
    toc_names: dict[str, str] = {}
    for element in tree.iter(qn("w:bookmarkStart")):
        old_id = element.get(qn("w:id"))
        if old_id is not None:
            id_map[old_id] = str(next_id)
            element.set(qn("w:id"), str(next_id))
            next_id += 1
        name = element.get(qn("w:name"))
        if name is not None and _TOC_BOOKMARK_RE.match(name):
            alias = f"_Toc{next_toc}"
            toc_names[name] = alias
            element.set(qn("w:name"), alias)
            next_toc += 1

    for element in tree.iter(qn("w:bookmarkEnd")):
        old_id = element.get(qn("w:id"))
        if old_id is not None and old_id in id_map:
            element.set(qn("w:id"), id_map[old_id])

    if not toc_names:
        return
    for element in tree.iter(qn("w:hyperlink")):
        anchor = element.get(qn("w:anchor"))
        if anchor is not None and anchor in toc_names:
            element.set(qn("w:anchor"), toc_names[anchor])
    for element in tree.iter(qn("w:instrText")):
        text = element.text
        if not text or "_Toc" not in text:
            continue
        for old_name, alias in toc_names.items():
            if old_name in text:
                text = text.replace(old_name, alias)
        element.text = text


def _remap_relationship_attributes(
    part_name: str, tree: Element, rel_alias: dict[tuple[str, str], str]
) -> None:
    """把 ``r:id`` / ``r:embed`` / ``r:link`` 换成按角色稳定排序后的别名。

    关系 ID 的分配顺序取决于关系文件里的书写顺序，而 Word 会因为插入页眉、
    删除图片等操作整片重排。不归一化的话，"只改了一个段落"会表现为全篇差异。
    """
    for element in tree.iter():
        for name, value in list(element.attrib.items()):
            if namespace_of_attribute(name) != _R_NS:
                continue
            alias = rel_alias.get((part_name, value))
            if alias is not None:
                element.set(name, alias)


def _alias_relationship_ids(
    owner: str, tree: Element, rel_alias: dict[tuple[str, str], str]
) -> None:
    """关系文件自身的 ``Id`` 按同一套别名改写，保持与引用侧一致。"""
    for element in tree:
        rel_id = element.get("Id")
        if rel_id is None:
            continue
        alias = rel_alias.get((owner, rel_id))
        if alias is not None:
            element.set("Id", alias)


def _drop_elements(tree: Element, local: str, namespaces: set[str] | None = None) -> None:
    targets = [
        node
        for node in tree.iter()
        if localname(node) == local and (namespaces is None or namespace_of(node) in namespaces)
    ]
    for node in targets:
        parent = node.getparent()
        if parent is not None:
            parent.remove(node)


# ------------------------------------------------------------------ 关系别名
def _build_relationship_alias(pkg: DocxPackage) -> dict[tuple[str, str], str]:
    """``(owner 部件, 原 rel id) -> 稳定别名``。

    排序键取 ``(关系类型, 目标, TargetMode)``：不依赖关系文件里的书写顺序，
    因此"往包里加一个页眉"不会改变其它关系的别名。
    """
    alias: dict[tuple[str, str], str] = {}
    for owner in _relationship_owners(pkg):
        records = sorted(
            pkg.relationships(owner),
            key=lambda item: (item.type, item.target, item.target_mode),
        )
        for index, record in enumerate(records, start=1):
            alias[(owner, record.rel_id)] = f"rId{index}"
    return alias


def _relationship_owners(pkg: DocxPackage) -> list[str]:
    """存在关系文件的部件路径（``""`` 表示包级）。"""
    owners: list[str] = []
    if pkg.has_part(rels_path_for("")):
        owners.append("")
    for name in pkg.part_names("all"):
        if pkg.has_part(rels_path_for(name)):
            owners.append(name)
    return owners


def owner_of_rels_path(path: str) -> str | None:
    """关系文件路径 -> 归属部件路径；不是关系文件则返回 ``None``。"""
    if path == rels_path_for(""):
        return ""
    directory, _, filename = path.rpartition("/")
    if not directory.endswith("_rels") or not filename.endswith(".rels"):
        return None
    owner_directory = directory[: -len("_rels")].rstrip("/")
    owner_name = filename[: -len(".rels")]
    return f"{owner_directory}/{owner_name}" if owner_directory else owner_name


__all__ = [
    "HANDLED_ACTIONS",
    "VOLATILE_RULES",
    "digest_of",
    "localname_of_attribute",
    "namespace_of_attribute",
    "normalize_volatile",
    "normalized_digest",
    "normalized_element",
    "normalized_parts",
    "owner_of_rels_path",
]
