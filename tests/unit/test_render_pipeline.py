"""``render`` 端到端：IR + 模板包 → docx（TASK-P0-09 的验收判据「docx 可产出」）。

这一组测的是**产物本身**，不是函数返回值。渲染层的全部风险都在"产物看起来对、
实际不对"：少一章、样式静默回退、模板示例残留、分页点散落。故断言全部落在
产物的字节与结构上：

* 未触碰的部件**字节全等**（样式保真承诺的技术边界）；
* 样式层**零改动**（产出引用的 styleId 与模板逐一对应，且没有多出样式定义）；
* 同一输入连续构建 3 次**逐字节一致**（NFR-02 幂等）；
* 前置版块（封面 / 修订记录 / 目录 / 页眉）在注入后仍在。
"""

from __future__ import annotations

import json
import zipfile
from dataclasses import dataclass
from pathlib import Path

import pytest

from docfidelity.contracts.ir import ContentIR, ContentUnit
from docfidelity.contracts.profile import DocTypeProfile
from docfidelity.contracts.template import TemplatePackage
from docfidelity.ooxml import (
    DOCUMENT_PART,
    DocxPackage,
    attr,
    find_all,
    iter_elements,
    localname,
    paragraph_text,
    qn,
)
from docfidelity.render import (
    DEFERRED_STEPS,
    STEP_ORDER,
    RenderContext,
    RenderPolicy,
    open_working_copy,
    render,
)
from docfidelity.render.blocks import (
    clear_protected_placeholders,
    locate_regions,
    placeholder_residue,
)
from docfidelity.render.pagination import count_page_breaks
from tests.fixtures.docx_builder import png_bytes
from tests.fixtures.render_fixture import (
    CHAPTER_PARAGRAPH,
    TEMPLATE_EXAMPLE_PARAGRAPH,
    body_texts,
    chapter_ir,
    docx_part_bytes,
    ingest_contract,
    make_context,
    plain_ir,
    template_docx_path,
)

pytestmark = pytest.mark.unit

#: 渲染**允许**改动的部件。其余部件必须与模板原件字节全等。
#: 多一个都说明"某条读路径把部件置脏了"——那正是 ``ensure_style`` 曾经踩过的坑。
#:
#: ``word/settings.xml`` 是 TASK-P0-10 加进来的：目录重建要置 ``w:updateFields``，
#: 让 Word 打开产物时自动刷新页码（不置的话显示的是模板的旧页码——**错的**而不是
#: **缺的**，使用者很难察觉）。它是**有意写**，不是被读路径置脏。
WRITABLE_PARTS = frozenset(
    {
        "word/document.xml",
        "word/header1.xml",
        "word/numbering.xml",
        "word/settings.xml",
        "word/styles.xml",
    }
)

#: 这些部件**只被读**，任何字节差异都意味着某条读路径用了 ``tree`` 而不是 ``read_tree``。
READ_ONLY_PARTS = ("[Content_Types].xml", "word/theme/theme1.xml")


@dataclass(frozen=True)
class Harness:
    """一次渲染所需的全部输入（模板路径 + 入库契约 + 档案）与出参目录。"""

    template: Path
    contract: TemplatePackage
    profile: DocTypeProfile
    out_dir: Path

    def render(self, name: str = "out.docx", **kwargs: object):  # type: ignore[no-untyped-def]
        from docfidelity.render import RenderResult

        policy = kwargs.pop("policy", None)
        assert policy is None or isinstance(policy, RenderPolicy)
        ir = kwargs.pop("ir", None)
        assert ir is None or isinstance(ir, ContentIR)
        asset_loader = kwargs.pop("asset_loader", None)
        # 域刷新是否走 Word COM 必须**钉死**，不能留给 ``None`` 去自动探测：
        # 探测读的是"本机装没装 Word"（``ooxml.refresh.office_available``），于是同一份
        # 测试在开发机上会真去起 Word、在 CI（Linux）上走降级，两边 ``refresh`` 步骤的
        # 载荷与判定都不同——而单测的结论不该随开发机变了什么而变。缺省 ``False``
        # 就是"没有 Office"，与 CI 同姿态。
        office = kwargs.pop("office", False)
        assert not kwargs, f"未识别的参数：{sorted(kwargs)}"
        out = self.out_dir / name
        result: RenderResult = render(
            pkg=open_working_copy(self.template),
            template=self.contract,
            profile=self.profile,
            ir=ir if ir is not None else plain_ir(),
            out_path=out,
            policy=policy,
            asset_loader=asset_loader,  # type: ignore[arg-type]
            office=office,  # type: ignore[arg-type]
        )
        return result, out


