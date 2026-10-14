"""白名单 XML 重建。文本、域结果、批注及对象从不复制。"""

from collections.abc import Mapping

from .doc import NS, Element, deep_copy, localname, namespace_of, new_tree, qn, sub, xpath

_FORMAT = frozenset(
    """
pPr rPr tblPr tblPrEx trPr tcPr tblGrid gridCol sectPr
pStyle rStyle tblStyle keepNext keepLines pageBreakBefore framePr widowControl
numPr ilvl numId suppressLineNumbers pBdr top left bottom right between bar
shd tabs tab suppressAutoHyphens kinsoku wordWrap overflowPunct topLinePunct
autoSpaceDE autoSpaceDN bidi adjustRightInd snapToGrid spacing ind contextualSpacing
mirrorIndents suppressOverlap jc textDirection textAlignment textboxTightWrap
outlineLvl divId cnfStyle rFonts b bCs i iCs caps smallCaps strike dstrike
outline shadow emboss imprint noProof vanish webHidden color sz szCs highlight
u effect bdr fitText vertAlign rtl cs em lang eastAsianLayout specVanish oMath
tblW tblInd tblBorders insideH insideV tblLayout tblCellMar tblLook tblCaption
tblOverlap tblpPr rowBandSize colBandSize cantSplit trHeight tblHeader tblCellSpacing
gridBefore gridAfter wBefore wAfter hidden tcW gridSpan hMerge vMerge tcBorders
noWrap tcMar tcFitText vAlign hideMark pgSz pgMar paperSrc pgBorders lnNumType
pgNumType cols col formProt vAlign titlePg docGrid type footnotePr endnotePr
numFmt numStart numRestart pos
styles docDefaults rPrDefault pPrDefault style name basedOn next link uiPriority
semiHidden unhideWhenUsed qFormat locked personal personalCompose personalReply
tblStylePr numbering abstractNum nsid multiLevelType tmpl lvl start lvlRestart
pStyle isLgl suff lvlText lvlJc num abstractNumId lvlOverride startOverride
""".split()
) - {"tblCaption", "nsid", "tmpl"}
_ATTRIBUTES = frozenset(
    """
val ascii hAnsi eastAsia cs asciiTheme hAnsiTheme eastAsiaTheme cstheme hint
fontFamily pitch charset themeColor themeTint themeShade fill themeFill themeFillTint
themeFillShade space sz shadow frame offsetFrom display orient code before after
beforeLines afterLines line lineRule beforeAutospacing afterAutospacing firstLine
firstLineChars hanging hangingChars left right leftChars rightChars start end
w h top bottom header footer gutter type num sep equalWidth color count
styleId default customStyle abstractNumId numId ilvl typeface pos leader
firstRow lastRow firstColumn lastColumn noHBand noVBand
""".split()
)
_SAFE_FIELDS = frozenset({"PAGE", "NUMPAGES", "SECTIONPAGES"})
_VML = "urn:schemas-microsoft-com:vml"


def unsupported_content(element: Element) -> bool:
    """格式子树与明确清理的历史元数据以外，未知内容必须拒绝选中。"""
    tag = localname(element)
    if any(localname(parent) in _FORMAT for parent in element.iterancestors()):
        return False
    if namespace_of(element) == _VML:
        return tag not in {
            "shape",
            "rect",
            "roundrect",
            "textbox",
            "group",
            "shapetype",
            "path",
            "stroke",
            "fill",
        }
    if any(localname(parent) == "drawing" for parent in element.iterancestors()):
        return False
    return tag not in _FORMAT | {
        "body",
        "hdr",
        "ftr",
        "document",
        "p",
        "r",
        "tbl",
        "tr",
        "tc",
        "txbxContent",
        "pict",
        "drawing",
        "sdt",
        "sdtPr",
        "sdtEndPr",
        "sdtContent",
        "hyperlink",
        "ins",
        "del",
        "smartTag",
        "AlternateContent",
        "Choice",
        "Fallback",
        "t",
        "delText",
        "br",
        "tab",
        "cr",
        "fldChar",
        "instrText",
        "fldSimple",
        "bookmarkStart",
        "bookmarkEnd",
        "commentRangeStart",
        "commentRangeEnd",
        "commentReference",
        "proofErr",
        "lastRenderedPageBreak",
        "tag",
        "alias",
        "id",
        "lock",
        "placeholder",
        "showingPlcHdr",
        "docPart",
        "text",
        "dataBinding",
    }


