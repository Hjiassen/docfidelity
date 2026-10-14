"""只消费冻结变量锚点，并保留段落及字符直接格式。"""

from collections.abc import Mapping, Sequence

from docfidelity.contracts.errors import EngineError, ErrorCode
from docfidelity.contracts.imports import FrozenVariable
from docfidelity.ooxml.doc import Element, find_first, qn, set_text, sub, xpath
from docfidelity.ooxml.package import DocxPackage


def marked_control(root: Element, marker: str) -> Element:
    """按冻结标记精确定位唯一内容控件。"""
    matches = [
        control
        for control in root.iter(qn("w:sdt"))
        if any(tag.get(qn("w:val")) == marker for tag in xpath(control, "./w:sdtPr/w:tag"))
    ]
    if len(matches) != 1:
        raise EngineError(ErrorCode.PACKAGE_INCOMPLETE, "冻结锚点缺失或重复")
    return matches[0]


def fill_paragraph(paragraph: Element, value: str) -> None:
    """填充空段落，沿用首个运行的属性，换行和制表符写为原生元素。"""
    if paragraph.tag != qn("w:p"):
        raise EngineError(ErrorCode.PACKAGE_INCOMPLETE, "冻结文字原型不是段落")
    runs = xpath(paragraph, "./w:r")
    if not runs:
        runs = [sub(paragraph, "w:r")]
    for child in list(paragraph):
        if child.tag not in {qn("w:pPr"), qn("w:r")}:
            paragraph.remove(child)
    for run in runs:
        for child in list(run):
            if child.tag != qn("w:rPr"):
                run.remove(child)
    run = runs[0]
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    buffer: list[str] = []
    for character in value:
        if character in {"\n", "\t"}:
            if buffer:
                set_text(sub(run, "w:t"), "".join(buffer))
                buffer.clear()
            sub(run, "w:br" if character == "\n" else "w:tab")
        else:
            buffer.append(character)
    if buffer or not len(run):
        set_text(sub(run, "w:t"), "".join(buffer))


def fill_variables(
    package: DocxPackage, bindings: Sequence[FrozenVariable], values: Mapping[str, str]
) -> None:
    """校验全部变量并按已冻结位置填充，不识别旧文字。"""
    keys = [binding.key for binding in bindings]
    if len(set(keys)) != len(keys) or set(values) - set(keys):
        raise EngineError(ErrorCode.SCHEMA_INVALID, "变量键重复或包含未声明变量")
    for binding in bindings:
        if binding.required and binding.key not in values:
            raise EngineError(ErrorCode.SCHEMA_INVALID, f"缺少必需变量：{binding.key}")
        value = values.get(binding.key, "")
        if not isinstance(value, str):
            raise EngineError(ErrorCode.SCHEMA_INVALID, "变量值必须为文字")
        control = marked_control(package.part(binding.part).tree, binding.marker)
        container = find_first(control, "w:sdtContent")
        paragraphs = xpath(container, "./w:p") if container is not None else []
        if len(paragraphs) != 1:
            raise EngineError(ErrorCode.PACKAGE_INCOMPLETE, "冻结变量容器必须含一个完整段落")
        fill_paragraph(paragraphs[0], value)
