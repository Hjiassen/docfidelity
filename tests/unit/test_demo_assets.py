"""示例资产的结构证据：``template.json`` 自称的能力，逐项都要在 docx 里找得到（10.10 / OS-20）。

这道门禁为什么必要
------------------
需求 10.10 把示例资产定为"开源版可用性的前提"，于是 ``template.json.covers`` 变成了一句
**对外承诺**。而"承诺"与"资产实际长什么样"是两处独立维护的东西，天然会漂：

* 生成脚本只给软件类模板放了合并单元格原型，非软件域模板的 ``covers`` 却照抄了同一行
  ——模板里根本没有那张表，而**没有任何一层判定会喊**（``--check`` 只比对"生成结果与
  已提交资产是否一致"，两边都是错的它照样过）；
* 非软件域模板的 ``covers`` 写了"冗余模块（参考资料）"，而它的骨架里没有这一章。

这类缺陷的形状与引擎里的静默降级**完全一样**：报告全绿、只有人眼看得出来。故这里把
``covers`` 的每一行都钉到一个**结构判据**上，并强制两件事：

1. 每个能力项都要有已登记的判据（``CHECKS``）；新加一行 ``covers`` 而不给判据 = 这道
   门禁会被绕过，直接失败。
2. 模板声明的每一项都要真通过——判据取自**引擎自己的契约与实现**（块索引、编号契约、
   原型索引、目录域读取），不是测试另写一套"我认为模板应该长这样"。
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from docfidelity.compose.glossary import glossary_apply
from docfidelity.contracts.config import Terminology
from docfidelity.contracts.template import BlockEntry, TemplatePackage
from docfidelity.ooxml import (
    DOCUMENT_PART,
    DocxPackage,
    Element,
    attr,
    find_first,
    iter_elements,
    level_style_map,
    paragraph_text,
    read_extents,
    toc_field_levels,
)
from docfidelity.registry import read_package
from docfidelity.render.context import (
    PrototypeIndex,
    body_children,
    content_region,
    harvest_prototypes,
    is_paragraph,
    is_table,
    paragraph_style_id,
)

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
DEMO_ROOT = REPO_ROOT / "demo-templates"
EXAMPLES = REPO_ROOT / "examples"

PLACEHOLDER_MARK = "此处填写"


@pytest.mark.parametrize("slug", ["demo-software-design", "demo-operations"])
def test_generated_demo_has_a_named_cover_before_revision_and_toc(
    slug: str, tmp_path: Path
) -> None:
    from scripts.make_demo_templates import TEMPLATES, build_docx

    spec = next(item for item in TEMPLATES if item.slug == slug)
    path = build_docx(spec, tmp_path / "template.docx")
    with DocxPackage.open(path, read_only=True) as pkg:
        body = find_first(pkg.part(DOCUMENT_PART).read_tree, "w:body")
        assert body is not None
        children = list(body)
        assert is_paragraph(children[0])
        title = paragraph_text(children[0])
        assert title and PLACEHOLDER_MARK not in title
        cover_end = next(
            index for index, child in enumerate(children) if list(iter_elements(child, "w:sectPr"))
        )
        revision = next(index for index, child in enumerate(children) if is_table(child))
        toc = next(index for index, child in enumerate(children) if child.tag.endswith("}sdt"))
        first_heading = next(
            index
            for index, child in enumerate(children)
            if is_paragraph(child) and paragraph_style_id(child) == "Heading1"
        )
        assert cover_end < revision < toc < first_heading
        assert any(
            list(iter_elements(child, "w:sectPr")) for child in children[revision + 1 : toc]
        ), "目录的分页边界必须在 TOC 域之外，避免 Word 重建目录时清掉首条分页属性"


# ------------------------------------------------------------------ 证据
@dataclass(frozen=True)
class Evidence:
    """一份示例模板的**可断言事实**：全部在包还开着的时候采完，之后不再持有句柄。"""

    slug: str
    covers: tuple[str, ...]
    content_headings: tuple[tuple[int, str], ...]
    content_tables: tuple[Element, ...]
    merged_tables: int
    cover_blocks: int
    revision_blocks: int
    header_families: frozenset[str]
    outline_styles: tuple[str, ...]
    bound_styles: frozenset[str]
    toc_block: BlockEntry | None
    toc_levels: tuple[int, int] | None
    page_field: bool
    sections: int
    extents: tuple[tuple[int, int], ...]
    placeholders: tuple[str, ...]
    prototypes: PrototypeIndex
    profile_titles: tuple[frozenset[str], ...]

    def heading_titles(self) -> set[str]:
        return {title for _, title in self.content_headings}


def _collect(slug: str, terminology: Terminology) -> Evidence:
    """采集一份模板的结构事实（包在读完之后立刻关闭）。"""
    directory = DEMO_ROOT / slug
    manifest = json.loads((directory / "template.json").read_text(encoding="utf-8"))
    package: TemplatePackage = read_package(directory / "package")
    with DocxPackage.open(directory / "template.docx", read_only=True) as pkg:
        # ``read_tree`` 的根是 ``w:document``，段落与表格挂在 ``w:body`` 下——直接把它当
        # body 用会得到"内容区为空、原型全 None"这种**看起来像资产缺失**的结论。
        body = find_first(pkg.part(DOCUMENT_PART).read_tree, "w:body")
        assert body is not None, f"{slug}：包内没有 w:body"
        styles = level_style_map(package.outline)
        start, end = content_region(body, styles)
        content = list(body_children(body))[start:end]
        parts = set(pkg.part_names())

        toc_block = next(iter(package.blocks.by_kind("toc")), None)
        return Evidence(
            slug=slug,
            covers=tuple(manifest.get("covers") or ()),
            content_headings=_headings(content, styles),
            content_tables=tuple(item for item in content if is_table(item)),
            merged_tables=sum(1 for item in content if is_table(item) and _is_merged(item)),
            cover_blocks=len(package.blocks.by_kind("cover")),
            revision_blocks=len(package.blocks.by_kind("revision")),
            header_families=frozenset(
                str(block.structure.get("family", "")) for block in package.blocks.by_kind("header")
            ),
            outline_styles=tuple(sorted(set(styles.values()))),
            bound_styles=frozenset(
                binding.style_id for binding in package.numbering.heading_bindings
            ),
            toc_block=toc_block,
            toc_levels=None if toc_block is None else toc_field_levels(pkg, toc_block),
            page_field=_has_field(pkg, parts, "word/footer", "PAGE"),
            sections=len(list(iter_elements(body, "w:sectPr"))),
            extents=tuple((extent.cx, extent.cy) for extent in read_extents(pkg)),
            placeholders=tuple(entry.text for entry in package.placeholders.entries),
            prototypes=harvest_prototypes(body, styles, package.numbering, region=(start, end)),
            profile_titles=(_profile_titles(slug, terminology, manifest.get("template_id", slug)),),
        )


def _headings(content: list[Element], styles: dict[int, str]) -> tuple[tuple[int, str], ...]:
    """内容区里按骨架标题样式排版的段落，``(层级, 文本)``。"""
    by_style = {style: level for level, style in styles.items()}
    found: list[tuple[int, str]] = []
    for item in content:
        if not is_paragraph(item):
            continue
        level = by_style.get(paragraph_style_id(item))
        if level is not None:
            found.append((level, paragraph_text(item)))
    return tuple(found)


def _is_merged(table: Element) -> bool:
    """表里有没有合并单元格（``w:gridSpan`` 跨列 / ``w:vMerge`` 跨行）。"""
    return any(iter_elements(table, tag) for tag in ("w:gridSpan", "w:vMerge"))


def _has_field(pkg: DocxPackage, parts: set[str], prefix: str, instruction: str) -> bool:
    """页眉 / 页脚部件里有没有某个域（如 ``PAGE``）。"""
    for name in sorted(item for item in parts if item.startswith(prefix)):
        part = pkg.find_part(name)
        if part is None:
            continue
        for element in iter_elements(part.read_tree, "w:fldSimple"):
            if instruction in (attr(element, "w:instr") or ""):
                return True
    return False


def _profile_titles(slug: str, terminology: Terminology, template_id: str) -> set[str]:
    """该模板名下所有档案声明的章节标题（术语占位符按引擎的口径解析后）。"""
    project = json.loads((EXAMPLES / "project.json").read_text(encoding="utf-8"))
    titles: set[str] = set()
    for entry in project["documents"]:
        if str(entry["template_ref"]).split("@", 1)[0] != template_id:
            continue
        profile = json.loads(
            (EXAMPLES / "profiles" / f"{entry['profile_id']}.json").read_text(encoding="utf-8")
        )
        for item in profile["genre"]["chapter_skeleton"]:
            titles.add(glossary_apply(str(item["title"]), terminology))
    return titles


@pytest.fixture(scope="module")
def templates() -> Iterator[list[Evidence]]:
    terminology = Terminology.model_validate(
        json.loads((EXAMPLES / "project.json").read_text(encoding="utf-8"))["terminology"]
    )
    slugs = sorted(path.name for path in DEMO_ROOT.iterdir() if (path / "template.json").is_file())
    assert slugs, f"示例模板资产缺失：{DEMO_ROOT}"
    yield [_collect(slug, terminology) for slug in slugs]


# ------------------------------------------------------------------ 判据
#: 能力项 → 结构判据；返回 ``None`` 表示满足，否则返回**缺了什么**。
#:
#: 键必须与 ``scripts/make_demo_templates.py`` 里 ``covers`` 的原文逐字一致——这是把
#: 那句承诺钉到结构上的唯一连接点。
CHECKS: dict[str, Callable[[Evidence], str | None]] = {
    "多级标题 + 自动编号": lambda item: _check_numbered_headings(item),
    "封面 + 修订记录 + 页眉页脚": lambda item: _check_front_matter(item),
    "多种表格原型（含合并单元格、跨行跨列）": lambda item: _check_table_prototypes(item),
    "目录域 + 页码 + 分节": lambda item: _check_toc_and_pagination(item),
    "长图与宽图槽位": lambda item: _check_figure_slots(item),
    "带编写说明的占位内容": lambda item: _check_placeholder_notes(item),
    "列表段落原型（项目符号）": lambda item: _check_bullet_prototype(item),
    "冗余模块（参考资料）": lambda item: _check_redundant_module(item),
}


def _check_numbered_headings(item: Evidence) -> str | None:
    levels = {level for level, _ in item.content_headings}
    if len(levels) < 2:
        return f"内容区只有 {len(levels)} 个标题层级"
    unbound = sorted(set(item.outline_styles) - item.bound_styles)
    if unbound:
        return f"这些标题样式没有绑定自动编号：{unbound}"
    return None


def _check_front_matter(item: Evidence) -> str | None:
    if not item.cover_blocks:
        return "块索引里没有封面块"
    if not item.revision_blocks:
        return "块索引里没有修订记录块"
    missing = sorted({"header", "footer"} - item.header_families)
    if missing:
        return f"块索引里没有页眉 / 页脚：{missing}"
    return None


def _check_table_prototypes(item: Evidence) -> str | None:
    if len(item.content_tables) < 2:
        return f"内容区只有 {len(item.content_tables)} 张表，谈不上「多种表格原型」"
    if not item.merged_tables:
        return "内容区没有含合并单元格 / 跨行跨列的表"
    return None


def _check_toc_and_pagination(item: Evidence) -> str | None:
    if item.toc_block is None:
        return "块索引里没有目录块"
    if item.toc_levels is None:
        return "目录块里读不到 TOC 域代码（只有条目、没有域，页码不会更新）"
    if not item.page_field:
        return "页脚里没有 PAGE 域：页码是写死的文本，改了页数不会变"
    if item.sections < 2:
        return f"只有 {item.sections} 个分节，体现不了分节"
    return None


def _check_figure_slots(item: Evidence) -> str | None:
    if len(item.extents) < 2:
        return f"只有 {len(item.extents)} 个图片槽位"
    if not any(cx < cy for cx, cy in item.extents):
        return "没有长图槽位（高 > 宽）"
    if not any(cx > cy for cx, cy in item.extents):
        return "没有宽图槽位（宽 > 高）"
    return None


def _check_placeholder_notes(item: Evidence) -> str | None:
    if not item.placeholders:
        return "模板包里没有登记任何占位符"
    if not any(PLACEHOLDER_MARK in text for text in item.placeholders):
        return f"登记的占位符里没有一处带编写说明（{PLACEHOLDER_MARK}…）"
    return None


def _check_bullet_prototype(item: Evidence) -> str | None:
    if item.prototypes.bullets is None:
        return "内容区取不到项目符号原型：注入时会静默回落到正文原型（符号没了，报告全绿）"
    return None


def _check_redundant_module(item: Evidence) -> str | None:
    """内容区里至少有一章，在**某一份**档案的骨架里没被用到。

    这才是"冗余模块 / 模板裁剪能力"的可验证形式：模板带了这一章，而某个体例不要它，
    于是它必须被裁掉。若每一章都被每一份档案填满，模板裁剪这条能力在演示里一次都没
    被执行过——``covers`` 那句话就是空头承诺。
    """
    if not item.profile_titles:
        return "找不到该模板名下的档案，无法判断哪一章构成冗余模块"
    for titles in item.profile_titles:
        extra = sorted(
            title
            for title in item.heading_titles()
            if not any(title in declared for declared in titles)
        )
        if extra:
            return None
    return "该模板的每一章都被所有档案用到了：没有可裁剪的冗余模块"


# ------------------------------------------------------------------ 断言
def test_every_declared_cover_has_a_structural_check(templates: list[Evidence]) -> None:
    """新加一行 ``covers`` 而不登记判据 = 这道门禁会被绕过，直接失败。"""
    declared = sorted({item for asset in templates for item in asset.covers})
    unknown = [item for item in declared if item not in CHECKS]
    assert not unknown, f"这些能力项没有结构判据，请先在 CHECKS 里登记：{unknown}"


def test_every_cover_item_holds_in_the_template(templates: list[Evidence]) -> None:
    failures: list[str] = []
    for asset in templates:
        for cover in asset.covers:
            problem = CHECKS[cover](asset)
            if problem is not None:
                failures.append(f"{asset.slug} / {cover}：{problem}")
    assert not failures, "示例模板自称的能力在 docx 里找不到结构证据：\n" + "\n".join(failures)


def test_figure_slot_claim_discriminates(templates: list[Evidence]) -> None:
    """非软件域模板不声明图槽位是**有意的**，不是漏项——判据必须两边都出现。

    没有这条对立断言，"图槽位"这一项可以被任何一份模板蒙过去：全部模板都不声明，
    上一条用例就一次也不检查它；全部模板都声明，则判定失去区分度。
    """
    declared = {asset.slug: "长图与宽图槽位" in asset.covers for asset in templates}
    assert any(declared.values()), f"没有任何模板声明图槽位：{declared}"
    assert not all(declared.values()), f"所有模板都声明了图槽位，判定没有区分度：{declared}"


def test_templates_disagree_on_layout(templates: list[Evidence]) -> None:
    """不同模板的结构形态必须真的不同，否则 10.10 的「≥3 类结构不同的文档」无从谈起。"""
    shapes = {
        asset.slug: (tuple(level for level, _ in asset.content_headings), len(asset.content_tables))
        for asset in templates
    }
    assert len(set(shapes.values())) == len(shapes), f"两份模板的结构形态一样：{shapes}"
