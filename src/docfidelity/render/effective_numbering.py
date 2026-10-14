"""沿冻结段落和样式的指针消费编号级，创建隔离的新实例。"""

import re

from docfidelity.contracts.errors import EngineError, ErrorCode
from docfidelity.ooxml.doc import Element, deep_copy, find_first, new_tree, qn, sub, xpath
from docfidelity.ooxml.package import NUMBERING_PART, STYLES_PART, DocxPackage


def _unique(elements: list[Element]) -> Element:
    if len(elements) != 1:
        raise EngineError(ErrorCode.PACKAGE_INCOMPLETE, "冻结列表的编号指针缺失或重复")
    return elements[0]


def _numbering_pointer(package: DocxPackage, paragraph: Element) -> tuple[str, str]:
    containers = [paragraph]
    seen: set[str] = set()
    style = find_first(find_first(paragraph, "w:pPr"), "w:pStyle")
    style_id = style.get(qn("w:val")) if style is not None else None
    while style_id is not None:
        if style_id in seen:
            raise EngineError(ErrorCode.PACKAGE_INCOMPLETE, "冻结列表样式继承循环")
        seen.add(style_id)
        root = package.part(STYLES_PART).read_tree
        current = _unique(
            [e for e in xpath(root, "./w:style") if e.get(qn("w:styleId")) == style_id]
        )
        containers.append(current)
        base = find_first(current, "w:basedOn")
        style_id = base.get(qn("w:val")) if base is not None else None
    num_id: str | None = None
    ilvl: str | None = None
    for container in containers:
        properties = find_first(find_first(container, "w:pPr"), "w:numPr")
        if properties is None:
            continue
        num = find_first(properties, "w:numId")
        level = find_first(properties, "w:ilvl")
        if num_id is None and num is not None:
            num_id = num.get(qn("w:val"))
        if ilvl is None and level is not None:
            ilvl = level.get(qn("w:val"))
    if num_id is None or num_id == "0":
        raise EngineError(ErrorCode.PACKAGE_INCOMPLETE, "冻结列表没有声明编号指针")
    return num_id, ilvl or "0"


def _bound_level(package: DocxPackage, paragraph: Element, numbering: Element) -> Element:
    num_id, ilvl = _numbering_pointer(package, paragraph)
    if ilvl != "0":
        raise EngineError(ErrorCode.SCHEMA_INVALID, "当前冻结列表契约不支持多级编号")
    instance = _unique([e for e in xpath(numbering, "./w:num") if e.get(qn("w:numId")) == num_id])
    reference = _unique(xpath(instance, "./w:abstractNumId")).get(qn("w:val"))
    definition = _unique(
        [
            e
            for e in xpath(numbering, "./w:abstractNum")
            if e.get(qn("w:abstractNumId")) == reference
        ]
    )
    level = _unique([e for e in xpath(definition, "./w:lvl") if e.get(qn("w:ilvl")) == ilvl])
    overrides = [e for e in xpath(instance, "./w:lvlOverride") if e.get(qn("w:ilvl")) == ilvl]
    if len(overrides) > 1:
        raise EngineError(ErrorCode.PACKAGE_INCOMPLETE, "冻结列表编号级覆盖重复")
    if overrides:
        override_levels = xpath(overrides[0], "./w:lvl")
        if override_levels:
            level = _unique(override_levels)
    result = deep_copy(level)
    if overrides:
        starts = xpath(overrides[0], "./w:startOverride")
        if starts:
            start = find_first(result, "w:start")
            if start is None:
                start = new_tree("w:start")
                result.insert(0, start)
            start.set(qn("w:val"), _unique(starts).get(qn("w:val"), "1"))
    return result


def _neutral_level(family: str) -> Element:
    level = new_tree("w:lvl", {"w:ilvl": "0"})
    sub(level, "w:start", {"w:val": "1"})
    fmt, text = ("bullet", "\u2022") if family == "bullets" else ("decimal", "%1.")
    sub(level, "w:numFmt", {"w:val": fmt})
    sub(level, "w:lvlText", {"w:val": text})
    sub(level, "w:lvlJc", {"w:val": "left"})
    return level


def create_list_numbering(
    package: DocxPackage, paragraph: Element, family: str, *, neutral: bool
) -> str:
    """复制冻结绑定编号级；格式关闭时使用明确中性编号，返回新 numId。"""
    root = package.part(NUMBERING_PART).tree
    level = _neutral_level(family) if neutral else _bound_level(package, paragraph, root)
    fmt = _unique(xpath(level, "./w:numFmt"))
    text = _unique(xpath(level, "./w:lvlText"))
    original_fmt = fmt.get(qn("w:val"))
    label = text.get(qn("w:val"), "")
    if not original_fmt or not label or any(token != "%1" for token in re.findall(r"%\d+", label)):
        raise EngineError(ErrorCode.SCHEMA_INVALID, "冻结列表标记缺失或含未支持的多级引用")
    wants_bullet = family == "bullets"
    if wants_bullet != (original_fmt == "bullet"):
        # IR 改变列表语义时保留几何格式，符号字体不能用于阿拉伯序号。
        fmt.set(qn("w:val"), "bullet" if wants_bullet else "decimal")
        text.set(qn("w:val"), "\u2022" if wants_bullet else "%1.")
        for fonts in xpath(level, "./w:rPr/w:rFonts"):
            parent = fonts.getparent()
            assert parent is not None
            parent.remove(fonts)
    level.set(qn("w:ilvl"), "0")
    for link in xpath(level, "./w:pStyle"):
        level.remove(link)
    abstract_id = str(
        max([int(e.get(qn("w:abstractNumId"), "0")) for e in xpath(root, "./w:abstractNum")] or [0])
        + 1
    )
    num_id = str(max([int(e.get(qn("w:numId"), "0")) for e in xpath(root, "./w:num")] or [0]) + 1)
    abstract = new_tree("w:abstractNum", {"w:abstractNumId": abstract_id})
    sub(abstract, "w:multiLevelType", {"w:val": "singleLevel"})
    abstract.append(level)
    instances = xpath(root, "./w:num")
    if instances:
        instances[0].addprevious(abstract)
    else:
        root.append(abstract)
    instance = sub(root, "w:num", {"w:numId": num_id})
    sub(instance, "w:abstractNumId", {"w:val": abstract_id})
    return num_id
