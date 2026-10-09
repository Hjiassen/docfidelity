"""模板冗余裁剪（DD-9.1 第 3 步 / FR-M2-10）。

模板里会带上"不该进产物"的内容：参考资料占位章、示例表格、演示用的附录。
`removal_rules` 声明要裁掉哪些，本模块负责把它们**连同其下属内容整块**裁掉。

"完整块"这三个字是本模块的全部难点
--------------------------------
DD-1.2 第 7 条（硬规则 7）：**裁剪必须定位到「标题 + 段落 + 表格」完整块**。
只删标题会留下一段无主的正文，只删正文会留下空章节——两种产物在 Word 里都
"看起来正常"，只有评审时才会发现少了半章。故本模块的判据是：

    从命中段落到「下一个同级或更高级标题」之前的**全部块级元素**，一并删除。

区间到此为止的两个边界同样重要：

* 遇到同级 / 更高级标题 → 停（那是下一章）；
* 走到**示例内容区之外** → 停。区域之外是前置版块（封面 / 修订记录 / 目录 /
  页眉）与尾节属性，裁掉它们等于把文件编号、修订记录或节属性一起删了。

裁剪的作用域**就是**示例内容区（不是"除内容区之外"）："演示章 / 示例表 /
演示用的附录"全在那里。把它排除在外，规则能匹配到的就只剩受保护版块，
``removal_rules`` 会永远不命中——而"规则没报错"最容易被读成"模板很干净"。

规则从哪来（契约缺口）
----------------------
DD-7.3 把 `removal_rules` 放在模板包的 `overrides` 里，而 overrides 目前**没有
契约模型**（`ProjectConfig` 只有 `overrides_ref` 字段，指向一个尚未建模的文件）。
故本模块自带 :class:`RemovalRule` 并把它作为渲染参数传入。等 overrides 契约落地时
只需把那个模型映射成这里的数据类——判定逻辑一行都不用改。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final

from docfidelity.ooxml import Element, attr, find_all, find_first, localname, paragraph_text

from .context import (
    REMOVAL_RULE_ID,
    RenderContext,
    paragraph_style_id,
)
from .order import is_paragraph

__all__ = [
    "MATCH_HEADING_TEXT",
    "MATCH_STYLE_ID",
    "RemovalReport",
    "RemovalRule",
    "apply_removals",
]

#: 规则支持的匹配键（白名单语义，不是表达式——与 ``ProjectionSpec.filter`` 同风格）。
MATCH_HEADING_TEXT: Final = "heading_text"
MATCH_STYLE_ID: Final = "style_id"

#: 可裁剪的块级元素（段落 / 表格）。其余（``w:sectPr`` 等）永不参与裁剪。
_BLOCK_LOCALS: Final[tuple[str, ...]] = ("p", "tbl")


@dataclass(frozen=True)
class RemovalRule:
    """一条裁剪规则。

    ``match`` 是"字段名 -> 取值"的白名单，语义为**全部条件同时满足**。
    支持 ``heading_text``（段落可见文本，完全相等）与 ``style_id``（段落样式 ID）。
    用白名单而不是表达式：表达式要能对模板作者开放就等于开放了一个求值器，
    而它的错误形态是"静默不匹配"——规则看起来生效了，实际一条没删。
    """

    rule_id: str
    match: Mapping[str, str]
    scope: str = "full_block"


@dataclass
class RemovalReport:
    """裁剪结果。"""

    removed_elements: int = 0
    removed_blocks: int = 0
    matched_rules: list[str] = field(default_factory=list)
    unmatched_rules: list[str] = field(default_factory=list)

    def as_payload(self) -> dict[str, object]:
        return {
            "removed_elements": self.removed_elements,
            "removed_blocks": self.removed_blocks,
            "matched_rules": list(self.matched_rules),
            "unmatched_rules": list(self.unmatched_rules),
        }


def apply_removals(ctx: RenderContext, rules: Sequence[RemovalRule]) -> RemovalReport:
    """逐条应用裁剪规则。"""
    report = RemovalReport()
    if not rules:
        return report

    for rule in rules:
        start = _find_start(ctx, rule)
        if start is None:
            report.unmatched_rules.append(rule.rule_id)
            ctx.note_once(
                REMOVAL_RULE_ID,
                "minor",
                f"removal-unmatched:{rule.rule_id}",
                f"裁剪规则 {rule.rule_id} 在模板里没有命中任何区块",
                evidence=f"match={dict(rule.match)}",
                suggestion="核对规则的匹配条件，或从裁剪规则中移除该条",
            )
            continue
        span = _span_of(ctx, start, rule)
        body = ctx.body
        children = list(body)
        for element in children[span[0] : span[1]]:
            body.remove(element)
        removed = span[1] - span[0]
        report.removed_blocks += 1
        report.removed_elements += removed
        report.matched_rules.append(rule.rule_id)
        # 命中不记 Finding：这是一次**按预期完成**的操作，不是待处理事项。
        # "做了什么"进 report（落 checkpoint，可审计）；Finding 只留给需要人看的。
        # 把成功动作也记成 Finding，会让真正需要关注的条目淹没在审计流里。
    return report


# ------------------------------------------------------------------- 内部
def _skippable(ctx: RenderContext) -> set[int]:
    """不可参与裁剪的 body 子元素下标。

    裁剪的作用域**就是模板的示例内容区**——"演示章 / 示例表 / 演示用的附录"全在
    那里。区域之外一律不参与：

    * 前置版块（封面 / 修订记录 / 目录 / 页眉）是体例的一部分，裁掉它等于把
      文件编号与修订记录一起删了；
    * 尾节属性（``w:sectPr``）是节属性，裁掉它产物当场变形。

    区域**起点之前**同样不动，这让 ``ctx.region[0]``（注入的插入点）在整个裁剪
    过程中始终有效——裁剪只能在区间内部增删，插入点不会被挪走。

    反过来说：把示例内容区**排除**在裁剪之外是错的。那样 ``removal_rules`` 能
    匹配到的只剩受保护版块，规则永远不命中，而"规则没报错"会被读成"模板很干净"。
    """
    children = list(ctx.body)
    start, end = ctx.region
    return {index for index in range(len(children)) if not (start <= index < end)}


def _find_start(ctx: RenderContext, rule: RemovalRule) -> int | None:
    skip = _skippable(ctx)
    for index, child in enumerate(ctx.body):
        if index in skip or not is_paragraph(child):
            continue
        if _matches(child, rule):
            return index
    return None


def _matches(paragraph: Element, rule: RemovalRule) -> bool:
    if not rule.match:
        return False
    for key, value in rule.match.items():
        if key == MATCH_HEADING_TEXT:
            if paragraph_text(paragraph).strip() != value.strip():
                return False
        elif key == MATCH_STYLE_ID:
            if paragraph_style_id(paragraph) != value:
                return False
        else:
            return False
    return True


def _span_of(ctx: RenderContext, start: int, rule: RemovalRule) -> tuple[int, int]:
    """裁剪区间 ``[start, end)``：命中的块 + 其后到下一个同级标题之前的全部块。"""
    if rule.scope != "full_block":
        return start, start + 1
    del rule
    children = list(ctx.body)
    level_by_style = {style: level for level, style in ctx.level_styles.items()}
    own_level = _level_of(children[start], level_by_style)
    skip = _skippable(ctx)

    end = len(children)
    for index in range(start + 1, len(children)):
        if index in skip:
            end = index
            break
        child = children[index]
        if not is_paragraph(child):
            continue
        level = _level_of(child, level_by_style)
        if own_level is None or level is None:
            continue
        if level <= own_level:
            end = index
            break
    return start, end


def _level_of(paragraph: Element, level_by_style: Mapping[str, int]) -> int | None:
    """ "按样式 ID 反查层级；**手写编号的样式（如 Title）算作 0**（=最高级）。

    样式不在骨架里时按 ``None`` 处理（"不知道是哪一级"），而不是猜一个数字：
    猜错的后果是裁剪区间在错误的位置停住，而产物表面看不出任何异常。
    """
    style_id = paragraph_style_id(paragraph)
    if style_id is None:
        return None
    return level_by_style.get(style_id)


def block_count(body: Element) -> int:
    """body 内块级元素数（段落 + 表格，含嵌套），供测试与诊断断言使用。"""
    return sum(1 for child in body.iter() if localname(child) in _BLOCK_LOCALS)


def first_heading_text(body: Element) -> str:
    """body 里第一个段落的文本（诊断用）。"""
    paragraphs = find_all(body, "w:p")
    if not paragraphs:
        return ""
    return paragraph_text(paragraphs[0])


def style_id_of(element: Element) -> str | None:
    """段落样式 ID（诊断用）。"""
    return attr(find_first(find_first(element, "w:pPr"), "w:pStyle"), "w:val")


__all__.extend(["block_count", "first_heading_text", "style_id_of"])
