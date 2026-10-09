"""``registry.package_io`` 单测（DD-7.1 ``package_io`` / ``register``）。

核心的"防假通过"断言有两条：

1. **``package_hash`` 不覆盖清单**——如果覆盖了，就会自指，于是这份哈希既算不出来
   也校验不了。测试用"改清单备注后仍能读回"来证明它确实被排除在外。
2. **改一个字节就必须被拒绝**——只断言"能读回自己刚写的东西"是拦不住
   "包被外部改过却照样加载"的。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from docfidelity.contracts.errors import EngineError, ErrorCode
from docfidelity.contracts.template import TemplateFingerprint, TemplatePackage
from docfidelity.platform.hashing import canonical_json
from docfidelity.registry import package_io
from docfidelity.registry.pipeline import ingest_template

pytestmark = pytest.mark.unit


@pytest.fixture
def package(template_docx: Path) -> TemplatePackage:
    result = ingest_template(template_docx, template_id="TPL-1", version="1.0")
    assert result.package is not None
    return result.package


@pytest.fixture
def written(package: TemplatePackage, tmp_path: Path) -> Path:
    return package_io.write_package(package, tmp_path / "pkg").target_dir


class TestWritePackage:
    def test_writes_every_required_file(self, written: Path) -> None:
        for relative in package_io.REQUIRED_PATHS:
            assert (written / relative).is_file(), f"缺少 {relative}"

    def test_contract_files_are_canonical_json(self, written: Path) -> None:
        """落盘一律走确定性序列化，否则同一份契约在两次写出之间就会有字节差异。"""
        for relative in package_io.HASHED_PATHS:
            payload = (written / relative).read_bytes()
            assert payload == canonical_json(json.loads(payload.decode("utf-8")))

    def test_manifest_hash_is_filled_in(self, written: Path) -> None:
        manifest = json.loads((written / package_io.MANIFEST_PATH).read_text("utf-8"))
        assert len(manifest["package_hash"]) == 64

    def test_no_temp_files_left_behind(self, written: Path) -> None:
        leftovers = [p.name for p in written.rglob("*") if p.name.endswith(".dftmp")]
        assert leftovers == []

    def test_existing_package_is_a_conflict_not_an_overwrite(
        self, package: TemplatePackage, tmp_path: Path
    ) -> None:
        """同 template_id@version 已存在时**不得**静默覆盖。"""
        package_io.write_package(package, tmp_path / "pkg")
        with pytest.raises(EngineError) as info:
            package_io.write_package(package, tmp_path / "pkg")
        assert info.value.code == ErrorCode.VERSION_CONFLICT
        assert info.value.remediation, "冲突必须带可选动作，界面据此让使用者选择"

    def test_empty_existing_directory_is_usable(
        self, package: TemplatePackage, tmp_path: Path
    ) -> None:
        """被提前创建出来的空目录不该算冲突。"""
        target = tmp_path / "pkg"
        target.mkdir()
        assert package_io.write_package(package, target).package_hash

    def test_file_in_the_way_is_reported_as_conflict(
        self, package: TemplatePackage, tmp_path: Path
    ) -> None:
        blocker = tmp_path / "pkg"
        blocker.write_text("not a directory")
        with pytest.raises(EngineError) as info:
            package_io.write_package(package, blocker)
        assert info.value.code == ErrorCode.VERSION_CONFLICT


class TestReadPackage:
    def test_round_trip_preserves_contracts(self, package: TemplatePackage, written: Path) -> None:
        loaded = package_io.read_package(written)
        assert loaded.manifest.ref == package.manifest.ref
        assert [style.style_id for style in loaded.styles] == [
            style.style_id for style in package.styles
        ]
        assert loaded.page_setup.page_size == package.page_setup.page_size
        assert [node.title for node in loaded.outline.chapter_tree] == [
            node.title for node in package.outline.chapter_tree
        ]

    def test_round_trip_preserves_blocks_and_placeholders(
        self, package: TemplatePackage, written: Path
    ) -> None:
        loaded = package_io.read_package(written)
        assert [block.block_id for block in loaded.blocks.blocks] == [
            block.block_id for block in package.blocks.blocks
        ]
        assert [entry.text for entry in loaded.placeholders.entries] == [
            entry.text for entry in package.placeholders.entries
        ]

    def test_missing_file_raises_incomplete(self, written: Path) -> None:
        (written / package_io.OUTLINE_PATH).unlink()
        with pytest.raises(EngineError) as info:
            package_io.read_package(written)
        assert info.value.code == ErrorCode.PACKAGE_INCOMPLETE
        assert package_io.OUTLINE_PATH in info.value.where["missing"]

    def test_every_missing_file_is_listed(self, written: Path) -> None:
        """一次报出全部缺件，使用者才不用"修一个、再跑一次、再发现一个"。"""
        (written / package_io.STYLES_PATH).unlink()
        (written / package_io.BLOCKS_PATH).unlink()
        with pytest.raises(EngineError) as info:
            package_io.read_package(written)
        assert set(info.value.where["missing"]) == {
            package_io.STYLES_PATH,
            package_io.BLOCKS_PATH,
        }

    def test_tampered_contract_is_detected(self, written: Path) -> None:
        """改一个字节就必须被拒绝——否则"包被外部改过"会静默通过。"""
        target = written / package_io.PAGE_SETUP_PATH
        payload = json.loads(target.read_text("utf-8"))
        payload["margins"]["top_twips"] = 9999
        target.write_bytes(canonical_json(payload))
        with pytest.raises(EngineError) as info:
            package_io.read_package(written)
        assert info.value.code == ErrorCode.PACKAGE_INCOMPLETE
        assert "摘要" in info.value.message

    def test_manifest_edits_do_not_break_the_hash(self, written: Path) -> None:
        """清单不参与 package_hash（否则自指）。

        改备注不该让包变成"损坏"，否则任何一次整理清单都会让全库无法加载。
        """
        target = written / package_io.MANIFEST_PATH
        payload = json.loads(target.read_text("utf-8"))
        payload["notes"] = "人工补注"
        target.write_bytes(canonical_json(payload))
        assert package_io.read_package(written).manifest.notes == "人工补注"

    def test_unparsable_file_is_reported(self, written: Path) -> None:
        (written / package_io.NUMBERING_PATH).write_bytes(b"{not json")
        with pytest.raises(EngineError) as info:
            package_io.read_package(written)
        assert info.value.code == ErrorCode.PACKAGE_INCOMPLETE

    def test_fingerprint_is_optional(self, package: TemplatePackage, tmp_path: Path) -> None:
        """没有指纹的包仍必须能读回来。

        P0-04 期间产出的包就没有 ``fingerprint.json``（指纹归 P0-05）。旧包不该因为
        "当时还没算指纹"就变成读不了——那会让整个模板库在升级后失效。
        """
        without = package.model_copy(update={"fingerprint": None})
        written = package_io.write_package(without, tmp_path / "pkg").target_dir
        assert not (written / package_io.FINGERPRINT_PATH).exists()
        assert package_io.read_package(written).fingerprint is None

    def test_fingerprint_is_read_back_when_present(
        self, package: TemplatePackage, tmp_path: Path
    ) -> None:
        fingerprint = TemplateFingerprint(digest="a" * 64, sections={"styles": "b" * 64})
        stamped = package.model_copy(update={"fingerprint": fingerprint})
        written = package_io.write_package(stamped, tmp_path / "pkg").target_dir
        loaded = package_io.read_package(written)
        assert loaded.fingerprint is not None
        assert loaded.fingerprint.digest == "a" * 64


class TestPackageHash:
    def test_hash_is_stable_across_writes(self, package: TemplatePackage, tmp_path: Path) -> None:
        first = package_io.write_package(package, tmp_path / "a").package_hash
        second = package_io.write_package(package, tmp_path / "b").package_hash
        assert first == second

    def test_block_assets_participate_in_the_hash(
        self, package: TemplatePackage, tmp_path: Path
    ) -> None:
        """换了一张封面图也是内容变更，必须改变摘要。"""
        without = package_io.write_package(package, tmp_path / "a").package_hash
        with_asset = package_io.write_package(
            package, tmp_path / "b", block_assets={"cover.png": b"\x89PNG"}
        ).package_hash
        assert without != with_asset

    def test_block_asset_round_trips(self, package: TemplatePackage, tmp_path: Path) -> None:
        written = package_io.write_package(
            package, tmp_path / "pkg", block_assets={"cover.png": b"\x89PNG"}
        ).target_dir
        assert (written / package_io.BLOCK_DIR / "cover.png").read_bytes() == b"\x89PNG"
        assert package_io.read_package(written).manifest.ref == package.manifest.ref

    def test_source_hash_matches_the_original_bytes(
        self, written: Path, template_docx: Path
    ) -> None:
        from docfidelity.platform.hashing import sha256_file

        manifest = package_io.read_package(written).manifest
        assert manifest.source_hash == sha256_file(template_docx)


class TestVerifyPackage:
    def test_clean_package_has_no_problems(self, written: Path) -> None:
        assert package_io.verify_package(written) == []

    def test_problems_are_returned_instead_of_raised(self, written: Path) -> None:
        """预检要一次呈现全部问题，而不是在第一个问题上抛异常。"""
        (written / package_io.PLACEHOLDERS_PATH).unlink()
        problems = package_io.verify_package(written)
        assert any("placeholders.json" in problem for problem in problems)

    def test_tampering_is_reported(self, written: Path) -> None:
        (written / package_io.STYLES_PATH).write_bytes(b"[]")
        assert package_io.verify_package(written) != []

    def test_broken_manifest_is_reported_not_raised(self, written: Path) -> None:
        (written / package_io.MANIFEST_PATH).write_bytes(b"nonsense")
        problems = package_io.verify_package(written)
        assert any("清单" in problem for problem in problems)
