"""明确取消格式继承时使用的中性格式与主题白名单。"""

import re
import unicodedata

from docfidelity.contracts.imports import FormatSelection

from .doc import NS, Element, localname, new_tree, qn, sub, xpath


def neutral_section() -> Element:
    """A4、四边一英寸；此基准由格式选择显式启用。"""
    section = new_tree("w:sectPr")
    sub(section, "w:pgSz", {"w:w": "11906", "w:h": "16838"})
    sub(
        section,
        "w:pgMar",
        {
            "w:top": "1440",
            "w:right": "1440",
            "w:bottom": "1440",
            "w:left": "1440",
            "w:header": "720",
            "w:footer": "720",
            "w:gutter": "0",
        },
    )
    return section


def neutral_paragraph(paragraph: Element, *, heading_level: int | None = None) -> None:
    """保留文字位置和域，替换继承及直接格式为明确基准。"""
    properties = paragraph.find(qn("w:pPr"))
    if properties is not None:
        paragraph.remove(properties)
    properties = new_tree("w:pPr")
    paragraph.insert(0, properties)
    sub(
        properties,
        "w:pStyle",
        {
            "w:val": f"DFNeutralHeading{heading_level}"
            if heading_level is not None
            else "DFNeutralBody"
        },
    )
    sub(properties, "w:spacing", {"w:after": "0", "w:line": "240", "w:lineRule": "auto"})
    if heading_level is not None:
        sub(properties, "w:outlineLvl", {"w:val": str(heading_level - 1)})
    for run in xpath(paragraph, "./w:r | ./w:fldSimple/w:r"):
        previous = run.find(qn("w:rPr"))
        if previous is not None:
            run.remove(previous)
        run_properties = new_tree("w:rPr")
        run.insert(0, run_properties)
        sub(run_properties, "w:b", {"w:val": "0"})
        sub(run_properties, "w:i", {"w:val": "0"})
        sub(
            run_properties,
            "w:rFonts",
            {"w:ascii": "Arial", "w:hAnsi": "Arial", "w:eastAsia": "Arial", "w:cs": "Arial"},
        )
        sub(run_properties, "w:sz", {"w:val": "28" if heading_level is not None else "22"})
        sub(run_properties, "w:color", {"w:val": "000000"})


def add_neutral_styles(styles: Element) -> None:
    """独立基准样式不使用源 Normal/basedOn 链。"""
    for level in (None, *range(1, 10)):
        identifier = "DFNeutralBody" if level is None else f"DFNeutralHeading{level}"
        for existing in xpath(styles, f'./w:style[@w:styleId="{identifier}"]'):
            styles.remove(existing)
        style = sub(styles, "w:style", {"w:type": "paragraph", "w:styleId": identifier})
        sub(style, "w:name", {"w:val": identifier})
        paragraph = new_tree("w:p")
        sub(sub(paragraph, "w:r"), "w:t")
        neutral_paragraph(paragraph, heading_level=level)
        properties = xpath(paragraph, "./w:pPr")[0]
        for reference in xpath(properties, "./w:pStyle"):
            properties.remove(reference)
        style.append(properties)
        style.append(xpath(paragraph, "./w:r/w:rPr")[0])


def apply_content_formats(
    element: Element, formats: FormatSelection, *, heading_level: int | None = None
) -> None:
    """冻结已清理节点的格式选择；不会查找原件。"""
    for paragraph in element.iter(qn("w:p")):
        if (heading_level is not None and not formats.heading) or (
            heading_level is None and not formats.body
        ):
            neutral_paragraph(paragraph, heading_level=heading_level)
    if not formats.tables:
        for table in element.iter(qn("w:tbl")):
            for child in list(table):
                if child.tag in {qn("w:tblPr"), qn("w:tblGrid")}:
                    table.remove(child)
            properties = new_tree("w:tblPr")
            table.insert(0, properties)
            sub(properties, "w:tblW", {"w:w": "0", "w:type": "auto"})
            for row in xpath(table, "./w:tr"):
                for previous in xpath(row, "./w:trPr | ./w:tblPrEx"):
                    row.remove(previous)
                for cell in xpath(row, "./w:tc"):
                    for previous in xpath(cell, "./w:tcPr"):
                        cell.remove(previous)
                    props = new_tree("w:tcPr")
                    cell.insert(0, props)
                    sub(props, "w:tcW", {"w:w": "0", "w:type": "auto"})
            for paragraph in table.iter(qn("w:p")):
                neutral_paragraph(paragraph)
    if not formats.numbering:
        remove_numbering(element)


def remove_numbering(element: Element) -> None:
    """段落和样式上的编号引用一起取消。"""
    for node in xpath(element, ".//w:numPr"):
        parent = node.getparent()
        assert parent is not None
        parent.remove(node)


def clean_numbering_labels(root: Element) -> int:
    """编号文字只保留序号占位、标点及单个符号项目符号。"""
    changed = 0
    for text in xpath(root, ".//w:lvlText"):
        old = text.get(qn("w:val"), "")
        pieces = re.findall(r"%[1-9]|\w+|.", old)
        kept = "".join(
            p
            for p in pieces
            if re.fullmatch(r"%[1-9]", p)
            or (len(p) == 1 and unicodedata.category(p)[0] in {"P", "S"})
            or (len(old) == 1 and unicodedata.category(p) == "Co")
        )
        if kept != old:
            text.set(qn("w:val"), kept)
            changed += 1
    return changed


_THEME_TAGS = frozenset(
    "theme themeElements clrScheme dk1 lt1 dk2 lt2 accent1 accent2 accent3 accent4 accent5 accent6 hlink folHlink fontScheme majorFont minorFont latin ea cs font fmtScheme fillStyleLst solidFill gradFill gsLst gs srgbClr sysClr schemeClr scrgbClr hslClr prstClr tint shade alpha alphaOff alphaMod hue hueOff hueMod sat satOff satMod lum lumOff lumMod red redOff redMod green greenOff greenMod blue blueOff blueMod lin path fillToRect tileRect lnStyleLst ln prstDash round bevel miter headEnd tailEnd effectStyleLst effectStyle effectLst outerShdw innerShdw glow softEdge reflection blur bgFillStyleLst noFill grpFill objectDefaults spDef lnDef txDef spPr bodyPr lstStyle style lnRef fillRef effectRef fontRef extraClrSchemeLst extraClrScheme clrMap".split()
)
_THEME_ATTRS = frozenset(
    "val lastClr typeface panose pitchFamily charset script ang scaled path l t r b pos rotWithShape w cap cmpd algn lim type len rad blurRad dist dir sx sy kx ky idx marL marR marT marB wrap anchor anchorCtr vert horzOverflow vertOverflow rtlCol numCol spcCol".split()
)


def clean_theme(element: Element) -> Element | None:
    """仅重建主题格式依赖，扩展、名字、附加文本与关系全部丢弃。"""
    if (
        localname(element) not in _THEME_TAGS
        or not isinstance(element.tag, str)
        or not element.tag.startswith("{" + NS["a"] + "}")
    ):
        return None
    result = new_tree("a:" + localname(element))
    if localname(element) in {"theme", "clrScheme", "fontScheme", "fmtScheme"}:
        result.set("name", "DocFidelity")
    for key, value in element.attrib.items():
        if key in _THEME_ATTRS:
            result.set(key, value)
    for child in element:
        cleaned = clean_theme(child)
        if cleaned is not None:
            result.append(cleaned)
    return result
