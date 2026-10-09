"""包与部件：zip 容器、关系解析、惰性加载与原子写（DD-4.1）。

三条纪律
--------
1. **惰性加载**：打开模板包时不解析任何 XML，只有真正访问元素树的部件才解析。
   大模板动辄上百个部件，全量解析是纯浪费。
2. **读用 ``read_tree``，写用 ``tree``**：只有 ``Part.tree`` 会置脏，``save`` 时置脏
   部件由元素树重新序列化，其余部件**按原始字节原样写回**。这是"样式层零改动"在
   文件层的落地——只读过、没改过的部件必须一个字节都不变。
   ``tests/unit/test_ooxml_package.py`` 里有一条"只读后保存 → 字节全等"的断言守着
   这条约定，任何读路径误用 ``tree`` 都会当场失败。
3. **``save`` 不做归一化**（DD-4.1）。归一化只服务于指纹与比对，产物必须保留原件
   的易变标识，否则 Word 打开时目录、修订、交叉引用会失去关联。
4. **zip 条目时刻钉死**（``date_time=ZIP_EPOCH``）。``zipfile.writestr`` 默认给条目写
   "现在"，于是**同一份输入连跑两次得到不同的产物字节**——部件内容一字不差，容器
   字节却不同。判据"连续 3 次构建幂等"（DD-17.1）与 golden 快照都会因此失去意义：
   它们看起来在比对"构建是否确定"，实际在比对"两次运行相隔几秒"。理由与
   ``platform.fs.zip_directory`` 完全一致（那里是导出件的凭据，这里是产物）。
"""

from __future__ import annotations

import os
import posixpath
import re
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, Literal

from docfidelity.contracts.errors import EngineError, ErrorCode
from docfidelity.platform.fs import ZIP_EPOCH

from .doc import Element, parse, serialize

PartScope = Literal["all", "xml", "content", "styles", "numbering"]

CONTENT_TYPES_PART: Final = "[Content_Types].xml"
PACKAGE_RELS_PART: Final = "_rels/.rels"

DOCUMENT_PART: Final = "word/document.xml"
STYLES_PART: Final = "word/styles.xml"
NUMBERING_PART: Final = "word/numbering.xml"

_RELS_NS: Final = "http://schemas.openxmlformats.org/package/2006/relationships"
_CT_NS: Final = "http://schemas.openxmlformats.org/package/2006/content-types"

_CONTENT_PART_RE: Final = re.compile(
    r"^word/(document|header\d*|footer\d*|footnotes|endnotes)\.xml$"
)

_REL_TAG: Final = f"{{{_RELS_NS}}}Relationship"
_OVERRIDE_TAG: Final = f"{{{_CT_NS}}}Override"
_DEFAULT_TAG: Final = f"{{{_CT_NS}}}Default"


#: 图片扩展名 -> MIME 类型。新增图片部件的 Default 内容类型从这里取，
#: 保证「登记内容类型」与「写 media 部件」两处不会各写一份而漂移。
IMAGE_CONTENT_TYPES: Final[dict[str, str]] = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "bmp": "image/bmp",
    "tif": "image/tiff",
    "tiff": "image/tiff",
    "emf": "image/x-emf",
    "wmf": "image/x-wmf",
    "svg": "image/svg+xml",
}


def content_type_for_extension(extension: str) -> str:
    """扩展名 -> MIME 类型；未知扩展名回落为 ``application/octet-stream``。"""
    return IMAGE_CONTENT_TYPES.get(extension.lower(), "application/octet-stream")


def is_content_part(path: str) -> bool:
    """是否为承载正文的部件（正文 / 页眉页脚 / 脚注尾注）。

    样式 ID 重映射只扫这些部件——``styles.xml`` 里自己引用自己的定义指针
    另行处理（DD-4.2.1 步骤 3）。
    """
    return _CONTENT_PART_RE.match(path) is not None


def rels_path_for(owner: str) -> str:
    """部件的关系文件路径。``owner=""`` 表示包级关系。"""
    if not owner:
        return PACKAGE_RELS_PART
    directory, _, filename = owner.rpartition("/")
    prefix = f"{directory}/" if directory else ""
    return f"{prefix}_rels/{filename}.rels"


@dataclass(frozen=True)
class Relationship:
    """一条 ``Relationship`` 记录。"""

    rel_id: str
    type: str
    target: str
    target_mode: str = "Internal"

    @property
    def is_external(self) -> bool:
        return self.target_mode.lower() == "external"

    @property
    def role(self) -> str:
        """关系类型的局部名（``.../relationships/styles`` -> ``styles``）。"""
        return self.type.rpartition("/")[2]


