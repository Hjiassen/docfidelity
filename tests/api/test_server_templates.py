"""模板入库用例与端点（P05 第 1~5 步；DD-7.1 / DD-13.2 的 ``templates/*``）。

三条验收口径来自需求 P05 的"状态与空态"，各自对应一组用例：

① **解析失败 → 可执行动作"仅登记原件、稍后重试"**（不阻塞流程）：``register_only``
   为真、``parsed`` 为假，登记仍然成功但明确标注"未解析"；
② **待确认项为 0 → 跳过第 4 步**：``pending`` 为空即可直接登记，不制造无意义的人工步骤；
③ **重复登记（同 ``template_id@version``）→ 提示选择新版本或覆盖，不静默覆盖**：
   内容不同时报 ``E_VERSION_CONFLICT`` 并带出旧/新哈希；内容**相同**时按幂等处理
   （没有"覆盖"这回事，因为根本没有东西被换掉）。

另有一组"两台机器上都会过"的对立断言：登记的元数据**两种仓储**给出同一结果
（``repo`` 夹具参数化）——协议不变、上层一行未改，等价才是被验证的事实。

模板库那一半（详情 / 版本对比 / 废弃 / 导出）在 ``test_server_template_library.py``
——与 ``server/templates.py`` / ``server/template_library.py`` 同一道分界线。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from docfidelity.contracts.errors import EngineError, ErrorCode
from docfidelity.pipeline import read_overrides, read_package
from server.main import create_app
from server.settings import ServerSettings
from server.storage import MemoryRepository, RegisteredTemplate, Repository
from server.templates import ReviewDecision
from tests.fixtures.server_fixture import Harness, build_harness, build_registry

pytestmark = pytest.mark.api

#: 夹具模板文件名（``conftest`` 里那份 session 级夹具用它命名）。
TEMPLATE_NAME = "template.docx"


@pytest.fixture
def harness(tmp_path: Path, repo: Repository) -> Harness:
    return build_harness(tmp_path, repository=repo)


def expect_error(code: str, fn: Callable[[], object]) -> EngineError:
    with pytest.raises(EngineError) as caught:
        fn()
    assert caught.value.code == code, caught.value.message
    return caught.value


def _import(
    harness: Harness,
    template_bytes: bytes,
    *,
    template_id: str | None = "TPL-1",
    version: str | None = "1.0",
    **kwargs: object,
):
    return harness.services.import_template(
        filename=TEMPLATE_NAME,
        payload=template_bytes,
        template_id=template_id,
        version=version,
        **kwargs,  # type: ignore[arg-type]
    )


# ======================================================== 一、导入（第 1~3 步）
class TestImport:
    def test_import_parses_and_stages_a_package(
        self, harness: Harness, template_bytes: bytes
    ) -> None:
        record = _import(harness, template_bytes)
        assert record.register_only is False
        assert record.parsed is True
        assert record.package_dir is not None
        assert record.package_dir.startswith("imports") or "imports" in record.package_dir
        assert record.package_hash
        assert record.integrity == []
        assert all(stage.ok for stage in record.stages), record.stages
        assert record.summary is not None
        assert record.template_id == "TPL-1"
        assert record.version == "1.0"
        assert record.version_source_explicit is True

    def test_staging_survives_on_disk(self, harness: Harness, template_bytes: bytes) -> None:
        """原件与包都必须真落在 ``imports/<id>/``：那份"稍后重试"的承诺靠它兑现。"""
        record = _import(harness, template_bytes)
        staging = harness.settings.imports_root / record.import_id
        assert (staging / "import.json").is_file()
        assert (staging / "source" / TEMPLATE_NAME).read_bytes() == template_bytes
        # 包本体也在暂存里（按包自己的布局读回来，而不是猜一个文件名）：
        # 生成期读契约只认这一份，"稍后重试"重试的是解析、不是重新上传。
        assert read_package(staging / "package").manifest.package_hash == record.package_hash

    def test_get_import_round_trips(self, harness: Harness, template_bytes: bytes) -> None:
        record = _import(harness, template_bytes)
        again = harness.services.get_import(record.import_id)
        assert again.import_id == record.import_id
        assert again.package_hash == record.package_hash
        assert again.created_at == record.created_at
        assert [item.finding_id for item in again.findings] == [
            item.finding_id for item in record.findings
        ]
        assert again.summary == record.summary

    def test_each_import_gets_its_own_id(self, harness: Harness, template_bytes: bytes) -> None:
        first = _import(harness, template_bytes)
        second = _import(harness, template_bytes)
        assert first.import_id != second.import_id
        assert first.package_dir != second.package_dir

    def test_unknown_import_is_not_found(self, harness: Harness) -> None:
        expect_error(ErrorCode.NOT_FOUND, lambda: harness.services.get_import("i_missing"))

    def test_import_id_is_validated_before_being_a_path(self, harness: Harness) -> None:
        """硬约束 14：id 当路径片段用**先校验**（``:`` / ``/`` / ``\\`` 一律拒）。

        判据来自唯一实现 ``pipeline.checkpoint.path_segment``，故错误码与内核其它
        "id 当路径用"的地方一致（``E_CONFIG_INVALID``，不是另行发明一个）。
        """
        expect_error(ErrorCode.CONFIG_INVALID, lambda: harness.services.get_import("i:bad"))
        expect_error(ErrorCode.CONFIG_INVALID, lambda: harness.services.get_import("i\\bad"))

    def test_upload_gates_run_on_import(self, harness: Harness, template_bytes: bytes) -> None:
        """导入路径重跑一遍四道闸门（与 ``POST /uploads`` 共用同一份判据）。"""
        expect_error(
            ErrorCode.UPLOAD_TYPE,
            lambda: harness.services.import_template(filename="", payload=b""),
        )
        expect_error(
            ErrorCode.PATH_ESCAPE,
            lambda: harness.services.import_template(filename="../x.docx", payload=template_bytes),
        )


class TestDegradedImport:
    """解析失败 → "仅登记原件、稍后重试"（P05 状态与空态的第一条）。"""

    def test_unreadable_docx_becomes_register_only(self, harness: Harness) -> None:
        record = harness.services.import_template(
            filename="broken.docx", payload=b"PK\x03\x04not-a-zip"
        )
        assert record.register_only is True
        assert record.parsed is False
        assert record.package_dir is None
        assert record.package_hash is None
        assert record.source_hash, "原件快照必须拿到，否则无从重试"
        assert [stage.stage for stage in record.stages if not stage.ok] == ["unpack"]
        assert record.pending(), "失败的 unpack 必须作为待确认项报出来"

    def test_register_only_still_registers_the_original(self, harness: Harness) -> None:
        """未解析也必须能登记：库里存的是**原件**，并且明确标注"未解析"。

        未解析的导入没有可识别的编号（解析还没跑到那一步），故编号与版本必须由登记
        表单给出——这正是"界面第 5 步要让人补编号"的由来。
        """
        record = harness.services.import_template(
            filename="broken.docx", payload=b"PK\x03\x04not-a-zip"
        )
        assert record.template_id == "" and record.version == ""
        done = harness.services.register_template(
            record.import_id, template_id="BROKEN-1", version="0.1"
        )
        assert done.registration is not None
        assert done.registration.parsed is False
        assert done.registration.package_hash == ""
        assert done.registration.source_hash
        listed = harness.repository.list_templates(template_id="BROKEN-1")
        assert [item.ref for item in listed] == ["BROKEN-1@0.1"]
        # 原件要真落在库里，否则"稍后重试"就没有可重试的东西。
        assert (
            harness.settings.templates_root / "BROKEN-1" / "0.1" / "source" / "broken.docx"
        ).is_file()


# ==================================================== 二、待确认项（第 4 步）
class TestReview:
    def test_decisions_shrink_pending(self, harness: Harness, template_bytes: bytes) -> None:
        record = _import(harness, template_bytes)
        assert record.pending(), "夹具模板应当产出至少一条待确认项"
        target = record.pending()[0]
        assert target.finding_id is not None
        done = harness.services.review_import(
            record.import_id,
            operator="森",
            decisions=[ReviewDecision(item_id=target.finding_id, action="accept")],
        )
        assert len(done.decisions) == 1
        assert done.decisions[0].item_id == target.finding_id
        assert done.decisions[0].rule_id == target.rule_id
        assert done.decisions[0].operator == "森"
        assert target.finding_id not in {item.finding_id for item in done.pending()}

    def test_specify_without_value_is_rejected(
        self, harness: Harness, template_bytes: bytes
    ) -> None:
        """``specify`` 缺取值 → 专属错误码（不是 422 的"字段必填"，是"必须给出取值"）。"""
        record = _import(harness, template_bytes)
        item = record.pending()[0]
        error = expect_error(
            ErrorCode.REVIEW_VALUE_REQUIRED,
            lambda: harness.services.review_import(
                record.import_id,
                operator="森",
                decisions=[ReviewDecision(item_id=item.finding_id or "", action="specify")],
            ),
        )
        assert error.where["item_id"] == item.finding_id

    def test_empty_decision_list_is_rejected(self, harness: Harness, template_bytes: bytes) -> None:
        record = _import(harness, template_bytes)
        expect_error(
            ErrorCode.REVIEW_VALUE_REQUIRED,
            lambda: harness.services.review_import(record.import_id, operator="森", decisions=[]),
        )

    def test_unknown_item_is_not_found(self, harness: Harness, template_bytes: bytes) -> None:
        record = _import(harness, template_bytes)
        expect_error(
            ErrorCode.NOT_FOUND,
            lambda: harness.services.review_import(
                record.import_id,
                operator="森",
                decisions=[ReviewDecision(item_id="nope@9", action="accept")],
            ),
        )

    def test_redecision_is_appended_and_last_one_wins(
        self, harness: Harness, template_bytes: bytes
    ) -> None:
        """改主意是正常行为；"改主意"与"当初就选这个"在审计上必须能分开看。"""
        record = _import(harness, template_bytes)
        item_id = record.pending()[0].finding_id or ""
        harness.services.review_import(
            record.import_id,
            operator="森",
            decisions=[ReviewDecision(item_id=item_id, action="accept")],
        )
        done = harness.services.review_import(
            record.import_id,
            operator="森",
            decisions=[ReviewDecision(item_id=item_id, action="specify", value="封面标题")],
        )
        assert [item.action for item in done.decisions] == ["accept", "specify"]

    def test_decisions_are_written_into_the_package(
        self, harness: Harness, template_bytes: bytes
    ) -> None:
        """留痕同时落**包内** ``overrides.json``——那是生成期真正会读的那一份。"""
        record = _import(harness, template_bytes)
        item_id = record.pending()[0].finding_id or ""
        done = harness.services.review_import(
            record.import_id,
            operator="森",
            decisions=[ReviewDecision(item_id=item_id, action="accept", note="看过封面了")],
        )
        assert done.package_dir is not None
        package_dir = harness.settings.storage_root / done.package_dir
        bundle = read_overrides(package_dir)
        assert bundle is not None
        assert [item.item_id for item in bundle.entries] == [item_id]

    def test_review_of_a_decided_item_is_not_reported_as_pending(
        self, harness: Harness, template_bytes: bytes
    ) -> None:
        """ "待确认项为 0 → 跳过第 4 步"：办结过的项不再进 ``pending``。"""
        record = _import(harness, template_bytes)
        item_id = record.pending()[0].finding_id or ""
        done = harness.services.review_import(
            record.import_id,
            operator="森",
            decisions=[ReviewDecision(item_id=item_id, action="accept")],
        )
        assert item_id not in {item.finding_id for item in done.pending()}


# ======================================================== 三、登记（第 5 步）
class TestRegister:
    def test_register_writes_the_library_and_lists_it(
        self, harness: Harness, template_bytes: bytes
    ) -> None:
        record = _import(harness, template_bytes)
        done = harness.services.register_template(record.import_id, operator="森")
        assert done.registration is not None
        registration = done.registration
        assert registration.ref == "TPL-1@1.0"
        assert registration.parsed is True
        assert registration.integrity == []
        assert registration.registered_by == "森"
        library = harness.settings.templates_root / "TPL-1" / "1.0"
        # 库目录里读回来的必须是**包**（按包自己的布局读），哈希与暂存里那次解析一致。
        assert read_package(library).manifest.package_hash == registration.package_hash
        listed = harness.services.list_templates()
        assert [item.ref for item in listed] == ["TPL-1@1.0"]
        assert listed[0].package_hash == registration.package_hash

    def test_register_finalizes_the_version(self, harness: Harness, template_bytes: bytes) -> None:
        """解析结果与版本号无关：登记这一刻可以定版，且**不改包哈希**。"""
        record = _import(harness, template_bytes)
        staged_hash = record.package_hash
        done = harness.services.register_template(record.import_id, version="2.0")
        assert done.registration is not None
        assert done.registration.version == "2.0"
        assert done.registration.ref == "TPL-1@2.0"
        assert done.registration.package_hash == staged_hash
        assert "2.0" in harness.services.list_templates()[0].ref

    def test_missing_identity_is_rejected(self, harness: Harness) -> None:
        """未解析且未给编号 → 登记必须明确拒绝（``E_CONFIG_INVALID`` + 可操作动作）。

        报一个"还要填编号"的码，而不是让它去建一个名为空串的目录：``template_id``
        是配置与历史产物的引用键，空编号的登记没有任何东西能引用它。
        """
        record = harness.services.import_template(
            filename="broken.docx", payload=b"PK\x03\x04not-a-zip"
        )
        error = expect_error(
            ErrorCode.CONFIG_INVALID,
            lambda: harness.services.register_template(record.import_id),
        )
        assert error.where["template_id"] == ""
        assert error.remediation, "必须给出「怎么补上编号」的可操作动作"

    def test_unknown_conflict_policy_is_rejected(
        self, harness: Harness, template_bytes: bytes
    ) -> None:
        record = _import(harness, template_bytes)
        expect_error(
            ErrorCode.CONFIG_INVALID,
            lambda: harness.services.register_template(record.import_id, on_conflict="rename"),
        )

    def test_conflicting_content_aborts_with_both_hashes(
        self, harness: Harness, template_bytes: bytes
    ) -> None:
        """内容不同且未允许覆盖 → ``E_VERSION_CONFLICT``，并带出旧/新哈希与登记时刻。

        界面据此显示"这个版本号已被占用、旧版可能已被既有文档引用"，
        而不是笼统一句"失败"。
        """
        record = _import(harness, template_bytes)
        _seed_registration(harness, "TPL-1", "1.0", package_hash="deadbeef")
        error = expect_error(
            ErrorCode.VERSION_CONFLICT,
            lambda: harness.services.register_template(record.import_id),
        )
        assert error.where["existing_package_hash"] == "deadbeef"
        assert error.where["incoming_package_hash"] == record.package_hash
        assert error.where["registered_at"]

    def test_overwrite_replaces_and_flags_it(self, harness: Harness, template_bytes: bytes) -> None:
        record = _import(harness, template_bytes)
        _seed_registration(harness, "TPL-1", "1.0", package_hash="deadbeef")
        done = harness.services.register_template(record.import_id, on_conflict="overwrite")
        assert done.registration is not None
        assert done.registration.replaced is True
        assert done.registration.package_hash == record.package_hash
        assert harness.services.list_templates()[0].package_hash == record.package_hash

    def test_identical_content_is_registered_idempotently(
        self, harness: Harness, template_bytes: bytes
    ) -> None:
        """**对立断言**：内容一字不差时重登记是幂等的，**不是**冲突。

        否则"同一份文件提交两次"会被报成一个需要人决策的版本冲突，而那个冲突
        根本不存在——没有任何东西被换掉。``replaced`` 也必须是假：它回答的是
        "旧版被换掉了吗"，而这里旧版还是那个版本。
        """
        record = _import(harness, template_bytes)
        assert record.package_hash is not None
        _seed_registration(harness, "TPL-1", "1.0", package_hash=record.package_hash)
        done = harness.services.register_template(record.import_id)
        assert done.registration is not None
        assert done.registration.replaced is False
        assert done.registration.package_hash == record.package_hash

    def test_unparsed_registration_falls_back_to_the_source_hash(self, harness: Harness) -> None:
        """未解析时两级判据的**第二级**：同一份坏文件再登记是幂等的（原件哈希相同），
        换一份坏文件则报冲突——"重试同一份文件"不该变成一个要人决策的版本冲突。"""
        first = harness.services.import_template(
            filename="broken.docx", payload=b"PK\x03\x04not-a-zip"
        )
        harness.services.register_template(first.import_id, template_id="B", version="0.1")
        again = harness.services.import_template(
            filename="broken.docx", payload=b"PK\x03\x04not-a-zip"
        )
        same = harness.services.register_template(again.import_id, template_id="B", version="0.1")
        assert same.registration is not None
        assert same.registration.replaced is False

        other = harness.services.import_template(
            filename="broken.docx", payload=b"PK\x03\x04a-different-broken-file"
        )
        expect_error(
            ErrorCode.VERSION_CONFLICT,
            lambda: harness.services.register_template(
                other.import_id, template_id="B", version="0.1"
            ),
        )


def _seed_registration(
    harness: Harness, template_id: str, version: str, *, package_hash: str
) -> None:
    """预先放一条登记（模拟"上一次登记的版本"），用于精确构造冲突。"""
    harness.repository.add_template(
        RegisteredTemplate(
            template_id=template_id,
            version=version,
            package_hash=package_hash,
            source_hash="seed",
            package_dir=str(harness.settings.templates_root / template_id / version),
            registered_at=datetime(2026, 9, 20, 9, 0, tzinfo=timezone.utc),
        )
    )


# ================================================= 四、重解析（第 3 步重跑）
class TestReparse:
    """「稍后重试解析」的落点：对**已收下的原件**再解析一次，不重新上传。

    四条判据各配一条用例，其中两条是必须存在的对立断言：

    * 未登记的可以重解析 ↔ **已登记的必须被拒**（登记之后模板包是不可变的历史）；
    * 重解析要能**连做两次**（否则"先清空包目录"这条实现细节会在第二次暴露成
      ``package_io`` 失败，而那正是最需要重试的情形）；
    * 人给的编号续用 ↔ **猜出来的编号不得因此变成"人给的"**（来源被改写会让界面
      不再提示人补编号，而那个编号从来没人确认过）。
    """

    def test_reparse_of_an_unregistered_import_stamps_the_moment(
        self, harness: Harness, template_bytes: bytes
    ) -> None:
        record = _import(harness, template_bytes)
        assert record.reparsed_at is None
        again = harness.services.reparse_import(record.import_id)
        assert again.import_id == record.import_id
        # 身份不变：重解析不改变"这份导入是什么时候发生的"。
        assert again.created_at == record.created_at
        assert again.source_hash == record.source_hash
        assert again.reparsed_at is not None
        assert again.package_dir == record.package_dir
        assert all(stage.ok for stage in again.stages), again.stages

    def test_reparse_can_run_twice_in_a_row(self, harness: Harness, template_bytes: bytes) -> None:
        """包目录必须先清空。

        ``write_package`` 对非空目标目录直接报 ``E_VERSION_CONFLICT``（它不静默覆盖），
        所以"不清空"的表现不是异常，而是 ``stages`` 里多出一条 ``package_io`` 失败——
        报告看上去仍是一份报告，只有这条断言能把它顶出来。
        """
        record = _import(harness, template_bytes)
        first = harness.services.reparse_import(record.import_id)
        second = harness.services.reparse_import(record.import_id)
        assert all(stage.ok for stage in first.stages), first.stages
        assert all(stage.ok for stage in second.stages), second.stages
        assert second.package_hash == first.package_hash

    def test_a_guessed_version_does_not_become_explicit(
        self, harness: Harness, template_bytes: bytes
    ) -> None:
        """对立断言：从文件名/文档里猜出来的版本，重解析之后来源**不得**变成 ``explicit``。

        传下去的值会被 ``detect_identity`` 记成"人给的"；若把猜出来的值当成人给的再传
        一遍，界面就会停止提示"登记前请确认编号"——而那个编号从来没人确认过。
        """
        record = harness.services.import_template(
            filename=TEMPLATE_NAME, payload=template_bytes, doc_type="demo"
        )
        assert record.version_source != "explicit", "夹具应当能猜出一个版本，否则本条无意义"
        again = harness.services.reparse_import(record.import_id)
        assert again.version_source == record.version_source
        assert again.template_id_source == record.template_id_source
        assert again.version == record.version

    def test_an_explicit_identity_is_kept(self, harness: Harness, template_bytes: bytes) -> None:
        """对立断言的另一半：人真的给了编号时，重解析要把它续用下去。"""
        record = _import(harness, template_bytes, template_id="TPL-9", version="2.1")
        assert record.version_source_explicit is True
        again = harness.services.reparse_import(record.import_id)
        assert again.template_id == "TPL-9"
        assert again.version == "2.1"
        assert again.version_source_explicit is True

    def test_reparse_reads_the_copy_in_the_staging_directory(
        self, harness: Harness, template_bytes: bytes
    ) -> None:
        """重解析读的是**暂存里那份副本**，不是上传目录、也不接受新上传。

        这里把暂存里的原件换成另一份结构不同的文档（``build_sample_docx``，与
        ``build_template_docx`` 的标题 / 表格 / 目录结构都不同），重解析必须读到它——
        这是这条路径存在的全部意义：上传件受保留策略管辖会过期，暂存里的副本不会。

        换的是**契约确实不同**的文档，不是"同一份再生成一次"：后者只会让文件字节不同
        （zip 时间戳）而契约哈希照旧，断言就退化成了在验归一化（那是另一条纪律）。
        """
        from tests.fixtures.docx_builder import build_sample_docx

        record = _import(harness, template_bytes)
        staged = harness.settings.imports_root / record.import_id / "source" / TEMPLATE_NAME
        assert staged.read_bytes() == template_bytes
        replacement = build_sample_docx(staged.parent / "replacement.docx")
        staged.write_bytes(replacement.read_bytes())

        again = harness.services.reparse_import(record.import_id)
        assert again.source_hash != record.source_hash
        assert again.package_hash != record.package_hash
        # 包真的重写好了（不是只更新了记录里的哈希）。
        assert again.package_dir is not None
        package = read_package(harness.settings.storage_root / again.package_dir)
        assert package.manifest.package_hash == again.package_hash
        assert again.integrity == []

    def test_a_missing_original_is_not_found(self, harness: Harness, template_bytes: bytes) -> None:
        """记录读得到、原件不在 → ``E_NOT_FOUND``（而不是抛一个裸异常）。"""
        record = _import(harness, template_bytes)
        (harness.settings.imports_root / record.import_id / "source" / TEMPLATE_NAME).unlink()
        expect_error(
            ErrorCode.NOT_FOUND,
            lambda: harness.services.reparse_import(record.import_id),
        )

    def test_reparsing_a_registered_import_is_refused(
        self, harness: Harness, template_bytes: bytes
    ) -> None:
        """对立断言：已登记的不得重解析。

        登记之后 ``templates/<tid>/<ver>/`` 是不可变的历史；就地重解析会让暂存里的包与
        已登记的包悄悄分叉，而下一次登记看上去就成了一次"同版本换内容"的覆盖。
        """
        record = _import(harness, template_bytes)
        harness.services.register_template(
            record.import_id, template_id="TPL-1", version="1.0", operator="森"
        )
        error = expect_error(
            ErrorCode.VERSION_CONFLICT,
            lambda: harness.services.reparse_import(record.import_id),
        )
        assert "TPL-1@1.0" in error.where.get("registered_ref", "")
        stored = harness.services.get_import(record.import_id)
        assert stored.reparsed_at is None
        assert stored.can_reparse is False
        assert stored.reparse_blocked_reason is not None
        assert "TPL-1@1.0" in stored.reparse_blocked_reason

    def test_decisions_survive_a_reparse(self, harness: Harness, template_bytes: bytes) -> None:
        """留痕原样保留，而"还剩几项"按**新**解析结果算。

        这两件事必须分开：``decisions`` 是"谁在什么时候确认了什么"的历史，重解析不该
        抹掉它；而 ``pending`` 是"现在还剩几项"，它只认当前 ``findings``。
        """
        record = _import(harness, template_bytes)
        target = record.pending()[0]
        assert target.finding_id is not None
        harness.services.review_import(
            record.import_id,
            operator="森",
            decisions=[ReviewDecision(item_id=target.finding_id, action="accept")],
        )
        again = harness.services.reparse_import(record.import_id)
        assert [item.item_id for item in again.decisions] == [target.finding_id]
        assert target.finding_id not in {item.finding_id for item in again.pending()}

    def test_reparse_refreshes_the_stage_report(
        self, harness: Harness, template_bytes: bytes
    ) -> None:
        """阶段报告是**这一次**解析的，不是上一次的残留。"""
        record = _import(harness, template_bytes)
        again = harness.services.reparse_import(record.import_id)
        assert [stage.stage for stage in again.stages] == [stage.stage for stage in record.stages]
        assert again.summary == record.summary
        assert again.integrity == []


# ============================================================== 五、HTTP 层
class TestHttp:
    """端点形状与状态码（DD-13.2）。走真 HTTP，验证路由、multipart 与响应模型。"""

    @pytest.fixture
    def client(self, tmp_path: Path) -> TestClient:
        # ``storage_root`` 必须显式注入：``create_app`` 的 ``root`` 只影响运行器工作区，
        # 而暂存 / 模板库用的是 **settings** 的根。不给就落到仓库里的 ``var/``，
        # 于是一次用例会把包写进工作树，并让下一次运行撞上"版本已存在"。
        root = tmp_path / "http"
        app = create_app(
            settings=ServerSettings(_env_file=None, storage_root=root),
            repository=MemoryRepository(),
            stages=build_registry(),
            root=root,
            start_worker=False,
        )
        return TestClient(app)

    def test_import_then_register_over_http(
        self, client: TestClient, template_bytes: bytes
    ) -> None:
        response = client.post(
            "/api/v1/templates/imports",
            files={
                "file": (
                    TEMPLATE_NAME,
                    template_bytes,
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                )
            },
            data={"template_id": "TPL-9", "version": "0.1", "doc_type": "demo", "author": "森"},
        )
        assert response.status_code == 201, response.text
        body = response.json()
        import_id = body["import_id"]
        assert body["template_id"] == "TPL-9"
        assert body["parsed"] is True
        assert body["pending_count"] == len(body["pending"])
        assert body["summary"]["style_count"] > 0
        # 落点不外泄（DD-13.7 数据边界）。
        assert "package_dir" not in body

        got = client.get(f"/api/v1/templates/imports/{import_id}")
        assert got.status_code == 200
        assert got.json()["import_id"] == import_id

        registered = client.post(
            f"/api/v1/templates/imports/{import_id}/register",
            json={"on_conflict": "abort", "operator": "森"},
        )
        assert registered.status_code == 200, registered.text
        assert registered.json()["registration"]["ref"] == "TPL-9@0.1"

        library = client.get("/api/v1/templates")
        assert library.status_code == 200
        assert library.json()["count"] == 1
        assert library.json()["items"][0]["ref"] == "TPL-9@0.1"

        versions = client.get("/api/v1/templates/TPL-9/versions")
        assert versions.status_code == 200
        assert versions.json()["count"] == 1
        assert versions.json()["items"][0]["version"] == "0.1"

        # ``/templates/{tid}`` 现在是**详情**（P04）：版本列表在 ``/versions``。
        # 两者分开是因为详情要带上整份契约，而"只列版本"是一个轻得多的请求。
        detail = client.get("/api/v1/templates/TPL-9")
        assert detail.status_code == 200
        body = detail.json()
        assert body["entry"]["ref"] == "TPL-9@0.1"
        assert body["parsed"] is True
        assert [item["version"] for item in body["versions"]] == ["0.1"]
        # 两条检查都要有结论：``integrity`` 空 = 包与清单一致，
        # ``fingerprint_problems`` 空 = 指纹与包内容一致。
        assert body["integrity"] == []
        assert body["fingerprint_problems"] == []
        assert body["summary"]["slot_count"] >= 0

    def test_reparse_over_http_carries_the_verdict_and_the_reason(
        self, client: TestClient, template_bytes: bytes
    ) -> None:
        """重解析的"能不能点"由服务端给：``can_reparse`` + ``reparse_blocked_reason``。

        界面照抄前者置灰、直接显示后者。少了它，前端就只能自己判"registration 是不是
        空"——那是把一条服务端规则抄成了两份（硬规则 15）。
        """
        created = client.post(
            "/api/v1/templates/imports",
            files={
                "file": (
                    TEMPLATE_NAME,
                    template_bytes,
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                )
            },
            data={"template_id": "TPL-1", "version": "1.0"},
        )
        import_id = created.json()["import_id"]
        assert created.json()["can_reparse"] is True
        assert created.json()["reparse_blocked_reason"] is None
        assert created.json()["reparsed_at"] is None

        reparsed = client.post(f"/api/v1/templates/imports/{import_id}/reparse")
        assert reparsed.status_code == 200, reparsed.text
        assert reparsed.json()["import_id"] == import_id
        assert reparsed.json()["reparsed_at"] is not None

        client.post(
            f"/api/v1/templates/imports/{import_id}/register",
            json={"template_id": "TPL-1", "version": "1.0", "operator": "森"},
        )
        after = client.get(f"/api/v1/templates/imports/{import_id}").json()
        assert after["can_reparse"] is False
        assert "TPL-1@1.0" in after["reparse_blocked_reason"]

        refused = client.post(f"/api/v1/templates/imports/{import_id}/reparse")
        assert refused.status_code == 409
        assert refused.json()["code"] == ErrorCode.VERSION_CONFLICT

    def test_reparse_of_a_missing_import_returns_404(self, client: TestClient) -> None:
        response = client.post("/api/v1/templates/imports/i_missing/reparse")
        assert response.status_code == 404

    def test_specify_without_value_returns_422(
        self, client: TestClient, template_bytes: bytes
    ) -> None:
        """错误包里的 ``code`` 是界面渲染的依据，故状态码与错误码都要断言。"""
        created = client.post(
            "/api/v1/templates/imports",
            files={"file": (TEMPLATE_NAME, template_bytes, "application/octet-stream")},
        ).json()
        item_id = created["pending"][0]["finding_id"]
        response = client.post(
            f"/api/v1/templates/imports/{created['import_id']}/decisions",
            json={
                "operator": "森",
                "decisions": [{"item_id": item_id, "action": "specify"}],
            },
        )
        assert response.status_code == 422, response.text
        assert response.json()["code"] == ErrorCode.REVIEW_VALUE_REQUIRED

    def test_missing_import_returns_404(self, client: TestClient) -> None:
        response = client.get("/api/v1/templates/imports/i_nope")
        assert response.status_code == 404
        assert response.json()["code"] == ErrorCode.NOT_FOUND

    def test_openapi_publishes_the_template_group(self, client: TestClient) -> None:
        schema = client.get("/openapi.json").json()
        paths = schema["paths"]
        assert "/api/v1/templates/imports" in paths
        assert "/api/v1/templates/imports/{import_id}/register" in paths
        tags = {tag["name"] for tag in schema["tags"]}
        assert "templates" in tags


class TestHttpDecisionValidation:
    """动作取值是**闭集**（``accept`` / ``specify``）——多一种就多一条"确认了什么"的歧义。"""

    @pytest.fixture
    def client(self, tmp_path: Path) -> TestClient:
        root = tmp_path / "http2"
        app = create_app(
            settings=ServerSettings(_env_file=None, storage_root=root),
            repository=MemoryRepository(),
            stages=build_registry(),
            root=root,
            start_worker=False,
        )
        return TestClient(app)

    def test_action_outside_the_closed_set_is_a_422(
        self, client: TestClient, template_bytes: bytes
    ) -> None:
        created = client.post(
            "/api/v1/templates/imports",
            files={"file": (TEMPLATE_NAME, template_bytes, "application/octet-stream")},
        ).json()
        item_id = created["pending"][0]["finding_id"]
        response = client.post(
            f"/api/v1/templates/imports/{created['import_id']}/decisions",
            json={
                "operator": "森",
                "decisions": [{"item_id": item_id, "action": "waive", "note": "放行"}],
            },
        )
        assert response.status_code == 422, response.text

    def test_operator_is_required(self, client: TestClient, template_bytes: bytes) -> None:
        """责任人必填（NFR-15）：没有它，留痕就答不出"谁确认的"。"""
        created = client.post(
            "/api/v1/templates/imports",
            files={"file": (TEMPLATE_NAME, template_bytes, "application/octet-stream")},
        ).json()
        item_id = created["pending"][0]["finding_id"]
        response = client.post(
            f"/api/v1/templates/imports/{created['import_id']}/decisions",
            json={"operator": "", "decisions": [{"item_id": item_id, "action": "accept"}]},
        )
        assert response.status_code == 422, response.text
