"""导入期固化源默认格式依赖，让中性内容脱离源 docDefaults。"""

from docfidelity.contracts.errors import EngineError, ErrorCode
from docfidelity.contracts.imports import FormatSelection

from .doc import Element, deep_copy, find_first, localname, new_tree, qn, sub, xpath
from .effective_index import paragraph_numbering

_TOGGLES = frozenset(
    "b bCs i iCs caps smallCaps strike outline shadow emboss imprint vanish".split()
)
_ORDERS = {
    "pPr": "pStyle keepNext keepLines pageBreakBefore framePr widowControl numPr suppressLineNumbers pBdr shd tabs suppressAutoHyphens kinsoku wordWrap overflowPunct topLinePunct autoSpaceDE autoSpaceDN bidi adjustRightInd snapToGrid spacing ind contextualSpacing mirrorIndents suppressOverlap jc textDirection textAlignment textboxTightWrap outlineLvl divId cnfStyle rPr sectPr".split(),
    "rPr": "rStyle rFonts b bCs i iCs caps smallCaps strike dstrike outline shadow emboss imprint noProof snapToGrid vanish webHidden color spacing w kern position sz szCs highlight u effect bdr shd fitText vertAlign rtl cs em lang eastAsianLayout specVanish oMath".split(),
}
_FONT_PAIRS = {
    "ascii": "asciiTheme",
    "hAnsi": "hAnsiTheme",
    "eastAsia": "eastAsiaTheme",
    "cs": "cstheme",
}
_FONT_PAIRS.update({value: key for key, value in tuple(_FONT_PAIRS.items())})


def _insert(parent: Element, child: Element) -> None:
    order = _ORDERS.get(localname(parent), [])
    rank = {name: i for i, name in enumerate(order)}
    position = rank.get(localname(child), len(order))
    for index, sibling in enumerate(parent):
        if rank.get(localname(sibling), len(order)) > position:
            parent.insert(index, child)
            return
    parent.append(child)


def _missing(default: Element, overlays: list[Element]) -> Element | None:
    result = new_tree("w:" + localname(default))
    for key, value in default.attrib.items():
        pair = _FONT_PAIRS.get(key.rpartition("}")[2]) if localname(default) == "rFonts" else None
        if not any(
            key in overlay.attrib or (pair and qn("w:" + pair) in overlay.attrib)
            for overlay in overlays
        ):
            result.set(key, value)
    for child in default:
        inherited = _missing(
            child, [e for overlay in overlays for e in overlay if e.tag == child.tag]
        )
        if inherited is not None:
            result.append(inherited)
    if result.attrib or len(result) or (not default.attrib and not len(default) and not overlays):
        return result
    return None


def _apply_missing(target: Element, defaults: Element, overlays: list[Element]) -> None:
    delta = _missing(defaults, overlays)
    if delta is None:
        return

    def merge(destination: Element, additions: Element) -> None:
        destination.attrib.update(additions.attrib)
        for child in additions:
            current = next((e for e in destination if e.tag == child.tag), None)
            if current is None:
                _insert(destination, deep_copy(child))
            else:
                merge(current, child)

    merge(target, delta)