@dataclass
class Part:
    """包内一个部件。``tree`` 首次访问时解析并置脏。"""

    path: str
    content_type: str
    data: bytes | None = None
    declaration: bool = True
    _tree: Element | None = field(default=None, repr=False)
    _dirty: bool = field(default=False, repr=False)

    @property
    def is_loaded(self) -> bool:
        """元素树是否已被解析过（读或写都算）。"""
        return self._tree is not None

    @property
    def is_dirty(self) -> bool:
        """是否被标记为"已修改"，即 ``save`` 时会由元素树重新序列化。

        与 :attr:`is_loaded` 分开是必要的：读路径（``read_tree``）也会解析元素树，
        但它承诺**不改字节**。只有 ``tree`` 会把 ``is_dirty`` 置真。
        """
        return self._dirty

    @property
    def tree(self) -> Element:
        """**可变**元素树。访问即置脏：save 时本部件将由元素树重新序列化。

        只有准备修改时才用它。纯读路径一律用 :attr:`read_tree`。
        """
        self._dirty = True
        return self.read_tree

    @property
    def read_tree(self) -> Element:
        """**只读**元素树。按需解析，但**不置脏**，save 时仍按原始字节写回。"""
        if self._tree is None:
            if self.data is None:
                raise EngineError(
                    ErrorCode.PACKAGE_INCOMPLETE,
                    f"部件无可解析内容：{self.path}",
                    where={"part": self.path},
                    remediation=["重新导出模板原件", "确认模板包未被人为裁剪"],
                )
            self._tree = parse(self.data)
        return self._tree

    def peek_tree(self) -> Element | None:
        """只读取已解析的树，不触发解析、不置脏。"""
        return self._tree

    def mark_dirty(self) -> None:
        """显式置脏（在别处拿到树、稍后才修改时使用）。"""
        self._dirty = True

    def to_bytes(self) -> bytes:
        """当前应当写回磁盘的字节。"""
        if self._dirty and self._tree is not None:
            return serialize(self._tree, xml_declaration=self.declaration)
        if self.data is not None:
            return self.data
        if self._tree is not None:
            return serialize(self._tree, xml_declaration=self.declaration)
        return b""


@dataclass
class ResolvedRelationship:
    """关系 + 其解析出的部件（外部目标或缺失部件时为 ``None``）。"""

    relationship: Relationship
    part: Part | None
    target_path: str


