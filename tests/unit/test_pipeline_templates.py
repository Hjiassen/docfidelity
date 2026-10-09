"""``pipeline.templates`` 接缝单测（DD-7.1 / DD-13.1）。

这一层是 ``server`` 与 ``registry`` 之间的**唯一**通道，故它的用例同时钉三件事：

① **对外形状**——服务端拿到的只有 :class:`~docfidelity.pipeline.TemplateImportResult`
   与它的几个 dataclass，一个 ``registry`` 的类型都不出现（"服务端不得认识 registry"
   这条约束的正面证据）；
② **失败姿态**——解析失败**不抛**，落在 ``register_only`` 上；只有调用方自己的问题才抛；
③ **包哈希的口径**——改版本号不改 ``package_hash``。这条最容易被后来的改动悄悄破坏
   （比如把 ``manifest.json`` 纳入摘要），所以专门有一组对立断言。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from docfidelity.contracts.errors import EngineError, ErrorCode
from docfidelity.contracts.template import ReviewOverride, TemplateOverrides
from docfidelity.pipeline import (
    BLOCK_KINDS,
    ContractSummary,
    import_template,
    read_overrides,
    read_package,
    register_package,
    summarize,
    verify_package,
    write_overrides,
)

pytestmark = pytest.mark.unit


def _override(item_id: str = "x@1", *, action: str = "specify", value: str = "v") -> ReviewOverride:
    from datetime import datetime, timezone

    return ReviewOverride(
        item_id=item_id,
        rule_id="REG-OUTLINE",
        action=action,  # type: ignore[arg-type]
        value=value if action == "specify" else None,
        operator="森",
        note=None,
        decided_at=datetime(2026, 9, 22, 9, 0, tzinfo=timezone.utc),
    )


class TestImportTemplate:
    def test_parses_a_real_template_end_to_end(self, template_docx: Path, tmp_path: Path) -> None:
        result = import_template(
            template_docx,
            import_id="i_abc",
            template_id="TPL-1",
            version="1.0",
            target_dir=tmp_path / "package",
        )
        assert result.register_only is False
        assert result.ok is True
        assert result.failed_stages() == []
        assert result.package_dir is not None
        assert result.package_hash
        assert result.integrity == [], result.integrity
        assert result.template_id == "TPL-1"
        assert result.version == "1.0"
        # 显式给编号时来源必须是 explicit（界面据此判断"登记前还要不要人补编号"）。
        assert result.template_id_source == "explicit"
        assert result.version_source == "explicit"

    def test_findings_get_ids_prefixed_by_import_id(
        self, template_docx: Path, tmp_path: Path
    ) -> None:
        """待确认项标识形如 ``<import_id>@<序号>``——"报告里第几条"这种坐标不行。"""
        result = import_template(template_docx, import_id="i_xyz", target_dir=tmp_path / "package")
        assert result.findings, "夹具模板应当产出至少一条待确认项"
        for item in result.findings:
            assert item.finding_id is not None
            assert item.finding_id.startswith("i_xyz@")

    def test_unpack_failure_degrades_instead_of_raising(self, tmp_path: Path) -> None:
        """非 docx → 只登记原件，**不抛**（"稍后重试"是一条真实存在的路径）。"""
        broken = tmp_path / "broken.docx"
        broken.write_bytes(b"not a zip at all")
        result = import_template(broken, import_id="i_bad", target_dir=tmp_path / "package")
        assert result.register_only is True
        assert result.package is None
        assert result.package_dir is None
        assert result.ok is False
        assert [stage.stage for stage in result.failed_stages()] == ["unpack"]
        failed = result.failed_stages()[0]
        assert failed.error is not None
        assert failed.error.code == ErrorCode.TEMPLATE_UNREADABLE
        # 原件快照必须拿到，否则"稍后重试"无从谈起。
        assert result.source_hash
        assert result.source_size_bytes > 0

    def test_write_failure_is_graded_not_raised(self, template_docx: Path, tmp_path: Path) -> None:
        """落盘失败走 DD-7.1 的**分级姿态**：记一条 ``package_io`` 失败，**不抛**。

        ``write_package`` 拒绝写非空目录（不静默覆盖），于是第二次写同一个目录会失败。
        这条失败不该变成一个异常——服务端据此把记录标成"契约产出来了但没落地"
        （``package_dir is None`` → 服务端的 ``parsed`` 为假），登记时自动走"仅登记原件"。
        服务端每次都写进新建的 ``imports/<新 id>/package``，故这条路径只在
        "目标目录被占"这种调用方场景出现。
        """
        target = tmp_path / "package"
        first = import_template(
            template_docx, import_id="i_1", template_id="T", version="1", target_dir=target
        )
        assert first.package_dir is not None
        second = import_template(
            template_docx, import_id="i_2", template_id="T", version="1", target_dir=target
        )
        assert second.package_dir is None
        assert second.package_hash is None
        failed = [stage for stage in second.stages if not stage.ok]
        assert [stage.stage for stage in failed] == ["package_io"]
        assert failed[0].error is not None
        assert failed[0].error.code == ErrorCode.VERSION_CONFLICT


class TestSummarize:
    def test_counts_are_pure_tallies(self, template_docx: Path, tmp_path: Path) -> None:
        result = import_template(
            template_docx,
            import_id="i_1",
            template_id="T",
            version="1",
            target_dir=tmp_path / "package",
        )
        assert result.package_dir is not None
        package = read_package(result.package_dir)
        summary = summarize(package)
        assert isinstance(summary, ContractSummary)
        assert summary.style_count == len(package.styles)
        # 块类别是闭集：这次没有的类别也要在卡上显示为 0（否则那一行会消失）。
        assert set(summary.block_counts) == set(BLOCK_KINDS)
        assert summary.table_prototypes == summary.block_counts["table"]

    def test_slot_completion_is_one_when_there_are_no_slots(self) -> None:
        """没有槽位不是"完成度为零"。返回 0.0 会让界面把一份完整的模板显示成"什么都没做"。"""
        assert ContractSummary(slot_count=0).slot_completion == 1.0
        assert ContractSummary(slot_count=4, declared_slot_count=3).slot_completion == 0.75


class TestOverrides:
    def test_absent_overrides_are_none_not_empty(self, template_docx: Path, tmp_path: Path) -> None:
        """**"没有"与"空"严格区分**：没确认过 ≠ 确认了零条。"""
        result = import_template(template_docx, import_id="i_1", target_dir=tmp_path / "package")
        assert result.package_dir is not None
        assert read_overrides(result.package_dir) is None

    def test_round_trip_and_hash_is_unaffected(self, template_docx: Path, tmp_path: Path) -> None:
        """确认留痕**不参与**包哈希:同一模板+同一决定不该因确认时刻不同而算出两个哈希。"""
        result = import_template(
            template_docx,
            import_id="i_1",
            template_id="T",
            version="1",
            target_dir=tmp_path / "package",
        )
        assert result.package_dir is not None
        before = result.package_hash
        bundle = TemplateOverrides(template_ref="T@1", entries=[_override("i_1@0")])
        write_overrides(result.package_dir, bundle)
        read_back = read_overrides(result.package_dir)
        assert read_back is not None
        assert [item.item_id for item in read_back.entries] == ["i_1@0"]
        # 哈希覆盖的是"解析出了什么"，人是后来才确认的，故哈希不变。
        assert verify_package(result.package_dir) == []
        assert read_package(result.package_dir) is not None
        assert result.package_hash == before


class TestRegisterPackage:
    def test_version_rewrite_does_not_change_package_hash(
        self, template_docx: Path, tmp_path: Path
    ) -> None:
        """**对立断言**：把版本号从 1.0 改成 2.0，包哈希必须一模一样。

        P05 第 5 步的"重复登记时选择新版本"就建立在这条性质上——若哈希随之变化，
        版本比对会把一次"改了个版本号"显示成一次内容变更。
        """
        source = import_template(template_docx, import_id="i", target_dir=tmp_path / "staging")
        assert source.package_dir is not None
        first = register_package(
            source.package_dir, tmp_path / "lib/a", template_id="T", version="1.0"
        )
        second = register_package(
            source.package_dir, tmp_path / "lib/b", template_id="T", version="2.0"
        )
        assert first.package_hash == second.package_hash
        assert first.package.manifest.version == "1.0"
        assert second.package.manifest.version == "2.0"

    def test_refuses_to_overwrite_a_non_empty_dir(
        self, template_docx: Path, tmp_path: Path
    ) -> None:
        source = import_template(template_docx, import_id="i", target_dir=tmp_path / "staging")
        assert source.package_dir is not None
        target = tmp_path / "lib/a"
        register_package(source.package_dir, target, template_id="T", version="1.0")
        with pytest.raises(EngineError) as caught:
            register_package(source.package_dir, target, template_id="T", version="1.0")
        assert caught.value.code == ErrorCode.VERSION_CONFLICT

    def test_overrides_ride_along_with_the_package(
        self, template_docx: Path, tmp_path: Path
    ) -> None:
        """登记时把留痕一起写进**库里那份**包——生成期读的是它，不是暂存那份。"""
        source = import_template(template_docx, import_id="i_1", target_dir=tmp_path / "staging")
        assert source.package_dir is not None
        bundle = TemplateOverrides(template_ref="T@1", entries=[_override("i_1@0")])
        registered = register_package(
            source.package_dir, tmp_path / "lib/a", template_id="T", version="1", overrides=bundle
        )
        assert read_overrides(registered.package_dir) is not None
