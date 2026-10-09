"""``registry`` 入库流水线与契约查询单测（DD-7.1 / DD-2.1）。

这是本轮的**端到端判据**：真实 docx → 模板包目录 → 读回 → 按槽位/样式/块查询。
它同时验证 DD-7.1 的失败分级确实生效——"仅登记原件、稍后重试"是一条真实存在
的路径，不是文档里的一句话。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from docfidelity.contracts.errors import EngineError, ErrorCode
from docfidelity.registry import STAGE_ORDER, ContractQuery, ingest_template
from docfidelity.registry.package_io import BLOCKS_PATH

pytestmark = pytest.mark.unit


@pytest.fixture
def store(tmp_path: Path) -> Path:
    return tmp_path / "templates"


@pytest.fixture
def ingested(template_docx: Path, store: Path) -> ContractQuery:
    ref = "TPL-1@1.0"
    result = ingest_template(
        template_docx,
        template_id="TPL-1",
        version="1.0",
        target_dir=store / ref,
        block_assets={"cover.png": b"\x89PNG\r\n"},
    )
    assert result.write_report is not None
    return ContractQuery(store)


class TestIngestPipeline:
    def test_stages_all_run_in_order(self, template_docx: Path) -> None:
        result = ingest_template(template_docx, template_id="T", version="1")
        assert [outcome.stage for outcome in result.stage_outcomes] == list(STAGE_ORDER)
        assert result.failed_stages() == []

    def test_unpack_failure_degrades_to_register_only(self, tmp_path: Path) -> None:
        """非 docx 必须降级成"仅登记原件、稍后重试"，而不是让整条流水线崩掉。"""
        broken = tmp_path / "broken.docx"
        broken.write_bytes(b"not a zip")
        result = ingest_template(broken)
        assert result.register_only
        assert result.package is None
        assert not result.ok
        failed = result.failed_stages()
        assert [outcome.stage for outcome in failed] == ["unpack"]
        assert failed[0].error is not None
        assert failed[0].error.code == ErrorCode.TEMPLATE_UNREADABLE

    def test_snapshot_is_kept_even_when_parsing_fails(self, tmp_path: Path) -> None:
        """原件快照必须先拿到——"稍后重试"靠的就是它，丢了就无从重试。"""
        broken = tmp_path / "broken.docx"
        broken.write_bytes(b"not a zip")
        result = ingest_template(broken)
        assert result.snapshot is not None
        assert len(result.snapshot.sha256) == 64

    def test_missing_source_is_register_only(self, tmp_path: Path) -> None:
        result = ingest_template(tmp_path / "absent.docx")
        assert result.register_only
        assert result.snapshot is None

    def test_manifest_carries_identity_and_source_hash(
        self, template_docx: Path, store: Path
    ) -> None:
        from docfidelity.platform.hashing import sha256_file

        result = ingest_template(
            template_docx, template_id="TPL-1", version="2.1", target_dir=store / "p"
        )
        assert result.package is not None
        manifest = result.package.manifest
        assert manifest.ref == "TPL-1@2.1"
        assert manifest.source_hash == sha256_file(template_docx)
        assert manifest.source_filename == "template.docx"

    def test_version_is_detected_from_the_cover(self, template_docx: Path) -> None:
        result = ingest_template(template_docx)
        assert result.identity is not None
        assert result.identity.version == "2.1"

    def test_low_confidence_identity_lands_in_the_review_queue(self, template_docx: Path) -> None:
        """模板编号来自文件名 → 必须进待确认，不能静默采用。"""
        result = ingest_template(template_docx)
        assert result.identity is not None
        assert not result.identity.is_confident
        assert result.pending_review, "低置信项必须出现在待确认队列"

    def test_no_target_dir_still_produces_the_contract(self, template_docx: Path) -> None:
        result = ingest_template(template_docx, template_id="T", version="1")
        assert result.package is not None
        assert result.write_report is None
        note = result.stage_outcomes[-1]
        assert note.stage == "package_io"
        assert note.note is not None

    def test_fingerprint_is_produced(self, template_docx: Path) -> None:
        """指纹归 TASK-P0-05，本轮起必须产出，否则"版本可比"无从谈起。"""
        result = ingest_template(template_docx, template_id="T", version="1")
        assert result.package is not None
        assert result.package.fingerprint is not None
        assert result.package.fingerprint.sections
        assert [outcome.stage for outcome in result.stage_outcomes][-2:] == [
            "fingerprint",
            "package_io",
        ]

    def test_ingest_is_deterministic_apart_from_metadata(self, template_docx: Path) -> None:
        """同一份原件两次入库必须得到同一批契约内容（时间戳只进元数据）。"""
        first = ingest_template(template_docx, template_id="T", version="1")
        second = ingest_template(template_docx, template_id="T", version="1")
        assert first.package is not None and second.package is not None
        assert first.package.page_setup == second.package.page_setup
        assert first.package.outline == second.package.outline
        assert first.package.blocks == second.package.blocks
        assert first.package.placeholders == second.package.placeholders

    def test_unexpected_errors_are_not_swallowed(
        self, template_docx: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``EngineError`` 之外的异常必须冒出来——否则代码 bug 会永远藏在待确认队列里。"""
        import docfidelity.registry.pipeline as module

        def explode(*_args: object, **_kwargs: object) -> None:
            raise TypeError("代码 bug")

        monkeypatch.setattr(module.parser, "parse_page_setup", explode)
        with pytest.raises(TypeError):
            ingest_template(template_docx, template_id="T", version="1")