class DocxPackage:
    """一个 ``.docx`` 容器。

    用法::

        with DocxPackage.open(path, read_only=True) as pkg:
            styles = pkg.part("word/styles.xml")
    """

    def __init__(
        self,
        *,
        source: Path | None,
        parts: dict[str, Part],
        order: list[str],
        read_only: bool,
    ) -> None:
        self._source = source
        self._parts = parts
        self._order = order
        self._read_only = read_only

    # ------------------------------------------------------------------ 打开
    @classmethod
    def open(cls, path: str | Path, *, read_only: bool = True) -> DocxPackage:
        """打开 ``.docx``。任何失败都转成 ``EngineError``。"""
        source = Path(path)
        if not source.is_file():
            raise EngineError(
                ErrorCode.TEMPLATE_UNREADABLE,
                f"模板文件不存在：{source}",
                where={"path": str(source)},
                remediation=["确认文件路径", "重新上传模板原件"],
            )
        parts: dict[str, Part] = {}
        order: list[str] = []
        try:
            with zipfile.ZipFile(source) as archive:
                for info in archive.infolist():
                    if info.is_dir():
                        continue
                    name = info.filename
                    payload = archive.read(name)
                    parts[name] = _make_part(name, payload)
                    order.append(name)
        except zipfile.BadZipFile as exc:
            raise EngineError(
                ErrorCode.TEMPLATE_UNREADABLE,
                f"不是有效的 OOXML 包（zip 解析失败）：{exc}",
                where={"path": str(source)},
                remediation=["确认上传的是 .docx 而非 .doc；.doc 需先另存为 .docx"],
            ) from exc

        if CONTENT_TYPES_PART not in parts:
            raise EngineError(
                ErrorCode.TEMPLATE_UNREADABLE,
                "包内缺少 [Content_Types].xml，不是有效的 OOXML 包",
                where={"path": str(source)},
                remediation=["重新导出模板原件"],
            )
        return cls(source=source, parts=parts, order=order, read_only=read_only)

    @classmethod
    def from_parts(cls, parts: dict[str, bytes], *, read_only: bool = False) -> DocxPackage:
        """由内存中的部件表构造（模板包重建 / 测试夹具用）。"""
        built = {name: _make_part(name, payload) for name, payload in parts.items()}
        return cls(source=None, parts=built, order=list(parts), read_only=read_only)

    def close(self) -> None:
        """释放资源。当前实现无外部句柄，保留接口以便后续改为流式读取。"""

    def __enter__(self) -> DocxPackage:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -------------------------------------------------------------- 部件访问
    @property
    def read_only(self) -> bool:
        return self._read_only

    @property
    def source(self) -> Path | None:
        return self._source

    def part_names(self, scope: PartScope = "all") -> list[str]:
        """按原始顺序返回部件路径。"""
        return [name for name in self._order if _in_scope(name, scope, self._parts)]

    def parts(self, scope: PartScope = "all") -> list[Part]:
        """按原始顺序返回部件。"""
        return [self._parts[name] for name in self.part_names(scope)]

    def has_part(self, path: str) -> bool:
        return path in self._parts

    def part(self, path: str) -> Part:
        """取部件；缺失即抛 ``E_PACKAGE_INCOMPLETE``。"""
        try:
            return self._parts[path]
        except KeyError:
            raise EngineError(
                ErrorCode.PACKAGE_INCOMPLETE,
                f"模板包缺少必需部件：{path}",
                where={"part": path, "available": sorted(self._parts)[:20]},
                remediation=["重新导出模板原件", "确认模板未被外部工具裁剪"],
            ) from None

    def find_part(self, path: str) -> Part | None:
        """取部件；缺失返回 ``None``。"""
        return self._parts.get(path)

    def content_type_of(self, path: str) -> str:
        """部件的内容类型：先查 Override，再按扩展名查 Default。"""
        override = self._content_type_map().get(path)
        if override:
            return override
        extension = path.rpartition(".")[2].lower()
        return self._default_type_map().get(extension, "")

    # ------------------------------------------------------------ 关系解析
    def relationships(self, owner: str = "") -> list[Relationship]:
        """读取某个部件（``owner=""`` 为包级）的全部关系。"""
        rels_part = self.find_part(rels_path_for(owner))
        if rels_part is None:
            return []
        records: list[Relationship] = []
        for element in rels_part.read_tree:
            if element.tag != _REL_TAG:
                continue
            rel_id = element.get("Id")
            rel_type = element.get("Type")
            target = element.get("Target")
            if not rel_id or not rel_type or target is None:
                continue
            records.append(
                Relationship(
                    rel_id=rel_id,
                    type=rel_type,
                    target=target,
                    target_mode=element.get("TargetMode") or "Internal",
                )
            )
        return records

    def resolve_rels(
        self, src: Part | str, *, role: str | None = None, type: str | None = None
    ) -> list[ResolvedRelationship]:
        """按角色（关系类型局部名）或完整类型过滤并解析目标部件。

        设计文档中的 ``type`` 与内置函数同名，此处保留原签名以求与 DD-4.1 一致。
        """
        owner = src.path if isinstance(src, Part) else src
        resolved: list[ResolvedRelationship] = []
        for relationship in self.relationships(owner):
            if role is not None and relationship.role != role:
                continue
            if type is not None and relationship.type != type:
                continue
            target_path = resolve_target_path(owner, relationship)
            part = None if relationship.is_external else self.find_part(target_path)
            resolved.append(
                ResolvedRelationship(relationship=relationship, part=part, target_path=target_path)
            )
        return resolved

    def resolve_single(self, src: Part | str, *, role: str) -> ResolvedRelationship | None:
        """按角色取唯一目标；无命中返回 ``None``。"""
        hits = self.resolve_rels(src, role=role)
        return hits[0] if hits else None

    # ---------------------------------------------------------------- 写入
    def add_xml_part(self, path: str, content_type: str, tree: Element) -> Part:
        """新增（或替换）一个 XML 部件。"""
        self._require_writable(f"新增部件 {path}")
        part = Part(path=path, content_type=content_type, data=None, declaration=True, _tree=tree)
        part.mark_dirty()
        self._register(part, content_type)
        return part

    def add_binary_part(self, path: str, content_type: str, payload: bytes) -> Part:
        """新增一个二进制部件（图片、嵌入对象等）。"""
        self._require_writable(f"新增部件 {path}")
        part = Part(path=path, content_type=content_type, data=payload, declaration=False)
        self._register(part, content_type)
        return part

    def attach_relationship(
        self,
        owner: str,
        *,
        type: str,
        target: str,
        target_mode: str = "Internal",
    ) -> Relationship:
        """在 ``owner`` 的关系文件中追加一条关系，返回新关系（含分配的 rel id）。"""
        self._require_writable(f"为 {owner} 追加关系")
        rels_part = self._ensure_rels_part(owner)
        root = rels_part.tree
        existing = self.relationships(owner)
        used = {item.rel_id for item in existing}
        index = 1
        while f"rId{index}" in used:
            index += 1
        from .doc import sub  # 局部导入避免模块级循环

        element = sub(root, "pkgrel:Relationship")
        element.set("Id", f"rId{index}")
        element.set("Type", type)
        element.set("Target", target)
        if target_mode != "Internal":
            element.set("TargetMode", target_mode)
        rels_part.mark_dirty()
        return Relationship(rel_id=f"rId{index}", type=type, target=target, target_mode=target_mode)

    def ensure_relationship(
        self, owner: str, *, type: str, target: str, target_mode: str = "Internal"
    ) -> Relationship:
        """幂等追加关系：同类型同目标已存在则复用。"""
        for relationship in self.relationships(owner):
            if relationship.type == type and relationship.target == target:
                return relationship
        return self.attach_relationship(owner, type=type, target=target, target_mode=target_mode)

    def clone_part(self, path: str) -> Part:
        """深拷贝出一个**游离**部件，供跨文档克隆使用（不注册进本包）。"""
        source = self.part(path)
        if source.data is not None and not source.is_loaded:
            return Part(
                path=path,
                content_type=source.content_type,
                data=source.data,
                declaration=source.declaration,
            )
        from .doc import deep_copy

        return Part(
            path=path,
            content_type=source.content_type,
            data=None,
            declaration=source.declaration,
            _tree=deep_copy(source.read_tree),
        )

    def save(self, dest: str | Path) -> Path:
        """原子写：同目录临时文件 + ``os.replace``。

        **不执行任何归一化**（DD-4.1）。写回顺序保持原始顺序，新增部件追加在末尾。

        条目的 ``date_time`` 取 :data:`~docfidelity.platform.fs.ZIP_EPOCH` 而不是
        "现在"：默认的 `now` 会让同一份输入连跑两次产出不同的字节，而"连续 3 次构建
        幂等"与 golden 快照都建立在"内容相同 ⇒ 字节相同"之上（见模块文档第 4 条）。
        """
        self._require_writable("保存")
        target = Path(dest)
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.with_name(f"{target.name}.dftmp")
        try:
            with zipfile.ZipFile(temp, "w", zipfile.ZIP_DEFLATED) as archive:
                written: set[str] = set()
                for name in [*self._order, *self._parts.keys()]:
                    if name in written or name not in self._parts:
                        continue
                    written.add(name)
                    info = zipfile.ZipInfo(filename=name, date_time=ZIP_EPOCH)
                    info.compress_type = zipfile.ZIP_DEFLATED
                    # 来源系统与权限位也要钉：``ZipInfo`` 的默认值随 ``sys.platform``
                    # 变（Windows 写 0、POSIX 写 3），跨平台比对时同一个包会给出两种字节。
                    info.create_system = 3
                    info.external_attr = 0o644 << 16
                    archive.writestr(info, self._parts[name].to_bytes())
        except OSError as exc:
            temp.unlink(missing_ok=True)
            raise EngineError(
                ErrorCode.INTERNAL,
                f"写入产物失败：{exc}",
                where={"dest": str(target)},
                remediation=["检查目标目录写权限与磁盘空间"],
            ) from exc
        os.replace(temp, target)
        return target

    # -------------------------------------------------------------- 内容类型
    def ensure_default_type(self, extension: str, content_type: str) -> None:
        """确保 ``[Content_Types].xml`` 中有给定扩展名的 Default 声明。"""
        self._require_writable("登记内容类型")
        if self._default_type_map().get(extension.lower()) == content_type:
            return
        from .doc import sub

        element = sub(self.part(CONTENT_TYPES_PART).tree, "ct:Default")
        element.set("Extension", extension)
        element.set("ContentType", content_type)

    def ensure_override_type(self, path: str, content_type: str) -> None:
        """确保 ``[Content_Types].xml`` 中有给定部件的 Override 声明。"""
        self._require_writable("登记内容类型")
        if self._content_type_map().get(path) == content_type:
            return
        from .doc import sub

        element = sub(self.part(CONTENT_TYPES_PART).tree, "ct:Override")
        element.set("PartName", f"/{path}")
        element.set("ContentType", content_type)

    # ---------------------------------------------------------------- 内部
    def _register(self, part: Part, content_type: str) -> None:
        self._parts[part.path] = part
        if part.path not in self._order:
            self._order.append(part.path)
        extension = part.path.rpartition(".")[2].lower()
        if extension in IMAGE_CONTENT_TYPES:
            self.ensure_default_type(extension, IMAGE_CONTENT_TYPES[extension])
        elif not extension:
            self.ensure_override_type(part.path, content_type)

    def _ensure_rels_part(self, owner: str) -> Part:
        path = rels_path_for(owner)
        existing = self.find_part(path)
        if existing is not None:
            return existing
        from .doc import new_tree

        root = new_tree("pkgrel:Relationships")
        part = Part(path=path, content_type=_RELS_CONTENT_TYPE, data=None, _tree=root)
        part.mark_dirty()
        self._parts[path] = part
        if path not in self._order:
            self._order.append(path)
        return part

    def _content_type_map(self) -> dict[str, str]:
        ct = self.find_part(CONTENT_TYPES_PART)
        if ct is None:
            return {}
        return _parse_content_types(ct.read_tree)[0]

    def _default_type_map(self) -> dict[str, str]:
        ct = self.find_part(CONTENT_TYPES_PART)
        if ct is None:
            return {}
        return _parse_content_types(ct.read_tree)[1]

    def _require_writable(self, action: str) -> None:
        if self._read_only:
            raise EngineError(
                ErrorCode.INTERNAL,
                f"只读包不允许修改：{action}",
                where={"source": str(self._source) if self._source else None},
                remediation=["以 read_only=False 打开后再修改"],
            )