def clean_format(element: Element) -> Element | None:
    """保留已知格式节点和属性，丢弃历史及未知负载。"""
    if localname(element) not in _FORMAT or namespace_of(element) != NS["w"]:
        return None
    result = new_tree("w:" + localname(element))
    for key, value in element.attrib.items():
        if key.rpartition("}")[2] in _ATTRIBUTES:
            result.set(key, value)
    for child in element:
        cleaned = clean_format(child)
        if cleaned is not None:
            result.append(cleaned)
    return result


def mark_container(paragraph: Element, marker: str) -> Element:
    """内容控件是冻结定位标记；其内段落保留全部 run 格式。"""
    wrapper = new_tree("w:sdt")
    sub(sub(wrapper, "w:sdtPr"), "w:tag", {"w:val": marker})
    sub(wrapper, "w:sdtContent").append(paragraph)
    return wrapper


def clean_content(
    element: Element,
    *,
    markers: Mapping[str, str],
    images: Mapping[str, str],
) -> Element | None:
    """递归重建受支持文字容器；图片必须通过独立确认映射。"""
    tag = localname(element)
    position = element.getroottree().getpath(element)
    if tag in _FORMAT:
        return clean_format(element)
    if tag == "drawing":
        replacement = images.get(position)
        if replacement is None:
            return None
        result = deep_copy(element)
        for descendant in result.iter():
            descendant.text = None
            descendant.tail = None
            for key in list(descendant.attrib):
                if key in {qn("r:link"), "name", "descr", "title"}:
                    del descendant.attrib[key]
            if descendant.tag == qn("a:blip"):
                descendant.set(qn("r:embed"), replacement)
        return result
    if tag == "fldSimple":
        instruction = element.get(qn("w:instr"), "").strip().upper()
        if instruction not in _SAFE_FIELDS:
            return None
        return new_tree("w:fldSimple", {"w:instr": instruction})
    if tag in {"p", "r", "tbl", "tr", "tc", "txbxContent", "pict"}:
        result = new_tree("w:" + tag)
    elif element.tag in {
        f"{{{_VML}}}{name}"
        for name in (
            "shape",
            "rect",
            "roundrect",
            "textbox",
            "group",
            "shapetype",
            "path",
            "stroke",
            "fill",
        )
    }:
        result = deep_copy(element)
        for child in list(result):
            result.remove(child)
        result.attrib.clear()
        for key in (
            "style",
            "type",
            "coordsize",
            "path",
            "fillcolor",
            "strokecolor",
            "strokeweight",
        ):
            if key in element.attrib:
                result.set(key, element.attrib[key])
        result.text = None
        result.tail = None
    elif tag in {
        "sdt",
        "sdtContent",
        "hyperlink",
        "ins",
        "smartTag",
        "AlternateContent",
        "Choice",
        "Fallback",
    }:
        # 包装节点只作为容器，字段元数据与外链不进入结果。
        result = new_tree("w:sdtContent")
    elif tag == "t":
        return new_tree("w:t")
    elif tag in {"br", "tab", "cr"}:
        return new_tree("w:" + tag)
    else:
        return None
    field_depth = 0
    instruction_parts: list[str] = []
    for child in element:
        if tag == "p":
            field_chars = xpath(child, ".//w:fldChar")
            begins = sum(c.get(qn("w:fldCharType")) == "begin" for c in field_chars)
            ends = sum(c.get(qn("w:fldCharType")) == "end" for c in field_chars)
            if begins or field_depth:
                field_depth += begins
                instruction_parts.extend(e.text or "" for e in xpath(child, ".//w:instrText"))
                field_depth -= ends
                if field_depth == 0:
                    instruction = "".join(instruction_parts).strip().upper()
                    if instruction in _SAFE_FIELDS:
                        result.append(new_tree("w:fldSimple", {"w:instr": instruction}))
                    instruction_parts = []
                continue
        cleaned = clean_content(child, markers=markers, images=images)
        if cleaned is not None:
            if cleaned.tag == qn("w:sdtContent"):
                result.extend(list(cleaned))
            else:
                result.append(cleaned)
    if result.tag == qn("w:r") and not any(c.tag != qn("w:rPr") for c in result):
        sub(result, "w:t")
    if result.tag == qn("w:p") and not any(c.tag in {qn("w:r"), qn("w:sdt")} for c in result):
        sub(sub(result, "w:r"), "w:t")
    marker = markers.get(position)
    return mark_container(result, marker) if marker is not None else result
