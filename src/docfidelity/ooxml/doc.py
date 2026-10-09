"""OOXML 元素门面：命名空间、标签构造与元素读写（DD-2.1 / DD-4）。

设计取舍
--------
1. **读写路径不使用 python-docx。** ``python-docx`` 的读写会重建部件、丢失未建模的
   属性与顺序，与「一切写入都是在原件深拷贝上做最小必要改动」这条铁律冲突
   （DD-4 开头）。本模块因此直接操作 ``lxml`` 元素树；``python-docx`` 仅用于
   测试夹具与模板创建，不参与渲染路径。
2. **标签一律写带前缀形式**（``w:pStyle`` / ``w:rsidR``），由 :func:`qn` 译为
   Clark 记法。前辍写错立即抛 :class:`~docfidelity.contracts.errors.EngineError`，
   不允许静默产生无命名空间的裸标签 —— 裸标签在 Word 里会被当成未知扩展丢弃，
   症状是"样式静默失效"，极难排查。
"""

from __future__ import annotations

import copy
from collections.abc import Iterator, Mapping, Sequence
from typing import Final

from lxml import etree

from docfidelity.contracts.errors import EngineError, ErrorCode

#: 命名空间前缀表。仅登记引擎实际读写的命名空间；新增必须有实测用例。
NS: Final[dict[str, str]] = {
    "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "wp": "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing",
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "pic": "http://schemas.openxmlformats.org/drawingml/2006/picture",
    "ct": "http://schemas.openxmlformats.org/package/2006/content-types",
    "pkgrel": "http://schemas.openxmlformats.org/package/2006/relationships",
    "cp": "http://schemas.openxmlformats.org/package/2006/metadata/core-properties",
    "dc": "http://purl.org/dc/elements/1.1/",
    "dcterms": "http://purl.org/dc/terms/",
    "mc": "http://schemas.openxmlformats.org/markup-compatibility/2006",
    "w14": "http://schemas.microsoft.com/office/word/2010/wordml",
    "xml": "http://www.w3.org/XML/1998/namespace",
}

#: 元素别名。lxml 的 ``_Element`` 是私有名，集中在此处引用一次，其余模块不再触碰。
Element = etree._Element


def qn(tag: str) -> str:
    """把 ``w:pStyle`` 形式的前缀标签译为 Clark 记法 ``{ns}pStyle``。

    ``tag`` 必须带前缀；未知前缀与无前缀一律拒绝。
    """
    prefix, _, local = tag.partition(":")
    if not local:
        raise EngineError(
            ErrorCode.INTERNAL,
            f"OOXML 标签必须带命名空间前缀：{tag!r}（裸标签会被 Word 当作未知扩展丢弃）",
            where={"tag": tag},
            remediation=["改用 'w:pStyle' 这类带前缀写法"],
        )
    namespace = NS.get(prefix)
    if namespace is None:
        raise EngineError(
            ErrorCode.INTERNAL,
            f"未登记的命名空间前缀：{prefix!r}",
            where={"tag": tag, "known_prefixes": sorted(NS)},
            remediation=["在 ooxml.doc.NS 中登记该命名空间，并补一条实测用例"],
        )
    return f"{{{namespace}}}{local}"


def localname(element: Element) -> str:
    """元素或属性的局部名（去命名空间）。"""
    tag = element.tag
    return tag.rpartition("}")[2] if isinstance(tag, str) else ""


def namespace_of(element: Element) -> str:
    """元素的命名空间 URI；无命名空间返回空串。"""
    tag = element.tag
    if not isinstance(tag, str) or not tag.startswith("{"):
        return ""
    return tag[1:].partition("}")[0]


def attr(element: Element | None, tag: str) -> str | None:
    """读取带前缀属性；元素缺失或属性不存在返回 ``None``。

    允许 ``None`` 入参是为了让 ``attr(find_first(el, 'w:val'), ...)`` 这类链式读取
    不必写三重判空——判空写法一旦漏写就是 ``AttributeError`` 崩在模板上。
    """
    if element is None:
        return None
    return element.get(qn(tag))


def set_attr(element: Element, tag: str, value: str) -> None:
    """写入带前缀属性（原属性被覆盖，保留其在文档中的位置）。"""
    element.set(qn(tag), value)


def drop_attr(element: Element, tag: str) -> bool:
    """删除带前缀属性；返回是否真的删掉了。"""
    key = qn(tag)
    if key in element.attrib:
        del element.attrib[key]
        return True
    return False


def sub(parent: Element, tag: str, attrs: Mapping[str, str] | None = None) -> Element:
    """在 ``parent`` 下追加子元素；``attrs`` 的键同样使用前缀写法。"""
    element = etree.SubElement(parent, qn(tag))
    for name, value in (attrs or {}).items():
        element.set(qn(name), value)
    return element