_RELS_CONTENT_TYPE: Final = "application/vnd.openxmlformats-package.relationships+xml"


def resolve_target_path(owner: str, relationship: Relationship) -> str:
    """关系目标解析为包内部件路径。

    相对目标按 **owner 所在目录**展开（不是根目录），并做 ``..`` 归一。
    这是最常见的实现错误：把 ``media/image1.png`` 当成根路径。
    """
    if relationship.is_external:
        return relationship.target
    target = relationship.target
    if target.startswith("/"):
        return posixpath.normpath(target.lstrip("/"))
    if not owner:
        return posixpath.normpath(target)
    directory = owner.rpartition("/")[0]
    return posixpath.normpath(posixpath.join(directory, target))


def iter_rels(pkg: DocxPackage, owner: str = "") -> Iterator[Relationship]:
    """遍历某个部件的关系（语法糖）。"""
    yield from pkg.relationships(owner)


def _parse_content_types(root: Element) -> tuple[dict[str, str], dict[str, str]]:
    """解析 ``[Content_Types].xml``，返回 ``(Override 表, Default 表)``。"""
    overrides: dict[str, str] = {}
    defaults: dict[str, str] = {}
    for element in root:
        if element.tag == _OVERRIDE_TAG:
            name = element.get("PartName")
            value = element.get("ContentType")
            if name and value:
                overrides[name.lstrip("/")] = value
        elif element.tag == _DEFAULT_TAG:
            extension = element.get("Extension")
            value = element.get("ContentType")
            if extension and value:
                defaults[extension.lower()] = value
    return overrides, defaults