@pytest.fixture(scope="module")
def harness(tmp_path_factory: pytest.TempPathFactory) -> Harness:
    template = template_docx_path(tmp_path_factory.mktemp("render"))
    contract, profile = ingest_contract(template)
    return Harness(
        template=template,
        contract=contract,
        profile=profile,
        out_dir=tmp_path_factory.mktemp("render-out"),
    )


def _style_ids(path: Path) -> set[str]:
    with DocxPackage.open(path, read_only=True) as pkg:
        # ``w:styles`` 就是部件根元素：``find_first`` 查的是**子元素**，对根自身
        # 永远返回 None。这个坑在测试里同样致命——断言会变成 `None == None` 恒真。
        root = pkg.part("word/styles.xml").read_tree
        assert localname(root) == "styles"
        return {
            value
            for value in (attr(item, "w:styleId") for item in find_all(root, "w:style"))
            if value
        }


def _referenced_paragraph_styles(path: Path) -> set[str]:
    with DocxPackage.open(path, read_only=True) as pkg:
        root = pkg.part(DOCUMENT_PART).read_tree
        return {
            value
            for value in (attr(item, "w:val") for item in iter_elements(root, "w:pStyle"))
            if value
        }


# ------------------------------------------------------------------ 可产出
def test_render_writes_a_readable_docx(harness: Harness) -> None:
    result, out = harness.render()

    assert result.written, f"产物未落盘，护栏 blocker：{result.guardrail_blockers}"
    assert result.artifact_path == out
    assert out.is_file() and zipfile.is_zipfile(out)
    # 能被自己的读取路径打开，且正文与样式表都在 —— "可产出"的最低要求。
    with DocxPackage.open(out, read_only=True) as pkg:
        assert pkg.has_part(DOCUMENT_PART)
        assert pkg.has_part("word/styles.xml")
    assert result.package_hash is not None and len(result.package_hash) == 64


def test_render_has_no_blocker(harness: Harness) -> None:
    result, _out = harness.render()
    blockers = [f"{item.rule_id} {item.message}" for item in result.blockers()]
    assert blockers == [], f"渲染产出 blocker：{blockers}"


def test_render_covers_every_step_in_order(harness: Harness) -> None:
    result, _out = harness.render()
    assert [step.step for step in result.steps] == list(STEP_ORDER)
    # DD-9.1 的 13 步已全部落地；"未实现的步骤"必须为空，而不是"忘了登记"。
    assert DEFERRED_STEPS == (), f"DD-9.1 的步骤应已全部落地，仍有：{DEFERRED_STEPS}"
    assert result.deferred_steps == DEFERRED_STEPS
    # 目录与域刷新必须在步骤序列里**确实出现**，而不是悄悄漏掉。
    assert {"toc", "refresh"} <= {step.step for step in result.steps}
    # checkpoint 摘要必须可 JSON 化（续跑要落库）。
    json.dumps(result.checkpoint_payload(), ensure_ascii=False)


def test_the_harness_pins_office_off(harness: Harness) -> None:
    """夹具必须**钉死**"本机没有 Word"，不能把 ``office`` 留给渲染去探测。

    域刷新是整条渲染里唯一会起 Word 进程的一步（``ooxml.refresh.refresh_with_office``）。
    夹具若让它自动探测，同一份测试在开发机上会真起 ``WINWORD.EXE``、在 CI（Linux）上
    走降级——两边 ``refresh`` 步骤的载荷与判定都不同，单测的结论就跟着"开发机装了
    什么"而变。这条用例把"钉死"本身钉住：谁把缺省改回 ``None``，它就会红。
    """
    result, _out = harness.render()

    payload = next(step.checkpoint_outputs for step in result.steps if step.step == "refresh")
    assert payload["office_available"] is False, "夹具没钉死 office，渲染会去探测本机 Word"
    assert payload["skipped_reason"] == "office-unavailable"