def find_first(root: Element | None, tag: str) -> Element | None:
    """按前缀标签查第一个直接子元素；``root`` 为 ``None`` 时返回 ``None``。"""
    if root is None:
        return None
    return root.find(qn(tag))


def child_of(parent: Element | None, tag: str) -> Element | None:
    """``parent`` 为 ``None`` 时返回 ``None``，否则 :func:`find_first`。

    不能写成 ``find_first(parent, tag) if parent else None``：lxml 元素的真值取决于
    子节点数量，``<w:rPr/>`` 这类空元素会被判为假，于是其上的属性静默丢失 ——
    症状是"某些样式尺寸突然读不到"，且只在特定模板上复现。
    """
    if parent is None:
        return None
    return find_first(parent, tag)


def find_all(root: Element, tag: str) -> list[Element]:
    """按前缀标签查全部直接子元素。"""
    return list(root.findall(qn(tag)))


def iter_elements(root: Element, tag: str) -> Iterator[Element]:
    """深度优先遍历指定标签的全部后代（含自身之后的全部层级）。"""
    yield from root.iter(qn(tag))


def xpath(root: Element, expression: str) -> list[Element]:
    """带命名空间的 XPath。返回元素列表（表达式不得返回标量）。"""
    result = root.xpath(expression, namespaces=NS)
    if isinstance(result, list):
        return [item for item in result if isinstance(item, etree._Element)]
    raise EngineError(
        ErrorCode.INTERNAL,
        f"XPath 必须返回节点集：{expression!r}",
        where={"xpath": expression},
        remediation=["改为返回节点的表达式，例如 .//w:p"],
    )


def xpath_first(root: Element, expression: str) -> Element | None:
    """:func:`xpath` 的首个结果；无命中返回 ``None``。"""
    hits = xpath(root, expression)
    return hits[0] if hits else None


def xpath_count(root: Element, expression: str) -> int:
    """带命名空间的 XPath 计数：``xpath_count(p, "preceding::w:p")``。"""
    result = root.xpath(f"count({expression})", namespaces=NS)
    return int(result) if isinstance(result, float) else 0


def text_of(element: Element) -> str:
    """元素的文本内容（``w:t`` / ``w:instrText`` 这类叶子用）。"""
    return element.text or ""


def set_text(element: Element, value: str) -> None:
    """写入元素文本，并按需补 ``xml:space="preserve"``。

    首尾空白被 Word 静默吞掉是"内容看似注入了、实际少一个空格"的根因，
    因此这里不是可选优化而是必须项。
    """
    element.text = value
    if value != value.strip():
        element.set(qn("xml:space"), "preserve")
    else:
        drop_attr(element, "xml:space")


def deep_copy(element: Element) -> Element:
    """深拷贝元素（跨文档克隆的唯一入口）。"""
    return copy.deepcopy(element)


def new_tree(root_tag: str, attrs: Mapping[str, str] | None = None) -> Element:
    """新建带完整命名空间声明的根元素。

    新建部件必须走本函数：由 lxml 自动生成的 ``ns0`` 前缀虽然语义正确，
    但会让产物与模板原件在文本 diff 上出现整片差异。
    """
    root = etree.Element(qn(root_tag), nsmap=NS)
    for name, value in (attrs or {}).items():
        root.set(qn(name), value)
    return root


def paragraph_text(paragraph: Element) -> str:
    """段落可见文本（拼接全部 ``w:t``，忽略域代码与批注引用）。"""
    return "".join(node.text or "" for node in paragraph.iter(qn("w:t")))


def ensure_paragraph_properties(paragraph: Element) -> Element:
    """取段落的 ``w:pPr``；不存在则**插入到首位**。

    ``w:pPr`` 必须位于 ``w:p`` 的首位（ISO/IEC 29500 的元素顺序约束）。
    追加到末尾的写法在 Word 里可能被容忍，但会让严格校验器与 LibreOffice 报错。
    """
    return ensure_leading_child(paragraph, "w:pPr")


def set_paragraph_text(paragraph: Element, value: str) -> Element:
    """把段落内容替换为单个文本运行，返回该运行。

    **保留 ``w:pPr``，删除其余全部子节点**：段落上的书签、域、批注锚点会一并消失。
    这条语义对"占位符替换"与"图注改写"是预期行为，对"正文改写"则不是——
    正文改写请走渲染层的注入流程，不要用这个函数。
    """
    properties = find_first(paragraph, "w:pPr")
    for child in list(paragraph):
        if child is properties:
            continue
        paragraph.remove(child)
    run = etree.SubElement(paragraph, qn("w:r"))
    text_element = etree.SubElement(run, qn("w:t"))
    set_text(text_element, value)
    return run