def _apply_content_types(parts: dict[str, Part]) -> None:
    """把内容类型回填到各部件（``Part.content_type`` 便于诊断与作用域判断）。"""
    ct = parts.get(CONTENT_TYPES_PART)
    if ct is None or ct.data is None:
        return
    overrides, defaults = _parse_content_types(parse(ct.data))
    for name, part in parts.items():
        extension = name.rpartition(".")[2].lower()
        part.content_type = overrides.get(name) or defaults.get(extension, "")


def _make_part(name: str, payload: bytes) -> Part:
    declaration = payload.lstrip().startswith(b"<?xml")
    return Part(name, "", data=payload, declaration=declaration)


def _in_scope(name: str, scope: PartScope, parts: dict[str, Part]) -> bool:
    if scope == "all":
        return True
    if scope == "content":
        return is_content_part(name)
    if scope == "styles":
        return name == STYLES_PART
    if scope == "numbering":
        return name == NUMBERING_PART
    content_type = parts[name].content_type
    return name.endswith(".xml") or content_type.endswith("+xml")


__all__ = [
    "CONTENT_TYPES_PART",
    "DOCUMENT_PART",
    "IMAGE_CONTENT_TYPES",
    "NUMBERING_PART",
    "PACKAGE_RELS_PART",
    "STYLES_PART",
    "DocxPackage",
    "Part",
    "PartScope",
    "Relationship",
    "ResolvedRelationship",
    "content_type_for_extension",
    "is_content_part",
    "iter_rels",
    "rels_path_for",
    "resolve_target_path",
]
