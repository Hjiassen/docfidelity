"""``render`` 目录重建与域刷新（TASK-P0-10 的验收判据「目录可用」）。

判据「目录可用」不是"目录里有一行字"，而是**这三件事同时成立**：

1. 目录条目与**实际产出的标题**一一对应（不是模板示例标题的残留）；
2. 每个条目的超链接 / ``PAGEREF`` 域指向一个**正文里真的存在**的书签——
   否则 Word 刷新后页码全空、点击不跳转；
3. 包裹整块的 ``TOC`` 域字符还在。丢了它，目录退化成静态文本：页面上看不出
   差别，但 Word 再也不会重算页码。

第 2 条是本轮最容易被假通过的一条：只断言"条目文本对"的实现，在**没有创建任何
书签**时同样能过。故这里配了**对立断言**——正文 ``_Toc`` 书签数必须等于标题数，
且每个 ``PAGEREF`` 名字都必须在其中。

``ooxml.fields`` / ``ooxml.refresh`` 的域原语（``rebuild_toc`` / ``bind_missing_toc_bookmarks`` /
``refresh_with_office`` / ``degrade_note``）在 P0-03 落地后一直没有测试覆盖，
本轮经渲染链路把它们真正跑起来——这也是这份用例的另一半职责。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import pytest

from docfidelity.contracts.profile import DocTypeProfile
from docfidelity.contracts.template import BlockIndex, TemplatePackage
from docfidelity.ooxml import (
    DOCUMENT_PART,
    DocxPackage,
    attr,
    find_first,
    iter_elements,
    localname,
)
from docfidelity.render import (
    RenderPolicy,
    TocPolicy,
    ensure_toc_bookmarks,
    open_working_copy,
    render,
)
from docfidelity.render.toc import (
    REFRESH_RULE_ID,
    TOC_RULE_ID,
    heading_paragraphs,
    rebuild_toc_entries,
    toc_blocks,
)
from tests.fixtures.docx_builder import png_bytes
from tests.fixtures.render_fixture import (
    chapter_ir,
    ingest_contract,
    make_context,
    plain_ir,
    template_docx_path,
)

pytestmark = pytest.mark.unit

#: 目录条目里的页码域代码（``PAGEREF _Toc00000001 \h``）。
_PAGEREF_RE = re.compile(r"PAGEREF\s+(\S+)")


@dataclass(frozen=True)
class Entry:
    """一个目录条目的可断言侧面。"""

    title: str
    page_refs: list[str]
    links: list[str | None]


@dataclass(frozen=True)
class Harness:
    template: Path
    contract: TemplatePackage
    profile: DocTypeProfile
    out_dir: Path

    def render(self, name: str = "toc.docx", **kwargs: object):  # type: ignore[no-untyped-def]
        policy = kwargs.pop("policy", None)
        ir = kwargs.pop("ir", None)
        # 缺省钉死 "本机没有 Word"，理由同 test_render_pipeline：留在 ``None`` 上会让
        # 每个用例都去探测本机、并真起一个 WINWORD.EXE。要验"有 Word"那两条分支的用例
        # 自己显式传 ``office=True``（并 monkeypatch 掉真刷新），它们的意图不受影响。
        office = kwargs.pop("office", False)
        office_reason = kwargs.pop("office_reason", None)
        contract = kwargs.pop("contract", None)
        asset_loader = kwargs.pop("asset_loader", None)
        assert not kwargs, f"未识别的参数：{sorted(kwargs)}"
        assert policy is None or isinstance(policy, RenderPolicy)
        out = self.out_dir / name
        return (
            render(
                pkg=open_working_copy(self.template),
                template=contract if contract is not None else self.contract,
                profile=self.profile,
                ir=ir if ir is not None else plain_ir(),  # type: ignore[arg-type]
                out_path=out,
                policy=policy,  # type: ignore[arg-type]
                office=office,  # type: ignore[arg-type]
                office_reason=office_reason,  # type: ignore[arg-type]
                asset_loader=asset_loader,  # type: ignore[arg-type]
            ),
            out,
        )


@pytest.fixture(scope="module")
def harness(tmp_path_factory: pytest.TempPathFactory) -> Harness:
    template = template_docx_path(tmp_path_factory.mktemp("toc"))
    contract, profile = ingest_contract(template)
    return Harness(
        template=template,
        contract=contract,
        profile=profile,
        out_dir=tmp_path_factory.mktemp("toc-out"),
    )


# ------------------------------------------------------------------ 读产物
def _toc_paragraphs(path: Path) -> list[object]:
    """目录控件（``w:sdt``）里的条目段落。"""
    with DocxPackage.open(path, read_only=True) as pkg:
        root = pkg.part(DOCUMENT_PART).read_tree
        controls = list(iter_elements(root, "w:sdt"))
    assert len(controls) == 1, f"夹具应只有一个目录控件，实际 {len(controls)} 个"
    content = find_first(controls[0], "w:sdtContent")
    assert content is not None, "目录控件缺少 w:sdtContent"
    return [child for child in content if localname(child) == "p"]


def _entries(path: Path) -> list[Entry]:
    entries: list[Entry] = []
    for paragraph in _toc_paragraphs(path):
        instructions = [node.text or "" for node in iter_elements(paragraph, "w:instrText")]
        page_refs = [
            match.group(1)
            for match in (_PAGEREF_RE.search(item) for item in instructions)
            if match is not None
        ]
        texts = [node.text or "" for node in iter_elements(paragraph, "w:t")]
        links = [attr(node, "w:anchor") for node in iter_elements(paragraph, "w:hyperlink")]
        entries.append(Entry(title=texts[0] if texts else "", page_refs=page_refs, links=links))
    return entries


def _count(path: Path, tag: str) -> int:
    with DocxPackage.open(path, read_only=True) as pkg:
        root = pkg.part(DOCUMENT_PART).read_tree
    return len(list(iter_elements(root, tag)))


def _body_toc_bookmarks(path: Path) -> list[str]:
    """正文里的 ``_Toc`` 书签名（文档序）——目录锚点的落点。"""
    with DocxPackage.open(path, read_only=True) as pkg:
        root = pkg.part(DOCUMENT_PART).read_tree
    names = [(attr(node, "w:name") or "") for node in iter_elements(root, "w:bookmarkStart")]
    return [name for name in names if name.startswith("_Toc")]


def _has_update_fields(path: Path) -> bool:
    with DocxPackage.open(path, read_only=True) as pkg:
        part = pkg.find_part("word/settings.xml")
        assert part is not None, "模板缺少 word/settings.xml"
        root = part.read_tree
    node = find_first(root, "w:updateFields")
    return node is not None and attr(node, "w:val") == "true"


def _toc_field_depths(path: Path) -> list[int]:
    """目录控件内每个条目段落**结束时**的域嵌套深度（只数 ``w:fldChar``）。"""
    depths: list[int] = []
    depth = 0
    for paragraph in _toc_paragraphs(path):
        for node in iter_elements(paragraph, "w:fldChar"):
            kind = attr(node, "w:fldCharType")
            if kind == "begin":
                depth += 1
            elif kind == "end":
                depth -= 1
        depths.append(depth)
    return depths


def _finding_ids(result) -> list[str]:  # type: ignore[no-untyped-def]
    return [item.rule_id for item in result.findings()]


def _messages(result, rule_id: str) -> list[str]:  # type: ignore[no-untyped-def]
    return [item.message for item in result.findings() if item.rule_id == rule_id]


def _step(result, name: str) -> dict:  # type: ignore[no-untyped-def]
    for step in result.steps:
        if step.step == name:
            return step.checkpoint_outputs
    raise AssertionError(f"没有名为 {name} 的步骤")


# ------------------------------------------------------------------ 目录可用
def test_toc_entries_match_injected_headings(harness: Harness) -> None:
    """目录条目 = 实际产出的标题（文档序、层级样式来自模板契约）。"""
    ir = chapter_ir(with_table=True, with_subheading=True)
    result, out = harness.render(ir=ir)

    assert result.written
    assert [entry.title for entry in _entries(out)] == ["概述", "范围", "附录"]
    payload = _step(result, "toc")
    assert payload["rebuilt"] is True
    assert payload["entries"] == 3
    assert payload["headings_total"] == 3
    assert payload["created"] == 1, "模板只有 2 个条目原型，第 3 条是新建的"


def test_every_toc_anchor_resolves_to_a_heading_bookmark(harness: Harness) -> None:
    """判据的核心：目录的每一个锚点都落在正文里真实存在的书签上。

    对立断言（防假通过）：正文 ``_Toc`` 书签数必须**等于**标题数。把
    ``bind_missing_toc_bookmarks`` 变成空操作，本用例必然失败——那时锚点名与书签集
    完全不相交，而这正是"目录看着有、点不动、页码刷新后全空"的形态。
    """
    result, out = harness.render(ir=chapter_ir(with_subheading=True))
    assert result.written

    entries = _entries(out)
    bookmarks = _body_toc_bookmarks(out)
    assert len(bookmarks) == len(entries) == 2

    resolved: list[str] = []
    for entry in entries:
        assert len(entry.page_refs) == 1, f"条目 {entry.title!r} 的 PAGEREF 数不为 1"
        anchor = entry.page_refs[0]
        assert entry.links == [anchor], "超链接锚点与 PAGEREF 域代码必须指向同一个书签"
        assert anchor in bookmarks, f"锚点 {anchor} 在正文里不存在"
        resolved.append(anchor)
    # 顺序也要对：条目 i 指向第 i 个标题的书签（错位时页码会互相串）。
    assert resolved == bookmarks

    payload = _step(result, "toc")
    assert payload["bookmarks_created"] == 2
    assert payload["bookmarks_bound"] == 2


def test_toc_field_survives_rebuild(harness: Harness) -> None:
    """``TOC`` 域字符必须保住，否则目录退化成静态文本（DD-9.6）。"""
    result, out = harness.render(ir=chapter_ir(with_subheading=True))
    assert result.written

    with DocxPackage.open(out, read_only=True) as pkg:
        root = pkg.part(DOCUMENT_PART).read_tree
        control = next(iter(iter_elements(root, "w:sdt")))
        instructions = [node.text or "" for node in iter_elements(control, "w:instrText")]
        markers = [attr(node, "w:fldCharType") for node in iter_elements(control, "w:fldChar")]

    assert any(item.startswith(" TOC") for item in instructions), "TOC 域代码丢失"
    assert "begin" in markers and "end" in markers, "TOC 域字符不配对"
    assert markers.count("begin") == markers.count("end"), markers
    assert _step(result, "toc")["field_preserved"] is True


def test_fewer_headings_than_template_entries_drops_the_surplus(harness: Harness) -> None:
    """IR 标题比模板目录条目少时：删多余条目、**只**把 TOC 域的 end 搬过来。

    搬域时把被删条目的 ``PAGEREF`` 域一起搬走是错的——它会让第一个目录项后面
    跟着两个页码。故断言 ``PAGEREF`` 总数恰好等于条目数（无遗留、无重复）。
    """
    result, out = harness.render(ir=plain_ir())
    assert result.written

    entries = _entries(out)
    assert [entry.title for entry in entries] == ["概述"]
    assert len(entries[0].page_refs) == 1, "被删条目的 PAGEREF 域被误搬到了存活条目上"

    payload = _step(result, "toc")
    assert payload["rewritten_in_place"] == 1
    assert payload["created"] == 0
    assert payload["hoisted_field_runs"] == 1, "只需搬走关闭 TOC 域的那一个运行"
    assert payload["field_preserved"] is True


def test_created_entries_do_not_duplicate_the_block_field(harness: Harness) -> None:
    """新建条目**不得**把外层 TOC 域的 ``begin`` / ``end`` / 指令一起复制过来。

    新条目只能拿模板已有的条目当原型，而第一条带着 TOC 域的 ``begin``＋指令、
    最后一条带着它的 ``end``。直接深拷贝原型当新条目，产物里就会出现两个
    ``TOC \\o "1-3"`` 指令与两组不配对的域字符——目录的域结构当场损坏，而页面上
    只是"多了个条目"，看不出异常。故断言整块里**恰好一条** TOC 指令、
    ``begin``/``end`` 数量相等、``PAGEREF`` 数恰好等于条目数。
    """
    result, out = harness.render(
        ir=chapter_ir(with_table=True, with_figure=True, with_subheading=True),
        asset_loader=lambda _ref: png_bytes(240, 160),
    )
    assert result.written
    assert _step(result, "toc")["created"] == 2, "本用例要走到'新建条目'这条路径"

    with DocxPackage.open(out, read_only=True) as pkg:
        root = pkg.part(DOCUMENT_PART).read_tree
        control = next(iter(iter_elements(root, "w:sdt")))
        instructions = [node.text or "" for node in iter_elements(control, "w:instrText")]
        markers = [attr(node, "w:fldCharType") for node in iter_elements(control, "w:fldChar")]

    assert sum(1 for item in instructions if item.strip().startswith("TOC")) == 1, instructions
    assert sum(1 for item in instructions if item.strip().startswith("PAGEREF")) == 4, instructions
    assert markers.count("begin") == markers.count("end"), markers
    assert len(_entries(out)) == 4


def test_toc_field_envelope_spans_every_created_entry(harness: Harness) -> None:
    """``TOC`` 域必须**夹住整份条目**：``begin`` 在首条、``end`` 落在末条。

    新建条目插在"带 ``end`` 的那条"之后。若 ``end`` 不跟着挪，域的结果就只覆盖到
    第 ``reused`` 条，新增的每一条都落在域外。产物在页面上看不出异常（条目一应
    俱全），但**用户在 Word 里更新目录会得到一份重复的目录**：域内的条目被整份
    重建，域外那几条原样留着，两份叠在一起。

    故断言末条之前深度恒大于零、末条归零——这正是"域夹住整块"的定义。
    """
    result, out = harness.render(ir=chapter_ir(with_table=True, with_subheading=True))
    assert result.written
    assert _step(result, "toc")["created"] == 1, "本用例要走到'新建条目'这条路径"

    depths = _toc_field_depths(out)
    assert len(depths) == 3
    assert depths[:-1] == [1, 1], f"末条之前 TOC 域必须一直开着，实际深度 {depths}"
    assert depths[-1] == 0, f"TOC 域必须收在最后一条，实际深度 {depths}"


def test_toc_field_envelope_holds_when_the_skeleton_shrinks(harness: Harness) -> None:
    """条目比模板原型**少**时，``end`` 必须随"搬域"落到存活的末条上（反向路径）。"""
    result, out = harness.render(ir=plain_ir())
    assert result.written
    assert _step(result, "toc")["hoisted_field_runs"] == 1, "本用例要走到'搬域'这条路径"

    depths = _toc_field_depths(out)
    assert len(depths) == 1
    assert depths == [0], f"TOC 域必须收在唯一的存活条目上，实际深度 {depths}"


def test_rebuild_is_idempotent_across_builds(harness: Harness) -> None:
    """两次构建的目录条目/书签完全一致（书签名与 ``w:id`` 都要确定性）。

    书签 ``w:id`` 从包内现有取值续编——若写成"从 1 开始"，第二次构建就会与第一次
    不同，幂等（NFR-02）当场失效。
    """
    ir = chapter_ir(with_subheading=True)
    first, out_a = harness.render("idem-a.docx", ir=ir)
    second, out_b = harness.render("idem-b.docx", ir=ir)
    assert first.written and second.written
    assert first.package_hash == second.package_hash
    assert [(e.title, e.page_refs) for e in _entries(out_a)] == [
        (e.title, e.page_refs) for e in _entries(out_b)
    ]
    assert _body_toc_bookmarks(out_a) == _body_toc_bookmarks(out_b)


def test_update_fields_is_requested(harness: Harness) -> None:
    """置 ``w:updateFields``：否则产物显示的是模板的旧页码（**错的**而不是缺的）。"""
    result, out = harness.render()
    assert result.written
    assert _has_update_fields(out), "没有置 w:updateFields，Word 不会自动刷新页码"
    assert "word/settings.xml" in _step(result, "toc")["parts_touched"]


# -------------------------------------------------------------- 降级与护栏
def test_empty_outline_keeps_the_template_toc(harness: Harness) -> None:
    """IR 没有标题时**不清空**目录：清空会连 TOC 域一起毁掉，宁可留着并报警。"""
    result, out = harness.render(ir=plain_ir([]))
    assert result.written

    payload = _step(result, "toc")
    assert payload["rebuilt"] is False
    assert payload["skipped_reason"] == "empty-outline"
    assert _count(out, "w:sdt") == 1, "目录控件被清掉了"
    assert _messages(result, TOC_RULE_ID), "空骨架必须留下一条判定"


def test_missing_toc_block_is_reported_not_fatal(harness: Harness) -> None:
    """模板没有目录块：报一条 major 并跳过，**构建照常成功**（硬规则 9）。"""
    stripped = harness.contract.model_copy(
        update={
            "blocks": BlockIndex(
                blocks=[b for b in harness.contract.blocks.blocks if b.kind != "toc"]
            )
        }
    )
    result, out = harness.render(contract=stripped, ir=chapter_ir())

    assert result.written, "缺目录块不该让整次构建失败"
    assert result.guardrail_blockers == []
    payload = _step(result, "toc")
    assert payload["rebuilt"] is False and payload["skipped_reason"] == "no-toc-block"
    assert _messages(result, TOC_RULE_ID), "缺目录块必须报出来"


def test_headings_beyond_toc_depth_are_filtered_and_reported(harness: Harness) -> None:
    """超出目录域声明深度的标题不进目录，但必须报出来——那是静默丢内容。"""
    policy = RenderPolicy(toc=TocPolicy(max_depth=1))
    result, out = harness.render(ir=chapter_ir(with_subheading=True), policy=policy)
    assert result.written

    assert [entry.title for entry in _entries(out)] == ["概述"], "超出深度的标题不该进目录"
    payload = _step(result, "toc")
    assert payload["max_depth"] == 1
    assert payload["headings_beyond_depth"] == 1
    assert any("超过目录域声明的深度" in message for message in _messages(result, TOC_RULE_ID))


def test_refresh_degrades_without_office(harness: Harness) -> None:
    """无 Word 环境：**降级而非失败**（NFR-05 / FR-M7-12），产物照样写盘。"""
    result, out = harness.render(office=False)
    assert result.written and out.is_file()

    payload = _step(result, "refresh")
    assert payload["needed"] is True
    assert payload["refreshed"] is False
    assert payload["skipped_reason"] == "office-unavailable"
    assert payload["note"], "降级必须给出人读提示"
    assert REFRESH_RULE_ID in _finding_ids(result), "降级必须留下一条判定"
    # 降级不是 blocker：目录页码过期不影响文档可用。
    assert [item.rule_id for item in result.blockers()] == []


def test_refresh_marks_a_deliberate_shutdown_apart_from_a_missing_word(
    harness: Harness,
) -> None:
    """``office=False`` 且给了原因 ⇒ ``office-disabled``，提示里带那句原因。

    与"本机没装 Word"（``office-unavailable``）分开是必需的：前者去查配置，后者去查
    部署环境。合成一句话会让人往错的方向排查，而症状（页码是占位值）两者完全一样。
    """
    reason = "确定性姿态已关闭本机 Office 参与"
    result, _out = harness.render("disabled.docx", office=False, office_reason=reason)

    payload = _step(result, "refresh")
    assert payload["skipped_reason"] == "office-disabled"
    assert reason in str(payload["note"]), "提示里没有说出关闭的原因"


def test_refresh_without_a_reason_still_reads_as_not_probed(harness: Harness) -> None:
    """``office=False`` 但**没给原因** ⇒ 仍是 ``office-unavailable``。

    对照组：不能因为"系统里有地方能关掉它"就把所有不可用都记成关掉的——探测不到
    与被人关掉是两种事实，缺了原因就只配说前者。
    """
    result, _out = harness.render("no-reason.docx", office=False)

    assert _step(result, "refresh")["skipped_reason"] == "office-unavailable"


def test_refresh_succeeds_when_office_is_available(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """有 Word 且刷新成功：不报降级判定，且产物摘要从磁盘重算。"""
    monkeypatch.setattr("docfidelity.render.toc.refresh_with_office", lambda _path: True)
    result, _out = harness.render("refreshed.docx", office=True)

    assert result.written
    payload = _step(result, "refresh")
    assert payload["attempted"] is True and payload["refreshed"] is True
    assert REFRESH_RULE_ID not in _finding_ids(result), "刷新成功却报了降级"
    assert result.package_hash is not None and len(result.package_hash) == 64


def test_refresh_reports_failure_when_office_call_fails(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """有 Word 但刷新失败：同样降级——``refresh_with_office`` 返回 False 而不抛异常。"""
    monkeypatch.setattr("docfidelity.render.toc.refresh_with_office", lambda _path: False)
    result, _out = harness.render("failed-refresh.docx", office=True)

    assert result.written
    payload = _step(result, "refresh")
    assert payload["attempted"] is True and payload["refreshed"] is False
    assert payload["skipped_reason"] == "refresh-failed"
    assert REFRESH_RULE_ID in _finding_ids(result)


def test_write_guardrail_skips_refresh_without_a_finding(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """写盘被护栏拦下时**没有文件可刷新**：登记 no-artifact，且不报降级判定。

    此时说"目录需在 Word 中刷新"是句空话——产物根本不存在。
    """
    from docfidelity.render import pipeline

    monkeypatch.setattr(pipeline, "validate_style_refs", lambda _pkg, _styles: ["Missing"])
    result, out = harness.render("blocked-toc.docx")

    assert not result.written and not out.exists()
    payload = _step(result, "refresh")
    assert payload["skipped_reason"] == "no-artifact"
    assert REFRESH_RULE_ID not in _finding_ids(result)


# ------------------------------------------------------------ 单步可独立调用
def test_steps_are_independently_callable(harness: Harness) -> None:
    """每一步都能单独调用（DD-9.1 扩展点 / 验收 25）。"""
    ctx = make_context(harness.template, ir=chapter_ir(with_subheading=True))
    assert len(toc_blocks(ctx)) == 1, "契约里应恰好登记一个目录块"
    # 还没注入：模板自带的示例标题（概述 / 范围 / 附录）仍带 ``_Toc`` 书签，
    # 故"补齐"在这里必须是**幂等**的空操作。
    assert len(heading_paragraphs(ctx)) == 3
    assert ensure_toc_bookmarks(ctx) == 0, "已有 _Toc 书签的标题不该重复挂"

    report = rebuild_toc_entries(ctx)
    assert report.rebuilt and report.entries == 2
    assert report.bookmarks_created == 0
    assert report.bookmarks_bound == 2
    # ``toc_field_levels`` 从域代码读出声明深度（夹具是 ``TOC \o "1-3"``）。
    assert report.max_depth == 3


# ------------------------------------------------------- 条目克隆不得带走版块级分页
def test_created_entry_does_not_inherit_the_block_page_break(harness: Harness) -> None:
    """新建的目录条目不得继承目录**版块级**的 ``pageBreakBefore``。

    目录块的第一条带着"目录另起一页"的分页（渲染层第 9 步落在受保护区首段上），
    而它恰好也是新建条目的深拷贝原型。不摘掉这道分页，产物表面只是"多了一个条目"，
    实际却是每个新条目各占一页，而且分页点落在白名单之外——门禁会据此判
    FR-M5-04 阻断。故这里钉住"整段目录里只有第一条带分页"。
    """
    ir = chapter_ir(with_table=True, with_subheading=True)
    result, out = harness.render("toc-created-entry.docx", ir=ir)
    assert result.written
    payload = _step(result, "toc")
    assert payload["created"] == 1, "夹具里应有且只有一个新建条目"

    paragraphs = _toc_paragraphs(out)
    assert len(paragraphs) == 3
    flagged = [
        index
        for index, paragraph in enumerate(paragraphs)
        if find_first(find_first(paragraph, "w:pPr"), "w:pageBreakBefore") is not None
    ]
    assert flagged == [0], f"只有目录第一条可以带分页，实际带分页的是 {flagged}"


def test_toc_block_page_break_is_still_applied(harness: Harness) -> None:
    """修掉克隆继承之后，"目录另起一页"这道**版块级**分页本身必须还在。"""
    _result, out = harness.render("toc-block-break.docx", ir=chapter_ir(with_subheading=True))
    first = _toc_paragraphs(out)[0]
    assert find_first(find_first(first, "w:pPr"), "w:pageBreakBefore") is not None