def set_on_off(
    parent: Element,
    tag: str,
    enabled: bool,
    *,
    order: Sequence[str] | None = None,
) -> Element | None:
    """设置 ``<w:keepNext/>`` 这类开关元素；``enabled=False`` 时删除该元素。

    删除而非写 ``w:val="0"``：两者语义相同，但前者不会在产物里留下无意义的
    差异噪音，也让"我们是否碰过这一段"可以从 XML 结构上直接看出来。

    ``order`` 给出 ``parent`` 的子元素规范序。**不传就会追加到末尾**——在
    ``w:pPr`` / ``w:trPr`` 这类顺序敏感容器上追加，Word 多半容忍，但 LibreOffice
    与严格校验器会拒收，且症状完全指不到"顺序"上。故顺序敏感的位置必须传。
    """
    element = find_first(parent, tag)
    if not enabled:
        if element is not None:
            parent.remove(element)
        return None
    if element is None:
        if order is None:
            element = etree.SubElement(parent, qn(tag))
        else:
            element = ensure_child_in_order(parent, tag, order)
    else:
        drop_attr(element, "w:val")
    return element


def serialize(element: Element, *, xml_declaration: bool = False) -> bytes:
    """确定性序列化（无 pretty print）。

    **不得开启 pretty print**：格式化会插入空白文本节点，破坏 ``xml:space``
    语义，也会让指纹与比对结果依赖序列化选项。
    """
    return etree.tostring(
        element,
        encoding="UTF-8",
        xml_declaration=xml_declaration,
        standalone=True if xml_declaration else None,
    )


def serialize_text(element: Element) -> str:
    """序列化为 ``str``（进契约的 ``raw_xml`` 字段用）。"""
    return etree.tostring(element, encoding="unicode")


def parse(data: bytes | str) -> Element:
    """解析 XML；失败即抛 ``E_TEMPLATE_UNREADABLE`` 而非裸异常。"""
    try:
        return etree.fromstring(data)
    except etree.XMLSyntaxError as exc:
        raise EngineError(
            ErrorCode.TEMPLATE_UNREADABLE,
            f"XML 解析失败：{exc}",
            where={"reason": str(exc)},
            remediation=["确认部件未被外部工具截断或改写", "重新导出模板原件"],
        ) from exc


def ensure_child_in_order(parent: Element, tag: str, order: Sequence[str]) -> Element:
    """取子元素；不存在则**按 ``order`` 声明的位置**插入一个空的。

    OOXML 里 ``w:tblPr`` / ``w:trPr`` / ``w:tcPr`` / ``w:rPr`` 的子元素顺序是
    规范约束，不是风格偏好。把 ``w:tblHeader`` 追加到 ``w:trPr`` 末尾，Word 可能
    照样打开，但 LibreOffice 与严格校验器会拒收，而症状是"跨页时表头不重复"这种
    完全联想不到顺序的地方。故新增子元素一律走本函数。

    ``order`` 是该容器下元素的**规范序**，按**局部名**给出（``"tblW"`` 而非
    ``"w:tblW"``）——调用方一律传带前缀的标签，故这里把 ``tag`` 也取局部名再查表。
    不这么做的话，``tag not in order`` 恒真，本函数会静默退化成"追加到末尾"：
    产物里子元素顺序仍然错，而"用不用这个函数"看不出任何差别。
    """
    existing = find_first(parent, tag)
    if existing is not None:
        return existing

    element = etree.Element(qn(tag))
    name = tag.partition(":")[2] or tag
    if name not in order:
        parent.append(element)
        return element

    rank = order.index(name)
    for index, child in enumerate(parent):
        position = _order_rank(child, order)
        if position is not None and position > rank:
            parent.insert(index, element)
            return element
    parent.append(element)
    return element


def ensure_leading_child(parent: Element, tag: str) -> Element:
    """取子元素；不存在则**插入到首位**。

    用于"必须是容器第一个子元素"的那些位置：``w:pPr`` 之于 ``w:p``、
    ``w:tblPr`` 之于 ``w:tbl``、``w:tcPr`` 之于 ``w:tc``。追加到末尾的写法
    Word 多半容忍，但严格校验器与 LibreOffice 会拒收，而报错信息完全指不到"顺序"。
    """
    existing = find_first(parent, tag)
    if existing is not None:
        return existing
    element = etree.Element(qn(tag))
    parent.insert(0, element)
    return element


def _order_rank(child: Element, order: Sequence[str]) -> int | None:
    """子元素在规范序里的名次；不在表内返回 ``None``。"""
    name = localname(child)
    try:
        return order.index(name)
    except ValueError:
        return None


__all__ = [
    "NS",
    "Element",
    "attr",
    "child_of",
    "deep_copy",
    "drop_attr",
    "ensure_child_in_order",
    "ensure_leading_child",
    "ensure_paragraph_properties",
    "find_all",
    "find_first",
    "iter_elements",
    "localname",
    "namespace_of",
    "new_tree",
    "paragraph_text",
    "parse",
    "qn",
    "serialize",
    "serialize_text",
    "set_attr",
    "set_on_off",
    "set_paragraph_text",
    "set_text",
    "sub",
    "text_of",
    "xpath",
    "xpath_count",
    "xpath_first",
]
