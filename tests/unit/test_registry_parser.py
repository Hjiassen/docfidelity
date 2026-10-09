"""``registry.parser`` 入库解析阶段单测（DD-7.1 / DD-7.2）。

这一组测试用**真实模板夹具**（封面独占一节、修订记录表、页眉占位、多级标题），
因为入库解析的全部风险都来自真实模板的形制：封面是小节不是段落样式、
修订记录表的识别靠表头词、占位符最爱藏在页眉里。

每个阶段都有一组"防静默失效"断言。文本提取最容易出的一类 bug 是
**把容器的 ``.text`` 当文本**（``w:p`` 的 ``.text`` 永远是空串），它不报错，
只是让骨架为空、占位符清单为空——所以下面既有"识别得出"的正向断言，
也有"不该识别成标题"的反向断言。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from docfidelity.contracts.errors import EngineError, ErrorCode
from docfidelity.ooxml import DOCUMENT_PART, DocxPackage, iter_elements, paragraph_text, qn, xpath
from docfidelity.registry import parser
from docfidelity.registry.parser.blocks import DEFAULT_REVISION_KEYWORDS
from docfidelity.registry.parser.common import document_order_locator

pytestmark = pytest.mark.unit


@pytest.fixture
def package(template_docx: Path) -> DocxPackage:
    return DocxPackage.open(template_docx, read_only=True)


class TestUnpack:
    def test_snapshot_records_name_size_and_digest(self, template_docx: Path) -> None:
        snapshot = parser.snapshot_source(template_docx)
        assert snapshot.filename == "template.docx"
        assert snapshot.size_bytes > 0
        assert len(snapshot.sha256) == 64

    def test_snapshot_is_stable_for_the_same_file(self, template_docx: Path) -> None:
        assert (
            parser.snapshot_source(template_docx).sha256
            == parser.snapshot_source(template_docx).sha256
        )

    def test_missing_file_raises_unreadable(self, tmp_path: Path) -> None:
        with pytest.raises(EngineError) as info:
            parser.snapshot_source(tmp_path / "nope.docx")
        assert info.value.code == ErrorCode.TEMPLATE_UNREADABLE

    def test_open_rejects_package_without_document_part(self, tmp_path: Path) -> None:
        """一个合法的 OOXML 包也可能没有正文——那种包解析不出骨架，必须当场拒掉。"""
        import zipfile

        target = tmp_path / "no-document.docx"
        with zipfile.ZipFile(target, "w") as archive:
            archive.writestr(
                "[Content_Types].xml",
                '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/'
                'package/2006/content-types"/>',
            )
        with pytest.raises(EngineError) as info:
            parser.open_template(target)
        assert info.value.code == ErrorCode.TEMPLATE_UNREADABLE
        assert "document.xml" in info.value.message

    def test_open_rejects_non_zip(self, tmp_path: Path) -> None:
        target = tmp_path / "broken.docx"
        target.write_bytes(b"this is not a zip at all")
        with pytest.raises(EngineError) as info:
            parser.open_template(target)
        assert info.value.code == ErrorCode.TEMPLATE_UNREADABLE


class TestIdentity:
    def test_version_is_read_from_the_cover(
        self, package: DocxPackage, template_docx: Path
    ) -> None:
        """封面写着「版本：V2.1」——版本号必须走"从文档里读"这条高置信路径。"""
        identity, _ = parser.detect_identity(package, parser.snapshot_source(template_docx))
        assert identity.version == "2.1"
        assert identity.version_source == "document"

    def test_explicit_values_always_win(self, package: DocxPackage, template_docx: Path) -> None:
        identity, findings = parser.detect_identity(
            package, parser.snapshot_source(template_docx), template_id="TPL-9", version="9.9"
        )
        assert identity.template_id == "TPL-9"
        assert identity.version == "9.9"
        assert identity.is_confident
        assert findings == []

    def test_filename_fallback_is_flagged_for_review(
        self, package: DocxPackage, template_docx: Path
    ) -> None:
        """低置信**必须**进待确认队列，不能静默用一个猜出来的编号。"""
        identity, findings = parser.detect_identity(package, parser.snapshot_source(template_docx))
        assert identity.template_id_source == "filename"
        assert not identity.is_confident
        assert [finding.rule_id for finding in findings] == [parser.IDENTITY_RULE_ID]

    def test_document_number_is_not_mistaken_for_a_version(
        self, package: DocxPackage, template_docx: Path
    ) -> None:
        """封面还有「文件编号：TF-001」——它只有一段数字，不该被当成版本号。"""
        identity, _ = parser.detect_identity(package, parser.snapshot_source(template_docx))
        assert identity.version != "001"


class TestPageSetup:
    def test_single_page_size_and_margins(self, package: DocxPackage) -> None:
        page = parser.parse_page_setup(package)
        assert page.page_size.width_twips > 0
        assert page.page_size.height_twips > 0
        assert page.margins.left_twips >= 0

    def test_section_zero_matches_the_top_level_fields(self, package: DocxPackage) -> None:
        """``geometry.usable_width(page, 0)`` 取的是第 0 节，两者必须一致。"""
        page = parser.parse_page_setup(package)
        assert page.sections[0].page_size == page.page_size
        assert page.sections[0].margins == page.margins

    def test_cover_section_comes_before_body_section(self, package: DocxPackage) -> None:
        """段落内 sectPr 是**前一节**的结束标记，body 直属的 sectPr 才是最后一节。

        写反会让首屏的版心宽取到末节的尺寸，图片缩放与表格列宽整体算错。
        """
        page = parser.parse_page_setup(package)
        assert len(page.sections) == 2
        assert [section.index for section in page.sections] == [0, 1]

    def test_header_reference_is_a_part_path_not_a_rel_id(self, package: DocxPackage) -> None:
        """关系 ID 是易变量，写进契约会让指纹随 Word 任意一次编辑而变。"""
        page = parser.parse_page_setup(package)
        assert page.sections[-1].header_ref == "word/header1.xml"
        assert page.sections[-1].header_footer_roles == ["default"]

    def test_diff_from_previous_lists_changed_fields(self, package: DocxPackage) -> None:
        page = parser.parse_page_setup(package)
        assert page.sections[0].diff_from_previous == []
        # 夹具的两节除页眉引用外版式相同，因此差异里不应出现 page_size。
        assert "page_size" not in page.sections[1].diff_from_previous


class TestOutline:
    def test_heading_levels_come_from_outline_lvl(self, package: DocxPackage) -> None:
        levels = parser.heading_levels(package)
        assert levels["Heading1"] == 1
        assert levels["Heading2"] == 2

    def test_chapter_tree_is_nested_by_level(self, package: DocxPackage) -> None:
        outline, _ = parser.parse_outline(package)
        titles = [node.title for node in outline.chapter_tree]
        assert titles == ["概述", "范围"]
        assert [child.title for child in outline.chapter_tree[1].children] == ["附录"]

    def test_body_text_is_not_mistaken_for_a_heading(self, package: DocxPackage) -> None:
        """正文段落没有标题样式，绝不能进骨架。"""
        outline, _ = parser.parse_outline(package)
        flats = {node.title for node in outline.chapter_tree}
        assert "此处填写项目背景。" not in flats

    def test_slots_are_derived_per_chapter(self, package: DocxPackage) -> None:
        outline, _ = parser.parse_outline(package)
        assert [slot.slot_id for slot in outline.slots] == ["c1", "c2", "c2.1"]
        assert [slot.locator for slot in outline.slots] == [
            "chapter[1]",
            "chapter[2]",
            "chapter[2].section[1]",
        ]

    def test_slots_are_attached_to_their_nodes(self, package: DocxPackage) -> None:
        """扁平索引与树上挂载必须是同一批对象，否则生成期按槽位取内容会落空。"""
        outline, _ = parser.parse_outline(package)
        on_tree = {slot.slot_id for node in outline.chapter_tree for slot in node.slots}
        nested = outline.chapter_tree[1].children[0]
        on_tree |= {slot.slot_id for slot in nested.slots}
        assert on_tree == {slot.slot_id for slot in outline.slots}

    def test_undeclared_slot_unit_types_are_flagged(self, package: DocxPackage) -> None:
        """槽位的内容类型只能由档案声明，骨架阶段不许猜——必须进待确认。"""
        _, findings = parser.parse_outline(package)
        assert [finding.rule_id for finding in findings] == [parser.OUTLINE_RULE_ID]

    def test_parsing_a_plain_document_does_not_raise(self, sample_docx: Path) -> None:
        """没有封面/修订表/页眉的普通文档也必须能解析出骨架，不能报错。"""
        pkg = DocxPackage.open(sample_docx, read_only=True)
        outline, findings = parser.parse_outline(pkg)
        assert [node.title for node in outline.chapter_tree] == ["概述", "范围"]
        assert all(finding.rule_id == parser.OUTLINE_RULE_ID for finding in findings)


class TestBlocks:
    def test_cover_locator_is_precise_and_unique(self, package: DocxPackage) -> None:
        """封面定位符必须**唯一命中**，且指向封面的末段。

        旧的 ``//w:p[1]`` 在这份模板上命中 12 个节点（每个表格单元格都算一个父节点），
        调用方取文档序第一个，拿到的是**内容控件里的目录条目段**——受保护区于是整个
        挪到了目录上，封面里的占位段落一个都判不进去。
        """
        blocks, _ = parser.parse_blocks(package)
        cover = [block for block in blocks.blocks if block.kind == "cover"]
        assert len(cover) == 1
        root = package.part(DOCUMENT_PART).read_tree
        hits = xpath(root, cover[0].locator.xpath)
        assert (
            len(hits) == 1
        ), f"位置型定位符必须唯一命中：{cover[0].locator.xpath} → {len(hits)} 项"
        # 末段 = 那个带 ``pPr/sectPr`` 的段落：分节符写在它身上，封面区间到此为止。
        properties = hits[0].find(qn("w:pPr"))
        assert properties is not None and properties.find(qn("w:sectPr")) is not None

    def test_positional_locators_must_be_parenthesised(self, package: DocxPackage) -> None:
        """``//w:p[6]`` 与 ``(//w:p)[6]`` 不是一回事——这条差异是整个缺陷的根因。

        谓词作用在"每个父节点的子节点集"上时，``//w:p[6]`` 的语义是"**每个**父节点下的
        第 6 段"。故这里正面断言两者的差异**确实存在**："记得加括号"于是不靠注释提醒。
        """
        root = package.part(DOCUMENT_PART).read_tree
        tight = xpath(root, document_order_locator("p", 6))
        loose = xpath(root, "//w:p[6]")
        assert [paragraph_text(item) for item in tight] == ["此处填写编制单位"]
        # 松散写法落在「概述」标题段上：body 的第 6 个 ``w:p`` 才是它。
        assert [paragraph_text(item) for item in loose] == ["概述"]

    def test_cover_range_does_not_swallow_the_toc_entries(self, package: DocxPackage) -> None:
        """封面区间从**正文直属**的第一段起算，不把内容控件里的目录条目算进来。

        ``iter_elements`` 给的是全篇文档序（含 ``w:sdt`` 与 ``w:tc`` 里的段落），
        而封面是"正文直属的一段连续段落"。按全篇第 1 段起算，区间里就混进了两个目录
        条目段——区间错一格，下游受保护区整个挪位。
        """
        blocks, _ = parser.parse_blocks(package)
        cover = next(block for block in blocks.blocks if block.kind == "cover")
        first, last = cover.structure["paragraph_range"]
        paragraphs = list(iter_elements(package.part(DOCUMENT_PART).read_tree, "w:p"))
        assert paragraph_text(paragraphs[first - 1]) == "XX 项目说明书"
        # 紧邻的前一段是目录条目，它**不属于**封面。
        assert paragraph_text(paragraphs[first - 2]) == "范围1"
        assert last > first

    def test_revision_table_is_located_by_header_keywords(self, package: DocxPackage) -> None:
        blocks, _ = parser.parse_blocks(package)
        revision = [block for block in blocks.blocks if block.kind == "revision"]
        assert len(revision) == 1
        matched = revision[0].structure["header_keywords"]
        assert len(matched) >= 2
        assert set(matched) <= set(DEFAULT_REVISION_KEYWORDS)

    def test_single_word_table_is_not_a_revision_table(self, package: DocxPackage) -> None:
        """正文里出现一次「日期」的普通表格很多，阈值太低会满屏误报。"""
        blocks, _ = parser.parse_blocks(package)
        revision = [block for block in blocks.blocks if block.kind == "revision"]
        assert revision[0].structure["row_count"] == 2

    def test_toc_block_points_at_the_content_control(self, package: DocxPackage) -> None:
        blocks, _ = parser.parse_blocks(package)
        toc = [block for block in blocks.blocks if block.kind == "toc"]
        assert len(toc) == 1
        assert "sdt" in toc[0].locator.xpath

    def test_header_block_carries_its_role(self, package: DocxPackage) -> None:
        blocks, _ = parser.parse_blocks(package)
        headers = [block for block in blocks.blocks if block.kind == "header"]
        assert [block.block_id for block in headers] == ["header-default"]
        assert headers[0].locator.part == "word/header1.xml"

    def test_block_ids_are_unique(self, package: DocxPackage) -> None:
        """块 id 是档案引用与生成期取块的键，重复会让引用指向随机一个块。"""
        blocks, _ = parser.parse_blocks(package)
        ids = [block.block_id for block in blocks.blocks]
        assert len(ids) == len(set(ids))


class TestPlaceholders:
    def test_body_placeholders_are_listed(self, package: DocxPackage) -> None:
        placeholders = parser.parse_placeholders(package)
        texts = [entry.text for entry in placeholders.entries]
        assert "此处填写编制单位" in texts
        assert "此处填写项目背景。" in texts

    def test_header_placeholder_is_not_missed(self, package: DocxPackage) -> None:
        """页眉不在正文里，却会在**每一页**重复出现——漏掉就等于让痕迹印满全篇。"""
        placeholders = parser.parse_placeholders(package)
        parts = {entry.locator.part for entry in placeholders.entries}
        assert "word/header1.xml" in parts

    def test_every_placeholder_locator_hits_its_own_paragraph(self, package: DocxPackage) -> None:
        """定位符必须命中**它自己那一段**——清单与产物对得上的唯一保证。

        这条不变量足以挡住整类缺陷：登记的是"此处填写编制单位"，定位符却指向
        「概述」标题段，于是清除步骤判它"不在受保护区内"而跳过，残留护栏再报一条
        FR-M2-11——而清单上的数字看起来完全正常。
        """
        placeholders = parser.parse_placeholders(package)
        assert placeholders.entries, "夹具模板里应当有占位符，否则这条断言是空转的"
        for entry in placeholders.entries:
            hits = xpath(package.part(entry.locator.part).read_tree, entry.locator.xpath)
            assert len(hits) == 1, f"{entry.locator.xpath} 命中 {len(hits)} 项"
            collapsed = " ".join(paragraph_text(hits[0]).split())
            # ``entry.text`` 是折叠后的整段文本（超长会被截断），故按前缀比。
            assert collapsed.startswith(entry.text), f"{entry.text!r} 定位到了 {collapsed!r}"

    def test_each_placeholder_is_listed_once(self, package: DocxPackage) -> None:
        """扫描范围重叠会让同一处占位出现两次，"待填 2 处"会把使用者引向不存在的地方。"""
        placeholders = parser.parse_placeholders(package)
        keys = [(entry.locator.part, entry.locator.xpath) for entry in placeholders.entries]
        assert len(keys) == len(set(keys))

    def test_proposed_rule_is_recorded(self, package: DocxPackage) -> None:
        placeholders = parser.parse_placeholders(package)
        assert all(
            entry.proposed_rule_id == parser.PLACEHOLDER_RULE_ID for entry in placeholders.entries
        )

    @pytest.mark.parametrize(
        "text",
        ["此处填写名称", "待补充", "XXX", "× × × ×", "TBD", "<项目名称>"],
    )
    def test_recognised_placeholder_forms(self, text: str) -> None:
        assert parser.matched_placeholder(text) is not None

    @pytest.mark.parametrize(
        "text",
        ["这是正常正文段落。", "略述背景情况", "策略与计划", "x×x×x"],
    )
    def test_ordinary_text_is_not_a_placeholder(self, text: str) -> None:
        """单字词（"略"）会让"策略""省略"全部命中，清单立刻失去可用性。"""
        assert parser.matched_placeholder(text) is None
