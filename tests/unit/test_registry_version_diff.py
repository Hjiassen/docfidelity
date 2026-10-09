"""模板版本 diff 单测（DD-7.4，验收 14）。

验收 14 的原文是：同一 ``template_id`` 的两个版本入库后，能输出**样式层 /
结构 / 表原型 / 页面设置**的差异清单与影响面。所以这里既断言"哪几个分区变了"
（不能多报也不能少报），也断言变更**归到哪一类**、以及**什么情况算阻断**。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from docfidelity.contracts.template import (
    BlockIndex,
    ChapterNode,
    OutlineSkeleton,
    SlotSpec,
    StyleContract,
    TemplatePackage,
)
from docfidelity.registry import (
    CATEGORY_PAGE_SETUP,
    CATEGORY_STRUCTURE,
    CATEGORY_STYLE_LAYER,
    CATEGORY_TABLE_PROTOTYPE,
    compute_fingerprint,
    diff_fingerprints,
    diff_packages,
    flatten,
    ingest_template,
)
from docfidelity.registry.fingerprint import section_payloads

pytestmark = pytest.mark.unit


def _ingest(source: Path, version: str = "1.0") -> TemplatePackage:
    result = ingest_template(source, template_id="TPL-1", version=version)
    assert result.ok
    assert result.package is not None
    return result.package


def _restamp(package: TemplatePackage, version: str) -> TemplatePackage:
    """改版本号：diff 的两个操作数必须是同一模板的两个版本。"""
    manifest = package.manifest.model_copy(update={"version": version})
    return package.model_copy(update={"manifest": manifest})


def _with_margin(package: TemplatePackage, left_twips: int) -> TemplatePackage:
    margins = package.page_setup.margins.model_copy(update={"left_twips": left_twips})
    page_setup = package.page_setup.model_copy(update={"margins": margins})
    return package.model_copy(update={"page_setup": page_setup})


def _with_style_size(package: TemplatePackage, style_id: str, size_pt: float) -> TemplatePackage:
    styles = [
        style.model_copy(update={"size_pt": size_pt}) if style.style_id == style_id else style
        for style in package.styles
    ]
    return package.model_copy(update={"styles": styles})


def _with_table_shading(package: TemplatePackage, style_id: str, fill: str) -> TemplatePackage:
    styles = []
    for style in package.styles:
        if style.style_id != style_id:
            styles.append(style)
            continue
        conditions = [
            item.model_copy(update={"shading_fill": fill}) if item.type == "firstRow" else item
            for item in style.tbl_style_pr
        ]
        styles.append(style.model_copy(update={"tbl_style_pr": conditions}))
    return package.model_copy(update={"styles": styles})


def _insert_style(package: TemplatePackage, style_id: str) -> TemplatePackage:
    """在样式列表**最前面**插一条新样式。

    这是"按索引比"的照妖镜：按索引比会把后面每一条样式都报成"变了"，
    于是真实的一处新增被埋在一百多条噪音里。
    """
    new_style = StyleContract(name=style_id, style_id=style_id, type="paragraph")
    return package.model_copy(update={"styles": [new_style, *package.styles]})


def _drop_block(package: TemplatePackage, block_id: str) -> TemplatePackage:
    blocks = [block for block in package.blocks.blocks if block.block_id != block_id]
    return package.model_copy(update={"blocks": BlockIndex(blocks=blocks)})


def _drop_slot(package: TemplatePackage, slot_id: str) -> TemplatePackage:
    """删掉一个槽位（扁平索引与骨架树一起删，跟解析器产出的形态一致）。"""

    def prune(nodes: list[ChapterNode]) -> list[ChapterNode]:
        return [
            node.model_copy(
                update={
                    "slots": [slot for slot in node.slots if slot.slot_id != slot_id],
                    "children": prune(node.children),
                }
            )
            for node in nodes
        ]

    outline: OutlineSkeleton = package.outline
    updated = outline.model_copy(
        update={
            "slots": [slot for slot in outline.slots if slot.slot_id != slot_id],
            "chapter_tree": prune(outline.chapter_tree),
        }
    )
    return package.model_copy(update={"outline": updated})


def _fingerprinted(package: TemplatePackage) -> TemplatePackage:
    return package.model_copy(update={"fingerprint": compute_fingerprint(package)})


class TestNoFalsePositives:
    def test_the_same_package_has_no_diff(self, template_docx: Path) -> None:
        package = _ingest(template_docx)
        diff = diff_packages(package, _restamp(package, "2.0"))
        assert diff.identical
        assert diff.ok
        assert diff.changelog() == ()
        assert "无差异" in diff.summary()

    def test_reordering_a_keyed_section_is_not_a_change(self, template_docx: Path) -> None:
        """带 id 的列表**顺序不是内容**。

        ``styles.xml`` 里的样式先后没有语义（引用走 styleId），把它当变更会让
        "Word 重排了一下样式表"报成"全部样式都改了"。
        """
        package = _ingest(template_docx)
        reordered = package.model_copy(update={"styles": list(reversed(package.styles))})
        assert diff_packages(package, reordered).identical

    def test_reordering_an_unkeyed_list_is_a_change(self, template_docx: Path) -> None:
        """反向断言：没有 id 可依的列表，顺序**就是**内容。

        章节树倒过来是真实的骨架变更，必须报出来——否则"调换了章节顺序"会静默通过。
        """
        package = _ingest(template_docx)
        assert len(package.outline.chapter_tree) >= 2, "夹具的骨架至少要有两章才能谈顺序"
        reordered = package.model_copy(
            update={
                "outline": package.outline.model_copy(
                    update={"chapter_tree": list(reversed(package.outline.chapter_tree))}
                )
            }
        )
        diff = diff_packages(package, reordered)
        assert diff.changed_sections == ("outline",)

    def test_one_field_change_does_not_light_up_every_section(self, template_docx: Path) -> None:
        """改一处只报一处。满屏假差异比没有 diff 更危险。"""
        package = _ingest(template_docx)
        changed = _with_margin(package, 2000)
        diff = diff_packages(package, changed)
        assert diff.changed_sections == ("page_setup",)


class TestCategories:
    def test_the_four_categories_from_acceptance_14(
        self, template_docx: Path, sample_docx: Path
    ) -> None:
        """样式层 / 结构 / 表原型 / 页面设置 四类必须都能出现（验收 14 原文）。"""
        base = _ingest(sample_docx, "1.0")
        changed = _with_margin(base, 2000)
        changed = _with_style_size(changed, "Heading1", 18.0)
        changed = _with_table_shading(changed, "TableGrid", "FF0000")
        changed = _drop_block(changed, "toc-1")
        categories = {
            change.category
            for delta in diff_packages(base, changed).deltas
            for change in delta.changes
        }
        assert categories == {
            CATEGORY_STYLE_LAYER,
            CATEGORY_STRUCTURE,
            CATEGORY_TABLE_PROTOTYPE,
            CATEGORY_PAGE_SETUP,
        }

    def test_conditional_format_is_classified_as_table_prototype(self, sample_docx: Path) -> None:
        base = _ingest(sample_docx, "1.0")
        changed = _with_table_shading(base, "TableGrid", "FF0000")
        diff = diff_packages(base, changed)
        categories = {change.category for delta in diff.deltas for change in delta.changes}
        assert categories == {CATEGORY_TABLE_PROTOTYPE}
        assert diff.changed_sections == ("styles",)

    def test_numbering_counts_as_the_style_layer(self, template_docx: Path) -> None:
        base = _ingest(template_docx, "1.0")
        numbering = base.numbering.model_copy(update={"num": []})
        changed = base.model_copy(update={"numbering": numbering})
        diff = diff_packages(base, changed)
        assert diff.changed_sections == ("numbering",)
        assert {change.category for delta in diff.deltas for change in delta.changes} == {
            CATEGORY_STYLE_LAYER
        }


class TestKeyedMatching:
    def test_inserting_a_style_touches_only_the_new_style(self, template_docx: Path) -> None:
        """按 id 匹配：插入一条只影响这一条，不是"它后面全变了"。

        按索引比的话，在最前面插一条会把后面一百多条全报成"变了"，
        真正的那一处新增就此埋掉。
        """
        base = _ingest(template_docx, "1.0")
        diff = diff_packages(base, _insert_style(base, "BrandNewStyle"))
        styles_delta = next(delta for delta in diff.deltas if delta.section == "styles")
        assert styles_delta.changes
        assert {change.kind for change in styles_delta.changes} == {"added"}
        assert {change.path.split("]", 1)[0] + "]" for change in styles_delta.changes} == {
            "styles[style_id=BrandNewStyle]"
        }

    def test_changing_one_style_reports_only_that_style(self, template_docx: Path) -> None:
        base = _ingest(template_docx, "1.0")
        changed = _with_style_size(base, "Heading1", 18.0)
        diff = diff_packages(base, changed)
        changes = [change for delta in diff.deltas for change in delta.changes]
        assert changes
        assert all("[style_id=Heading1]" in change.path for change in changes)
        assert {change.kind for change in changes} == {"changed"}

    def test_nested_slots_in_the_tree_are_not_reported_twice(self, template_docx: Path) -> None:
        """骨架树里的 ``slots`` 是扁平索引的挂载副本，不该让一处变更报两次。"""
        base = _ingest(template_docx, "1.0")
        slots = [
            slot.model_copy(update={"required": not slot.required})
            if slot.slot_id == "c1"
            else slot
            for slot in base.outline.slots
        ]
        outline = base.outline.model_copy(update={"slots": slots})
        changed = base.model_copy(update={"outline": outline})
        diff = diff_packages(base, changed)
        changes = [change for delta in diff.deltas for change in delta.changes]
        assert len(changes) == 1
        assert "slots[slot_id=c1].required" in changes[0].path


class TestBlocking:
    def test_removing_a_block_id_blocks(self, template_docx: Path) -> None:
        """``block_id`` 消失是硬阻断：档案与生成期引用的是 id，不是位置。"""
        base = _ingest(template_docx, "1.0")
        diff = diff_packages(base, _drop_block(base, "revision-1"))
        assert not diff.ok
        assert "blocks" in {delta.section for delta in diff.blocking_deltas}
        assert any("block_id=revision-1" in line for line in diff.changelog())
        assert "阻断项" in diff.summary()

    def test_removing_a_slot_blocks(self, template_docx: Path) -> None:
        """槽位消失影响档案可满足性（DD-7.4 的 outline 一行）。"""
        base = _ingest(template_docx, "1.0")
        assert "c2.1" in [slot.slot_id for slot in base.outline.slots]
        diff = diff_packages(base, _drop_slot(base, "c2.1"))
        assert diff.changed_sections == ("outline",)
        assert not diff.ok

    def test_changing_a_slot_does_not_block(self, template_docx: Path) -> None:
        """改槽位的描述/必填性不该阻断构建——否则预检会变成一句空话。"""
        base = _ingest(template_docx, "1.0")
        slots = [
            slot.model_copy(update={"description": "改写过的"}) if slot.slot_id == "c1" else slot
            for slot in base.outline.slots
        ]
        changed = base.model_copy(
            update={"outline": base.outline.model_copy(update={"slots": slots})}
        )
        diff = diff_packages(base, changed)
        assert diff.changed_sections == ("outline",)
        assert diff.ok

    def test_removing_a_block_asset_blocks(self, template_docx: Path) -> None:
        base = _ingest(template_docx, "1.0")
        diff = diff_packages(
            base, base, block_assets_before={"cover.png": b"PNG"}, block_assets_after={}
        )
        assert not diff.ok
        assert diff.changed_sections == ("blocks",)

    def test_replacing_a_block_asset_is_not_blocking(self, template_docx: Path) -> None:
        base = _ingest(template_docx, "1.0")
        diff = diff_packages(
            base,
            base,
            block_assets_before={"cover.png": b"PNG-1"},
            block_assets_after={"cover.png": b"PNG-2"},
        )
        assert diff.changed_sections == ("blocks",)
        assert diff.ok


class TestFingerprintLevelDiff:
    def test_diff_fingerprints_agrees_with_diff_packages(self, template_docx: Path) -> None:
        """廉价通路与全量通路必须给出同一组"变了的分区"。

        两者不一致时使用者会相信其中一个，而另一个会变成"看起来坏了"的功能。
        """
        base = _ingest(template_docx, "1.0")
        changed = _with_style_size(_with_margin(base, 2000), "Heading1", 18.0)
        cheap = diff_fingerprints(compute_fingerprint(base), compute_fingerprint(changed))
        full = diff_packages(base, changed).changed_sections
        assert set(cheap) == set(full)

    def test_source_change_is_visible_only_through_the_fingerprint(
        self, template_docx: Path
    ) -> None:
        """原件摘要只能从指纹里读——包目录不含原件，重算不出来。

        它是"原件真的改了、契约一个字没变"（模板被重存、或改动落在未建模属性上）
        的唯一可见通道。
        """
        base = _fingerprinted(_ingest(template_docx, "1.0"))
        assert base.fingerprint is not None
        base = base.model_copy(
            update={
                "fingerprint": base.fingerprint.model_copy(
                    update={"sections": {**base.fingerprint.sections, "source": "1" * 64}}
                )
            }
        )
        other = _fingerprinted(_restamp(base, "2.0"))
        assert other.fingerprint is not None
        other = other.model_copy(
            update={
                "fingerprint": other.fingerprint.model_copy(
                    update={"sections": {**other.fingerprint.sections, "source": "0" * 64}}
                )
            }
        )
        # 两个包的契约内容完全一致，唯一差别就是原件摘要。
        diff = diff_packages(base, other)
        assert diff.changed_sections == ("source",)
        assert diff.ok

    def test_missing_fingerprints_skip_the_source_section(self, template_docx: Path) -> None:
        """两侧都没算过指纹 → 不报"原件变了"，而不是把"没算过"当成"变了"。"""
        base = _ingest(template_docx, "1.0").model_copy(update={"fingerprint": None})
        diff = diff_packages(base, _restamp(base, "2.0"))
        assert "source" not in diff.changed_sections
        assert diff.identical


class TestFlattening:
    def test_keyed_lists_use_the_id_not_the_index(self, template_docx: Path) -> None:
        paths = flatten(section_payloads(_ingest(template_docx))["blocks"], prefix="blocks")
        assert any(path.startswith("blocks.index.blocks[block_id=toc-1]") for path in paths)

    def test_scalars_are_rendered_as_json_text(self) -> None:
        """``True`` 与 ``1`` 在 Python 里相等，在 diff 里必须不等。"""
        assert flatten({"flag": True})["flag"] != flatten({"flag": 1})["flag"]
        assert flatten({"flag": None})["flag"] == "null"

    def test_list_indices_are_stable(self) -> None:
        assert flatten({"items": ["a", "b"]}) == {"items[0]": '"a"', "items[1]": '"b"'}


class TestChangelog:
    def test_changelog_names_the_field_the_old_and_the_new_value(self, template_docx: Path) -> None:
        base = _ingest(template_docx, "1.0")
        diff = diff_packages(base, _with_margin(base, 2000))
        lines = diff.changelog()
        assert len(lines) == 1
        assert "page_setup.margins.left_twips" in lines[0]
        assert str(base.page_setup.margins.left_twips) in lines[0]
        assert "2000" in lines[0]
        assert lines[0].startswith(f"[{CATEGORY_PAGE_SETUP}]")

    def test_changelog_limit_keeps_the_order(self, template_docx: Path) -> None:
        base = _ingest(template_docx, "1.0")
        changed = _with_style_size(_with_margin(base, 2000), "Heading1", 18.0)
        diff = diff_packages(base, changed)
        assert diff.changelog(limit=2) == diff.changelog()[:2]

    def test_unused_slot_helper_keeps_the_fixture_honest(self, template_docx: Path) -> None:
        """``SlotSpec`` 的存在感：夹具确实带槽位，否则上面的断言是空跑。"""
        assert _ingest(template_docx).outline.slots
        assert isinstance(_ingest(template_docx).outline.slots[0], SlotSpec)