# ---------------------------------------------------------------- 内容正确
def test_injected_units_appear_in_document_order(harness: Harness) -> None:
    result, out = harness.render(ir=chapter_ir(with_table=True, with_subheading=True))
    assert result.written

    entries = body_texts(out)
    texts = [text for _tag, text in entries]
    assert "概述" in texts and CHAPTER_PARAGRAPH in texts
    assert "范围" in texts and "表 2-1 范围对照" in texts
    assert "附录" in texts
    # 表格是真的表格元素，不是被塞进段落的文本。
    assert any(tag == "tbl" for tag, _text in entries)
    # 顺序：章标题 → 正文 → 章标题 → 表注。
    assert texts.index("概述") < texts.index(CHAPTER_PARAGRAPH) < texts.index("范围")
    assert texts.index("范围") < texts.index("表 2-1 范围对照")


def test_template_example_area_is_replaced(harness: Harness) -> None:
    _result, out = harness.render()
    texts = [text for _tag, text in body_texts(out)]
    assert TEMPLATE_EXAMPLE_PARAGRAPH not in texts, "模板示例内容区没有被裁掉"
    assert CHAPTER_PARAGRAPH in texts


def test_protected_front_matter_survives(harness: Harness) -> None:
    """封面 / 修订记录 / 目录 / 页眉是体例的一部分，注入后必须还在。"""
    _result, out = harness.render()
    entries = body_texts(out)
    tags = [tag for tag, _text in entries]
    texts = [text for _tag, text in entries]

    assert "sdt" in tags, "目录控件（sdt）被裁掉了"
    assert "tbl" in tags, "修订记录表（前置版块）被裁掉了"
    assert "XX 项目说明书" in texts
    assert "文件编号：TF-001" in texts
    with DocxPackage.open(out, read_only=True) as pkg:
        assert pkg.has_part("word/header1.xml"), "页眉部件丢失"
        header = pkg.part("word/header1.xml").read_tree
        assert localname(header) == "hdr"
        # 页眉里的占位文本必须被清掉（受保护区内的占位符）。
        heading_texts = "".join(node.text or "" for node in iter_elements(header, "w:t"))
        assert "此处填写" not in heading_texts
        # 修订记录表不仅元素在，表头词也还在——元素在而内容被清空同样是"体例被破坏"。
        document = pkg.part(DOCUMENT_PART).read_tree
        tokens = {node.text or "" for node in iter_elements(document, "w:t")}
        assert {"序号", "修改内容", "修改人"} <= tokens


def test_cover_region_covers_the_whole_cover(harness: Harness) -> None:
    """封面受保护区必须覆盖**一整段**，而不是"区间里的某一个段落"。

    封面是"首个分节符之前的连续段落"，是一个区间。退化成单节点之后，封面里的占位
    段落判不进受保护区，清除步骤静默跳过——产物里留着编辑痕迹，而清单说这一处已处理。
    """
    ctx = make_context(harness.template, contract=harness.contract, profile=harness.profile)
    locate_regions(ctx)
    region = ctx.regions["cover-1"]
    texts = [paragraph_text(element) for element in region.elements]
    assert texts[0] == "XX 项目说明书", f"封面区间起点不对：{texts}"
    assert "此处填写编制单位" in texts, f"封面里的占位段落没被区间覆盖：{texts}"
    # 区间以那个带分节符的段落收尾（分节符写在它的 pPr 里）。
    properties = region.elements[-1].find(qn("w:pPr"))
    assert properties is not None and properties.find(qn("w:sectPr")) is not None
    # 首个元素就是分页白名单要的那个落点。
    assert region.element is region.elements[0]


def test_cover_placeholder_is_cleared_not_left_behind(harness: Harness) -> None:
    """封面占位必须被**清除**，而不是"漏进产物 + 报一条残留"。

    三条断言同时成立才算修好：清除清单里有它、产物正文里搜不到它、而且**没有**
    FR-M2-11 判定。此前每份产物都挂着一条 FR-M2-11（它只到 major，不是 blocker，
    所以门禁全绿把它盖住了）。
    """
    result, out = harness.render()
    outputs = _step(result, "blocks")
    assert "此处填写编制单位" in outputs["cleared_texts"]
    assert outputs["unmatched"] == [], f"有占位段定位不到：{outputs['unmatched']}"

    with DocxPackage.open(out, read_only=True) as pkg:
        document = pkg.part(DOCUMENT_PART).read_tree
        texts = "".join(node.text or "" for node in iter_elements(document, "w:t"))
    assert "此处填写" not in texts, "封面占位漏进了产物"

    residue_findings = [item for item in result.findings() if item.rule_id == "FR-M2-11"]
    assert residue_findings == [], f"仍报占位残留：{residue_findings}"


