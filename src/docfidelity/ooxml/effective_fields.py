"""入库期目录范围与安全指令；不保留缓存或旧书签参数。"""

import re

from docfidelity.contracts.errors import EngineError, ErrorCode

from .doc import Element, deep_copy, new_tree, qn, sub, xpath
from .effective_clean import clean_content


def toc_ranges(root: Element) -> list[tuple[int, int]]:
    """查找简单域和跨完整块的复杂 TOC 域范围。"""
    result: list[tuple[int, int]] = []
    depth = 0
    start = 0
    instruction: list[str] = []
    collecting = False
    for i, block in enumerate(root):
        simple = xpath(block, ".//w:fldSimple")
        if any(
            (field.get(qn("w:instr"), "").strip().split() or [""])[0].upper() == "TOC"
            for field in simple
        ):
            result.append((i, i))
        for node in block.iter():
            if node.tag == qn("w:fldChar"):
                marker = node.get(qn("w:fldCharType"))
                if marker == "begin":
                    if depth == 0:
                        start, instruction, collecting = i, [], True
                    depth += 1
                elif marker == "separate" and depth == 1:
                    collecting = False
                elif marker == "end":
                    depth -= 1
                    if (
                        depth == 0
                        and ("".join(instruction).strip().split() or [""])[0].upper() == "TOC"
                    ):
                        result.append((start, i))
            elif node.tag == qn("w:instrText") and collecting and depth == 1:
                instruction.append(node.text or "")
    return result


def safe_toc_instruction(blocks: list[Element]) -> str:
    """仅允许数字层级和无参数的版式开关，不接受书签或旧文字。"""
    instructions: list[str] = []
    depth = 0
    collecting = False
    parts: list[str] = []
    for block in blocks:
        for node in block.iter():
            if node.tag == qn("w:fldChar"):
                marker = node.get(qn("w:fldCharType"))
                if marker == "begin":
                    if depth == 0:
                        parts, collecting = [], True
                    depth += 1
                elif marker == "separate" and depth == 1:
                    collecting = False
                elif marker == "end":
                    depth -= 1
                    if depth == 0:
                        instructions.append("".join(parts))
                        collecting = False
            elif node.tag == qn("w:instrText") and depth == 1 and collecting:
                parts.append(node.text or "")
            elif node.tag == qn("w:fldSimple") and depth == 0:
                instructions.append(node.get(qn("w:instr"), ""))
    raw = next(
        (
            instruction.strip()
            for instruction in instructions
            if re.match(r"^TOC\b", instruction.strip(), re.IGNORECASE)
        ),
        "",
    )
    tokens = re.findall(r'"[^"\r\n]*"|\S+', raw)
    if not tokens or tokens[0].upper() != "TOC":
        raise EngineError(ErrorCode.SCHEMA_INVALID, "所选目录没有可冻结的完整 TOC 域")
    safe = ["TOC"]
    i = 1
    while i < len(tokens):
        token = tokens[i].lower()
        if token in {"\\h", "\\z", "\\u", "\\w", "\\x"}:
            safe.append(token)
        elif (
            token in {"\\o", "\\n"}
            and i + 1 < len(tokens)
            and re.fullmatch(r'"[1-9]-[1-9]"', tokens[i + 1])
        ):
            safe.extend((token, tokens[i + 1]))
            i += 1
        else:
            raise EngineError(
                ErrorCode.SCHEMA_INVALID, "目录指令含不支持参数或旧内容，请重新插入安全目录域"
            )
        i += 1
    return " ".join(safe)


def freeze_toc(
    blocks: list[Element],
    marker: str,
    instruction: str,
    *,
    markers: dict[str, str],
    images: dict[str, str],
) -> Element:
    """空格式段落保留在同一定位控件；复杂域归一为可更新简单域。"""
    wrapper = new_tree("w:sdt")
    sub(sub(wrapper, "w:sdtPr"), "w:tag", {"w:val": marker})
    content = sub(wrapper, "w:sdtContent")
    for block in blocks:
        source = deep_copy(block)
        original_nodes = {
            original.getroottree().getpath(original): copy
            for original, copy in zip(block.iter(), source.iter(), strict=True)
        }
        for node in xpath(source, ".//w:fldChar | .//w:instrText"):
            parent = node.getparent()
            assert parent is not None
            parent.remove(node)
        for field in xpath(source, ".//w:fldSimple"):
            field.tag = qn("w:sdtContent")
            field.attrib.clear()
        # 深拷贝游离后的绝对路径改变；控件绑定仍以冻结时的源路径映射。
        bound_markers = {
            original_nodes[key].getroottree().getpath(original_nodes[key]): value
            for key, value in markers.items()
            if key in original_nodes
        }
        bound_images = {
            original_nodes[key].getroottree().getpath(original_nodes[key]): value
            for key, value in images.items()
            if key in original_nodes
        }
        cleaned = clean_content(source, markers=bound_markers, images=bound_images)
        if cleaned is not None:
            if cleaned.tag == qn("w:sdtContent"):
                content.extend(list(cleaned))
            else:
                content.append(cleaned)
    paragraphs = xpath(content, ".//w:p")
    if not paragraphs:
        paragraphs = [sub(content, "w:p")]
    first = paragraphs[0]
    index = int(bool(xpath(first, "./w:pPr")))
    first.insert(index, new_tree("w:fldSimple", {"w:instr": instruction, "w:dirty": "true"}))
    return wrapper