class TestContractQuery:
    def test_available_lists_the_ref(self, ingested: ContractQuery) -> None:
        assert ingested.available() == ["TPL-1@1.0"]

    def test_package_loads_from_the_store(self, ingested: ContractQuery) -> None:
        assert ingested.package("TPL-1@1.0").manifest.template_id == "TPL-1"

    def test_unknown_ref_raises_template_missing(self, ingested: ContractQuery) -> None:
        with pytest.raises(EngineError) as info:
            ingested.package("NOPE@1.0")
        assert info.value.code == ErrorCode.PROFILE_TEMPLATE_MISSING

    def test_slot_lookup(self, ingested: ContractQuery) -> None:
        slot = ingested.slot("TPL-1@1.0", "c2.1")
        assert slot is not None
        assert slot.locator == "chapter[2].section[1]"

    def test_unknown_slot_returns_none(self, ingested: ContractQuery) -> None:
        assert ingested.slot("TPL-1@1.0", "c99") is None

    def test_style_lookup(self, ingested: ContractQuery) -> None:
        style = ingested.style("TPL-1@1.0", "Heading1")
        assert style is not None
        assert style.name

    def test_block_lookup_and_kind_filter(self, ingested: ContractQuery) -> None:
        block = ingested.block("TPL-1@1.0", "revision-1")
        assert block is not None
        assert block.kind == "revision"
        assert [item.block_id for item in ingested.blocks_of_kind("TPL-1@1.0", "header")] == [
            "header-default"
        ]

    def test_used_style_ids_query(self, ingested: ContractQuery) -> None:
        used = ingested.used_style_ids("TPL-1@1.0")
        assert "Heading1" in used

    def test_cache_returns_the_same_object(self, ingested: ContractQuery) -> None:
        first = ingested.package("TPL-1@1.0")
        assert ingested.package("TPL-1@1.0") is first

    def test_forget_drops_the_cache(self, ingested: ContractQuery) -> None:
        first = ingested.package("TPL-1@1.0")
        ingested.forget("TPL-1@1.0")
        assert ingested.package("TPL-1@1.0") is not first

    def test_query_works_after_the_source_is_deleted(
        self, template_docx: Path, store: Path, tmp_path: Path
    ) -> None:
        """决策二：生成期只查契约。

        入库完成后把模板原件删掉，查询必须照常工作——如果查询链路里还留着
        "顺手打开原件看一眼"的路径，这里就会失败。
        """
        local = tmp_path / "source.docx"
        local.write_bytes(template_docx.read_bytes())
        ref = "TPL-9@1.0"
        ingest_template(local, template_id="TPL-9", version="1.0", target_dir=store / ref)
        local.unlink()

        query = ContractQuery(store)
        assert query.outline(ref).chapter_tree
        assert query.slot(ref, "c1") is not None
        assert query.style(ref, "Heading1") is not None

    def test_verify_detects_a_removed_block_index(
        self, ingested: ContractQuery, store: Path
    ) -> None:
        from docfidelity.registry import verify_package

        (store / "TPL-1@1.0" / BLOCKS_PATH).unlink()
        assert verify_package(store / "TPL-1@1.0") != []