def test_cut_away_placeholder_is_not_counted_as_residue(harness: Harness) -> None:
    """判残留要问"它还在不在产物里"，不是"它有没有文本"。

    内容区里的占位段随内容区一起被裁掉，元素成了游离子树：文本还在内存里，
    产物里却已经没有这一段。反面也断言到了——**还没裁**的时候它确实算残留，
    这样"判空"就不是因为判据恒假。
    """
    ctx = make_context(harness.template, contract=harness.contract, profile=harness.profile)
    entries = locate_regions(ctx)
    clear_protected_placeholders(ctx, entries)

    sites = {site.text: site for site in ctx.placeholder_sites}
    example = sites[TEMPLATE_EXAMPLE_PARAGRAPH]
    assert not example.protected, "内容区里的占位符不该被登记成受保护区内的"
    assert example.still_in_document()
    assert TEMPLATE_EXAMPLE_PARAGRAPH in placeholder_residue(ctx)

    element = example.element
    assert element is not None
    parent = element.getparent()
    assert parent is not None
    parent.remove(element)
    assert not example.still_in_document()
    assert TEMPLATE_EXAMPLE_PARAGRAPH not in placeholder_residue(ctx)


def test_lists_and_pagebreak_land_on_declared_targets(harness: Harness) -> None:
    result, out = harness.render(ir=chapter_ir(with_lists=True, with_pagebreak=True))
    assert result.written

    texts = [text for _tag, text in body_texts(out)]
    assert "要点一" in texts and "要点二" in texts and "第一步" in texts

    with DocxPackage.open(out, read_only=True) as pkg:
        body = pkg.part(DOCUMENT_PART).read_tree

    outputs = _step(result, "pagination")
    # 判据不是"分页点为 1"，而是"**每一个**分页点都落在白名单上"（DD-9.3；见
    # ``render.pagination`` 模块文档对那处自相矛盾的拆解）。白名单本就有三个来源：
    # 修订记录页 / 目录页的强制分页 + IR 里声明的分页单元。故这里的断言是
    # "分页点数 == 白名单落点数 == 实际设置数"，而不是一个写死的数字。
    assert outputs["off_whitelist"] == [], "出现了白名单之外的分页点"
    assert outputs["applied_breaks"] == len(outputs["whitelist"])
    assert (
        count_page_breaks(body) == outputs["applied_breaks"]
    ), "产物的分页点数与白名单落点数不一致——多出来的是残留，少掉的是被清掉后没重建"
    # 声明的分页单元必须**确实**产出了一个分页点（而不是被"清除"步骤静默吃掉）。
    assert count_page_breaks(body) >= 1


def test_bullets_from_declared_field_are_not_lost(harness: Harness) -> None:
    """列表条目装在 ``fields["items"]`` 里时也必须逐条落进产物。

    装配层（``model.projection``）对 ``unit=bullets`` 产出的就是这个形态：``text``
    为 ``None``、条目在 ``fields["items"]``。渲染层若只读 ``text``，整个列表会退化成
    **一个空段落**——单元在、字段有值、判定全绿，只有人眼能看出列表整段消失。
    两条通路各有一半测试覆盖（compose 侧断言 ``fields["items"]``、render 侧夹具用
    ``text``），所以分叉处此前没人钉。
    """
    bullets = ContentUnit(
        unit_id="c1#l0",
        type="bullets",
        fields={"items": ["要点甲", "要点乙", "要点丙"]},
    )
    ir = ContentIR(
        project_id="demo",
        doc_key="detail",
        profile_id="api-compact",
        template_ref="TPL-DEMO@1.0",
        units=[ContentUnit(unit_id="c1", type="heading", level=1, text="概述", children=[bullets])],
    )
    result, out = harness.render(ir=ir)
    assert result.written
    texts = [text for _tag, text in body_texts(out)]
    for item in ("要点甲", "要点乙", "要点丙"):
        assert item in texts, f"列表条目 {item!r} 没有落进产物：{texts}"
    assert _step(result, "inject")["list_items"] == 3


