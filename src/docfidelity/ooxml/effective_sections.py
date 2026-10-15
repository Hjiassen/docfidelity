"""按保留内容重连分节，跨已删除节解析页眉页脚继承。"""

from collections.abc import Callable

from docfidelity.contracts.imports import FormatSelection

from .doc import Element, localname, new_tree, qn, xpath
from .effective_clean import clean_format
from .effective_formats import neutral_section


def reconnect_sections(
    root: Element,
    selected: set[int],
    relationships: dict[str, str],
    formats: FormatSelection,
    empty_relationship: Callable[[str], str],
) -> tuple[dict[int, Element], Element]:
    """已排除的完整节不产生段落；保留节承接有效继承引用。"""
    retained: list[tuple[int, Element]] = []
    inherited: dict[tuple[str, str], str | None] = {}
    previous: dict[tuple[str, str], str | None] = {}
    segment_start = 0
    for index, block in enumerate(root):
        originals = [block] if block.tag == qn("w:sectPr") else xpath(block, "./w:pPr/w:sectPr")
        for original in originals:
            explicit: set[tuple[str, str]] = set()
            for reference in original:
                if reference.tag in {qn("w:headerReference"), qn("w:footerReference")}:
                    key = (localname(reference), reference.get(qn("w:type"), "default"))
                    explicit.add(key)
                    mapped = relationships.get(reference.get(qn("r:id"), ""))
                    # None 表示源角色被明确取消，后续源节也继承该取消状态。
                    inherited[key] = mapped
            if any(segment_start <= position <= index for position in selected):
                section = clean_format(original) if formats.page_setup else neutral_section()
                assert section is not None
                if not formats.page_setup:
                    for child in original:
                        if child.tag in {qn("w:titlePg"), qn("w:type")}:
                            cleaned = clean_format(child)
                            if cleaned is not None:
                                section.append(cleaned)
                for key, relation_id in inherited.items():
                    tag, role = key
                    if relation_id is None:
                        if previous.get(key) is None:
                            continue
                        relation_id = empty_relationship(tag.removesuffix("Reference"))
                    if not retained or key in explicit or previous.get(key) != relation_id:
                        section.insert(
                            0, new_tree("w:" + tag, {"w:type": role, "r:id": relation_id})
                        )
                retained.append((index, section))
                previous = dict(inherited)
            segment_start = index + 1
    if not retained:
        return {}, neutral_section()
    final = retained[-1][1]
    return dict(retained[:-1]), final