class SourceDefaults:
    """只在冻结期使用；把默认值固化在源内容或源表格样式的正确优先级。"""

    def __init__(self, styles: Element, formats: FormatSelection) -> None:
        self.resolution_styles = deep_copy(styles)
        self.defaults = new_tree("w:docDefaults")
        for defaults in xpath(styles, "./w:docDefaults"):
            self.defaults.extend(deep_copy(child) for child in defaults)
            styles.remove(defaults)
        if not formats.numbering:
            for pointer in xpath(self.defaults, ".//w:numPr"):
                parent = pointer.getparent()
                assert parent is not None
                parent.remove(pointer)
        self.by_id = {e.get(qn("w:styleId"), ""): e for e in xpath(styles, "./w:style")}
        table_styles = [e for e in self.by_id.values() if e.get(qn("w:type")) == "table"]
        if not formats.body:
            for style in table_styles:
                for properties in xpath(style, ".//w:pPr | .//w:rPr"):
                    parent = properties.getparent()
                    assert parent is not None
                    parent.remove(properties)
        elif table_styles and len(self.defaults):
            identifier = "DFSourceTableDefaults"
            while identifier in self.by_id:
                identifier += "_"
            base = sub(styles, "w:style", {"w:type": "table", "w:styleId": identifier})
            sub(base, "w:name", {"w:val": identifier})
            for kind in ("pPr", "rPr"):
                base.extend(
                    deep_copy(e) for e in xpath(self.defaults, f"./w:{kind}Default/w:{kind}")
                )
            for style in table_styles:
                if not xpath(style, "./w:basedOn"):
                    style.insert(
                        1 if xpath(style, "./w:name") else 0,
                        new_tree("w:basedOn", {"w:val": identifier}),
                    )
        self.default_style = next(
            (
                e.get(qn("w:styleId"), "")
                for e in self.by_id.values()
                if e.get(qn("w:type")) == "paragraph"
                and e.get(qn("w:default")) in {"1", "true", "on"}
            ),
            "",
        )

    def _chain(self, identifier: str) -> list[Element]:
        result = []
        seen: set[str] = set()
        while identifier in self.by_id:
            if identifier in seen:
                raise EngineError(ErrorCode.SCHEMA_INVALID, "所选源样式继承循环")
            seen.add(identifier)
            current = self.by_id[identifier]
            result.append(current)
            base = find_first(current, "w:basedOn")
            identifier = base.get(qn("w:val"), "") if base is not None else ""
        return list(reversed(result))

    def apply(self, element: Element, numbering: Element | None) -> None:
        """源默认值仅填充未被高优先级格式覆盖的属性；中性段落不接收它们。"""
        for paragraph in element.iter(qn("w:p")):
            reference = find_first(find_first(paragraph, "w:pPr"), "w:pStyle")
            identifier = (
                reference.get(qn("w:val"), "") if reference is not None else self.default_style
            )
            if identifier in {"DFNeutralBody", *(f"DFNeutralHeading{i}" for i in range(1, 10))}:
                continue
            if any(True for _ in paragraph.iterancestors(qn("w:tbl"))):
                continue
            chain = self._chain(identifier)
            properties = find_first(paragraph, "w:pPr")
            if properties is None:
                properties = new_tree("w:pPr")
                paragraph.insert(0, properties)
            overlays = [e for style in chain for e in xpath(style, "./w:pPr")]
            pointer = paragraph_numbering(paragraph, self.resolution_styles)
            if pointer is not None and numbering is not None:
                for instance in xpath(
                    numbering, f'./w:num[@w:numId="{pointer[0]}"]/w:abstractNumId'
                ):
                    overlays.extend(
                        xpath(
                            numbering,
                            f'./w:abstractNum[@w:abstractNumId="{instance.get(qn("w:val"))}"]/w:lvl[@w:ilvl="{pointer[1]}"]/w:pPr',
                        )
                    )
                for override in xpath(
                    numbering,
                    f'./w:num[@w:numId="{pointer[0]}"]/w:lvlOverride[@w:ilvl="{pointer[1]}"]/w:lvl/w:pPr',
                ):
                    overlays.append(override)
            for default in xpath(self.defaults, "./w:pPrDefault/w:pPr"):
                _apply_missing(properties, default, [*overlays, properties])
            runs = [
                run
                for run in paragraph.iter(qn("w:r"))
                if next(run.iterancestors(qn("w:p")), None) is paragraph
            ]
            if not runs:
                runs = [sub(paragraph, "w:r")]
            for run in runs:
                self._run(run, chain)

    def _run(self, run: Element, paragraph_chain: list[Element]) -> None:
        properties = find_first(run, "w:rPr")
        if properties is None:
            properties = new_tree("w:rPr")
            run.insert(0, properties)
        character = find_first(properties, "w:rStyle")
        chain = [
            *paragraph_chain,
            *self._chain(character.get(qn("w:val"), "") if character is not None else ""),
        ]
        inherited = [e for style in chain for e in xpath(style, "./w:rPr")]
        direct_tags = {child.tag for child in properties}
        for default in xpath(self.defaults, "./w:rPrDefault/w:rPr"):
            _apply_missing(properties, default, [*inherited, properties])
            # 样式开关是异或，直接格式开关是绝对值；保留默认值参与后的最终状态。
            for toggle in default:
                tag = localname(toggle)
                if tag not in _TOGGLES or toggle.get(qn("w:val"), "1") in {"0", "false", "off"}:
                    continue
                # 原有直接开关已经覆盖默认值，无需固化样式状态。
                if toggle.tag in direct_tags:
                    continue
                direct = find_first(properties, "w:" + tag)
                originals = [e for p in inherited for e in p if e.tag == toggle.tag]
                enabled = True
                for value in originals:
                    if value.get(qn("w:val"), "1") not in {"0", "false", "off"}:
                        enabled = not enabled
                if direct is None:
                    direct = new_tree("w:" + tag)
                    _insert(properties, direct)
                direct.set(qn("w:val"), "1" if enabled else "0")