def test_prototypes_come_from_the_example_region(harness: Harness) -> None:
    """正文原型取自**示例内容区**，前置版块（封面 / 修订记录）不参与。

    整篇取第一个候选会把封面段落当正文原型：注入的每一段正文都套上封面样式，
    而产物表面完全正常。夹具的封面是"首个分节符之前的连续段落"，正好是这条判据的
    反例来源——它的第一段（``XX 项目说明书``）就是整篇第一个非标题段落。
    """
    ctx = make_context(harness.template, contract=harness.contract, profile=harness.profile)
    children = list(ctx.body)
    start, end = ctx.region
    assert end > start, "夹具应当有示例内容区，否则这条测试没有反例"

    pre_block_texts = {
        paragraph_text(child) for child in children[:start] if localname(child) == "p"
    }
    assert "XX 项目说明书" in pre_block_texts, f"封面段落没落在内容区之前：{pre_block_texts}"

    prototype = ctx.prototypes.body
    assert prototype is not None
    assert any(
        prototype is child for child in children[start:end]
    ), f"正文原型取自前置版块：{paragraph_text(prototype)!r}"


def test_figure_is_placed_and_captioned(harness: Harness) -> None:
    result, out = harness.render(
        ir=chapter_ir(with_figure=True),
        asset_loader=lambda _ref: png_bytes(240, 160),
    )
    assert result.written
    assert [item.message for item in result.blockers()] == []

    texts = [text for _tag, text in body_texts(out)]
    assert "图 3-1 示意图" in texts
    with DocxPackage.open(out, read_only=True) as pkg:
        media = [name for name in pkg.part_names("all") if name.startswith("word/media/")]
    assert media, "图片没有落进 word/media/"


def test_consecutive_picture_captions_preserve_the_next_heading(harness: Harness) -> None:
    figures = [
        ContentUnit(
            unit_id=f"pictures#{index}",
            type="figure",
            fields={
                "image_ref": f"image-{index}.png",
                "caption": f"图 {index} 图注",
                "width_px": 240,
                "height_px": 160,
                "dpi": 96,
            },
        )
        for index in range(1, 4)
    ]
    ir = plain_ir(
        [
            ContentUnit(unit_id="pictures", type="heading", level=1, text="图示", children=figures),
            ContentUnit(
                unit_id="following",
                type="heading",
                level=1,
                text="后继章节",
                children=[ContentUnit(unit_id="following#text", type="para", text="后继正文")],
            ),
        ]
    )
    result, out = harness.render(ir=ir, asset_loader=lambda _ref: png_bytes(240, 160))
    assert result.written
    texts = [text for _tag, text in body_texts(out)]
    for caption in ["图 1 图注", "图 2 图注", "图 3 图注"]:
        assert caption in texts
    assert "后继章节" in texts
    assert "后继正文" in texts
    assert texts.index("图 3 图注") < texts.index("后继章节")


def test_missing_figure_asset_is_reported_but_still_produces(harness: Harness) -> None:
    """资源不可用时**必须可运行**（硬规则 9）：报 blocker、照样写盘、不抛异常。

    内容不完整的 blocker 是"给人看的"，不是"拦写盘的"（见 ``pipeline`` 模块文档）。
    """
    result, out = harness.render(ir=chapter_ir(with_figure=True))
    assert result.written and out.is_file()
    assert result.guardrail_blockers == []
    assert any(item.rule_id == "FR-M5-09" for item in result.findings())
    # 但内容确实少了：标题段落仍注入，图片段落存在而图没有。
    texts = [text for _tag, text in body_texts(out)]
    assert "图示" in texts


# -------------------------------------------------------------- 样式保真
def test_untouched_parts_are_byte_identical(harness: Harness) -> None:
    _result, out = harness.render()

    before = docx_part_bytes(harness.template)
    after = docx_part_bytes(out)
    assert set(before) == set(after), "本用例不含图片，部件集合不应变化"

    changed = {name for name in before if before[name] != after[name]}
    unexpected = changed - WRITABLE_PARTS
    assert not unexpected, f"这些部件不该被改动：{sorted(unexpected)}"
    # "读一遍就会置脏"的那几个必须原封不动。settings.xml **不**在此列：目录重建
    # 有意往里置 updateFields（见 WRITABLE_PARTS 的说明）。
    for name in READ_ONLY_PARTS:
        if name in before:
            assert before[name] == after[name], f"{name} 被读路径置脏了"


def test_style_layer_is_unchanged(harness: Harness) -> None:
    """样式层零改动：引用全部有定义，且没有多出样式定义。"""
    _result, out = harness.render()

    assert _style_ids(out) == _style_ids(harness.template)
    defined = _style_ids(out)
    referenced = _referenced_paragraph_styles(out)
    assert referenced, "产物里没有任何段落样式引用，断言会变成恒真"
    assert referenced <= defined, f"悬挂样式引用：{sorted(referenced - defined)}"
    # 标题样式来自模板契约，不是引擎凭空造的。
    assert {"Heading1", "Heading2"} <= defined


# -------------------------------------------------------------- 确定性
def test_three_builds_are_byte_identical(harness: Harness) -> None:
    """NFR-02：同一输入连续构建 3 次，产物逐字节一致。"""
    ir = chapter_ir(with_table=True, with_subheading=True)
    hashes: list[str] = []
    payloads: list[bytes] = []
    for index in range(3):
        result, out = harness.render(f"det-{index}.docx", ir=ir)
        assert result.written
        assert result.package_hash is not None
        hashes.append(result.package_hash)
        payloads.append(docx_part_bytes(out)["word/document.xml"])

    assert len(set(hashes)) == 1, f"package_hash 不确定：{hashes}"
    assert len(set(payloads)) == 1, "三次构建的正文部件字节不一致"


def test_output_filename_is_deterministic(harness: Harness) -> None:
    first, _a = harness.render("a.docx")
    second, _b = harness.render("b.docx")
    assert first.filename == second.filename == "demo-detail.docx"


# -------------------------------------------------------------- 护栏
def test_guardrail_blocks_write_when_style_refs_dangle(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """写盘护栏：产物已坏但看不出来的情形，宁可不写盘。"""
    from docfidelity.render import pipeline

    monkeypatch.setattr(pipeline, "validate_style_refs", lambda _pkg, _styles: ["Missing"])
    result, out = harness.render("guarded.docx")

    assert not result.written
    assert not out.exists(), "命中护栏却仍然写盘了"
    assert [item.rule_id for item in result.guardrail_blockers] == ["FR-M5-02"]


def test_unknown_unit_type_is_reported_and_skipped(harness: Harness) -> None:
    """分派表缺项必须**显式**兜底：内容凭空少一段是最难发现的缺陷。"""
    unit = ContentUnit.model_construct(
        unit_id="x1",
        type="mystery",
        level=None,
        text="不该被静默丢弃",
        fields={},
        children=[],
        trace=[],
        origin=None,
    )
    result, _out = harness.render("mystery.docx", ir=plain_ir([unit]))

    outputs = _step(result, "inject")
    assert outputs["skipped"] == 1
    assert outputs["injected_elements"] == 0
    assert any(item.rule_id == "FR-M2-13" for item in result.findings())


def test_placeholder_residue_guard_detects_uncleared_placeholder(harness: Harness) -> None:
    """护栏的**反向**证明：把清除步骤跳过去，残留检查必须报出来。

    这才是"占位残留数 == 0"这条判据有意义的前提——一个恒返回空列表的检查器，
    和一条真正生效的检查器，输出是一样的。
    """
    ctx = RenderContext(
        pkg=open_working_copy(harness.template),
        template=harness.contract,
        profile=harness.profile,
        ir=plain_ir(),
        protected_blocks=harness.contract.blocks.blocks,
    )
    locate_regions(ctx)
    # 刻意不清占位符：页眉里的占位必须被残留检查抓到。
    residue = placeholder_residue(ctx)
    assert any("此处填写" in text for text in residue), f"残留检查漏报：{residue}"


def _step(result, name: str) -> dict:  # type: ignore[no-untyped-def]
    for step in result.steps:
        if step.step == name:
            return step.checkpoint_outputs
    raise AssertionError(f"没有名为 {name} 的步骤")
