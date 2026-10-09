"""档案包的导出与导入（FR-M2-25 / 验收 45；DD-13.2 的 ``profiles/import`` + ``profiles/{pid}/export``）。

这个文件回答一句话：**"档案连同模板包打包导出后，在另一环境导入即可复现同样产出"
（验收 45 原文）到底成不成立。** 前半句靠"包内真的有模板包"，后半句靠"真的能在
一个空库里导入出来"——所以本文件的每一组用例都跑**两个 harness**：发送端装好资产
导出，收件端是一个全新的、空空的库。

六条引脚：

① **包是自足的**——``templates/<ref>/`` 里既要有契约，也要有**原件**；收件方不必
   回头找发件人要模板（那是"打包交付"与"发了个 JSON"的区别）；
② **一步导入**——一次调用里模板包先登记、档案随后登记。顺序颠倒的话，一次正确的
   导入会被自己刚交上来的档案判成"引用了未登记的模板"（FR-M2-23）；
③ **收件端的库是空的，且它不需要发件端的任何东西**——包内不出现发件端的绝对路径；
④ **两端的库因此长得一样**——收件端登记出来的 ``package_hash`` 与包内清单声明的一致，
   这是"你发我的就是你库里的那一份"在**导入侧**的对照；
⑤ **包是不可信输入**——不是 zip、越界路径、缺 ``profile.json``、引用了包里没有的模板，
   一律 ``E_PROFILE_PACKAGE_INVALID`` 并说清是哪一条，**不是一半装进去再说**；
⑥ **不是自足的包导不出去**——引用没登记的模板时导出**当场拒**，而不是打一个缺件的包
   出去让收件方那边才炸。
"""

from __future__ import annotations

import io
import json
import zipfile
from collections.abc import Callable
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from docfidelity.contracts.errors import EngineError, ErrorCode
from docfidelity.contracts.profile import Bindings, DocTypeProfile
from server.main import create_app
from server.profile_package import (
    APPLICABILITY_FILENAME,
    PACKAGE_NOTES_FILENAME,
    PROFILE_PACKAGE_FORMAT,
    SAMPLE_INPUT_FILENAME,
    TEMPLATE_BUNDLE_DIRNAME,
)
from server.profiles import PROFILE_FILENAME
from server.storage import (
    MemoryRepository,
    Repository,
    SqlAlchemyRepository,
    create_engine_for,
    run_migrations,
)
from tests.fixtures.domain_fixture import api_profile
from tests.fixtures.server_fixture import Harness, build_harness, build_registry

pytestmark = pytest.mark.api

#: 本文件用的模板标识（与 ``domain_fixture.api_profile`` 的 ``applies.layout_source``
#: 不是同一个串——那个是给人读的说明，引用键在 ``bindings.template_packages`` 里）。
TEMPLATE_ID = "TPL-PKG"
TEMPLATE_VERSION = "1.0"
TEMPLATE_REF = f"{TEMPLATE_ID}@{TEMPLATE_VERSION}"

TEMPLATE_NAME = "template.docx"


@pytest.fixture
def sender(tmp_path: Path, repo: Repository) -> Harness:
    """发送端：一台装好了模板包与档案的机器。"""
    return build_harness(tmp_path / "sender", repository=repo)


@pytest.fixture
def receiver(tmp_path: Path, repo: Repository) -> Harness:
    """收件端：**另一台机器**上、**空的**库。

    仓储种类跟着 ``repo`` 参数走（同一句断言在内存与落库实现下各跑一遍），但**实例
    是新的**——复用 ``sender`` 那个仓储的话，"导入到一个空库"就不是被验证的事实了。
    """
    return build_harness(tmp_path / "receiver", repository=_fresh_like(repo, tmp_path))


def _fresh_like(repo: Repository, tmp_path: Path) -> Repository:
    """造一个与 ``repo`` **同一种实现**、但状态为空的仓储。"""
    if isinstance(repo, SqlAlchemyRepository):
        url = f"sqlite:///{(tmp_path / 'receiver.db').as_posix()}"
        run_migrations(url)
        return SqlAlchemyRepository(create_engine_for(url))
    return MemoryRepository()


def expect_error(code: str, fn: Callable[[], object]) -> EngineError:
    with pytest.raises(EngineError) as caught:
        fn()
    assert caught.value.code == code, caught.value.message
    return caught.value


# ------------------------------------------------------------------ 小工具
def register_template(harness: Harness, payload: bytes) -> str:
    """走**真实导入流水线**把一份模板包登记进库，返回它的引用。

    不走"直接落仓储"的捷径：本文件要证的正是"包里那份模板能在收件端被**登记**成
    一个能用的包"，而登记要的契约、原件、``package_hash`` 全是这条流水线产出的。
    绕过它，测的就是一个手搭的状态了。
    """
    record = harness.services.import_template(
        filename=TEMPLATE_NAME,
        payload=payload,
        template_id=TEMPLATE_ID,
        version=TEMPLATE_VERSION,
    )
    done = harness.services.register_template(record.import_id, operator="森")
    assert done.registration is not None, [stage.stage for stage in record.stages if not stage.ok]
    return done.registration.ref


def bound_profile(**overrides: object) -> DocTypeProfile:
    """引用 :data:`TEMPLATE_REF` 的档案（``api_profile`` 默认绑的是另一个引用）。"""
    base = api_profile().model_copy(
        update={"bindings": Bindings(template_packages=[TEMPLATE_REF], rule_sets=["base_style"])}
    )
    return base.model_copy(update=overrides) if overrides else base


def register_profile(harness: Harness, document: DocTypeProfile, **kwargs: object) -> object:
    """按 HTTP 层的做法登记：先读库拿引用集与领域模型，再交给用例层。"""
    return harness.services.register_profile(
        document=document,
        known_template_refs=[item.ref for item in harness.services.list_templates()],
        domain_models=harness.services.domain_declarations(),
        **kwargs,
    )


def import_package(harness: Harness, payload: bytes, **kwargs: object) -> object:
    """按 HTTP 层的做法导入：领域模型从**收件端的库**读好再传进去。

    用例层那一头的缺省是**空序列**而不是"自己去查"（硬规则 11：漏传不得表现为通过），
    故测试若直接调 ``services.import_profile_package`` 且不传，就会撞上一次
    **调用方造成的** ``E_DOMAIN_KIND_UNKNOWN``——那是"这次没给库"，不是"库里没有"。
    这个包装做的正是生产路径那一步。
    """
    return harness.services.import_profile_package(
        payload=payload, domain_models=harness.services.domain_declarations(), **kwargs
    )


def entries_of(payload: bytes) -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        return {item.filename: archive.read(item.filename) for item in archive.infolist()}


def prepared(sender: Harness, template_bytes: bytes) -> tuple[bytes, str]:
    """发送端装好"模板包 + 引用它的档案"，返回 ``(导出件字节, 档案指纹)``。"""
    from tests.fixtures.server_fixture import seed_domain_models

    seed_domain_models(sender)
    register_template(sender, template_bytes)
    receipt = register_profile(sender, bound_profile())
    _, payload = sender.services.export_profile("api-reference")
    return payload, receipt.fingerprint  # type: ignore[attr-defined]


# ======================================================= 一、导出件是自足的
class TestExportIsSelfContained:
    def test_carries_the_referenced_template_package(
        self, sender: Harness, template_bytes: bytes
    ) -> None:
        """**验收 45 的核心一句**：包内要有它引用的模板包，且契约与原件都在。

        只发 ``profile.json`` 的包不是"打包交付"——收件方拿着 ``bindings`` 里的引用键，
        还得回去找发件人要模板。
        """
        payload, _ = prepared(sender, template_bytes)
        entries = entries_of(payload)
        prefix = f"{TEMPLATE_BUNDLE_DIRNAME}/{TEMPLATE_REF}/"
        inside = sorted(name for name in entries if name.startswith(prefix))
        assert inside, sorted(entries)
        # 契约与**原件**都要在：渲染的第一步就是打开那份 .docx，包里没有它，
        # 收件端登记出来的就是一个"能回答有哪些槽位、却拿不出一份可写文档"的模板。
        assert f"{prefix}source/{TEMPLATE_NAME}" in inside
        assert any(name.startswith(f"{prefix}contract/") for name in inside)

    def test_carries_the_applicability_note_the_sample_input_and_the_notes(
        self, sender: Harness, template_bytes: bytes
    ) -> None:
        """另外三件随包走的东西：适用判据（需求 2.6.1）、样例输入、包说明。"""
        payload, _ = prepared(sender, template_bytes)
        entries = entries_of(payload)
        assert APPLICABILITY_FILENAME in entries
        assert SAMPLE_INPUT_FILENAME in entries
        assert PACKAGE_NOTES_FILENAME in entries

    def test_the_sample_input_is_generated_from_the_profile(
        self, sender: Harness, template_bytes: bytes
    ) -> None:
        """样例输入**随档案生成**，不是手写放在仓库里的那一份。

        手写的那份会与档案漂移：加一个 ``required`` 字段而样例没跟上，收件方照着样例
        准备输入，构建时才在裁决队列里发现缺字段。故这里断言的是"它答出了档案声明的
        领域集合与字段"，而不是"它等于某个文件"。
        """
        payload, _ = prepared(sender, template_bytes)
        sample = json.loads(entries_of(payload)[SAMPLE_INPUT_FILENAME].decode("utf-8"))
        assert sample["profile_id"] == "api-reference"
        declared = {
            str(spec.for_each).partition(".")[0]
            for spec in api_profile().projections
            if spec.for_each
        }
        assert set(sample["领域集合"]) == declared, sample["领域集合"]

    def test_the_notes_declare_the_format_and_the_contents(
        self, sender: Harness, template_bytes: bytes
    ) -> None:
        """包说明回答"这个包我这一版读不读得懂、里面有什么、我该先做什么"。"""
        payload, receipt_fingerprint = prepared(sender, template_bytes)
        notes = json.loads(entries_of(payload)[PACKAGE_NOTES_FILENAME].decode("utf-8"))
        assert notes["format"] == PROFILE_PACKAGE_FORMAT
        assert notes["profile_ref"] == "api-reference@1.0"
        assert notes["fingerprint"] == receipt_fingerprint
        assert TEMPLATE_REF in notes["template_packages"]
        assert PROFILE_FILENAME in notes["contents"]
        assert notes["how_to_use"]

    def test_no_absolute_path_of_the_sender_leaks(
        self, sender: Harness, template_bytes: bytes
    ) -> None:
        """包内不出现发送端的绝对路径。

        包要能在**另一台机器**上导入，那么它就不得携带"我原来放在哪"——
        携带了的那一份在收件端要么写不进去、要么把收件端的目录结构按发送端的习惯摆。
        """
        payload, _ = prepared(sender, template_bytes)
        needle = str(sender.settings.storage_root).replace("\\", "/")
        for name, data in entries_of(payload).items():
            assert needle not in name, name
            if name.endswith(".json"):
                assert needle not in data.decode("utf-8", errors="replace"), name

    def test_is_byte_identical_across_exports(self, sender: Harness, template_bytes: bytes) -> None:
        """包变厚了，确定性没变：同一份输入导出两次字节相同。"""
        prepared(sender, template_bytes)
        _, first = sender.services.export_profile("api-reference")
        _, second = sender.services.export_profile("api-reference")
        assert first == second

    def test_a_missing_template_package_refuses_the_export(
        self, sender: Harness, template_bytes: bytes
    ) -> None:
        """**引脚⑥**：引用不在库里（或已废弃）就拒，而不是打一个缺件的包出去。

        拒绝的理由必须指向"收件方能不能复现"，不是"库里少一行"——后者听起来像内部
        记账问题，而实际后果是收件方拿到一个装不上的包。

        用的是一条**真的能到达这里的路径**：登记期校验的是"引用在不在库"，而模板可以
        在档案登记**之后**被废弃。``resolve_template`` 对废弃的一版返回 ``None``
        （"别再拿它产出新东西了"，与档案那边逐字同理由），于是那次导出必须当场拒——
        否则废弃标记在导出路径上就失效了。
        """
        from tests.fixtures.server_fixture import seed_domain_models

        seed_domain_models(sender)
        register_template(sender, template_bytes)
        register_profile(sender, bound_profile())
        sender.services.deprecate_template(
            TEMPLATE_ID, version=TEMPLATE_VERSION, reason="改用新版模板"
        )

        error = expect_error(
            ErrorCode.PROFILE_TEMPLATE_MISSING,
            lambda: sender.services.export_profile("api-reference"),
        )
        assert TEMPLATE_REF in json.dumps(error.where, ensure_ascii=False)

    def test_an_unreadable_template_package_refuses_the_export(
        self, sender: Harness, template_bytes: bytes
    ) -> None:
        """**对立断言**：库里那条登记指向一个**读不回来**的包时，导出也拒。

        与上一条是两个不同的分支（``resolve_template`` 返回 ``None`` vs 返回了但
        ``verify_package`` 报问题），而两者的处置必须一样——打一个"看着有模板、其实
        打不开"的包出去，收件端要到构建期才发现。
        """
        import shutil

        from tests.fixtures.server_fixture import seed_domain_models

        seed_domain_models(sender)
        register_template(sender, template_bytes)
        register_profile(sender, bound_profile())
        entry = sender.services.get_template(TEMPLATE_ID, TEMPLATE_VERSION)
        shutil.rmtree(Path(entry.package_dir) / "contract")

        error = expect_error(
            ErrorCode.PROFILE_PACKAGE_INVALID,
            lambda: sender.services.export_profile("api-reference"),
        )
        assert TEMPLATE_REF in json.dumps(error.where, ensure_ascii=False)


# ======================================================= 二、一步导入
class TestImportRoundTrip:
    def test_imports_into_an_empty_library_and_reproduces_the_assets(
        self, sender: Harness, receiver: Harness, template_bytes: bytes
    ) -> None:
        """**验收 45 的后半句**：把包丢进一个空库，两样东西都到齐。

        这条用例是整个文件的目的地：发送端装了什么，收件端就有什麼。
        """
        payload, fingerprint = prepared(sender, template_bytes)
        assert receiver.services.list_templates() == []
        assert receiver.services.list_profiles() == []

        from tests.fixtures.server_fixture import seed_domain_models

        seed_domain_models(receiver)
        result = import_package(receiver, payload, author="森")

        assert result.registration.profile_id == "api-reference"
        assert result.registration.version == "1.0"
        assert result.registration.fingerprint == fingerprint
        assert [item.ref for item in result.templates] == [TEMPLATE_REF]
        assert result.unused_templates == []
        # 收件端的库现在**真的**有这两样，且模板是**已解析**的那一种
        # （``parsed`` 由 ``package_hash`` 推出来，见 ``RegisteredTemplate``）。
        template = receiver.services.get_template(TEMPLATE_ID, TEMPLATE_VERSION)
        assert template.parsed is True
        assert template.source_filename == TEMPLATE_NAME
        assert receiver.services.get_profile("api-reference").version == "1.0"

    def test_the_registered_package_hash_matches_the_one_in_the_bundle(
        self, sender: Harness, receiver: Harness, template_bytes: bytes
    ) -> None:
        """**引脚④**：收件端登记出来的包哈希 = 包里清单声明的那个。

        这句话是"你发我的就是你库里的那一份"在导入侧的对照。不比它的话，"导入成功"
        只说明文件写下去了，不说明写下去的是同一份。
        """
        payload, _ = prepared(sender, template_bytes)
        from tests.fixtures.server_fixture import seed_domain_models

        seed_domain_models(receiver)
        result = import_package(receiver, payload)
        sender_entry = sender.services.get_template(TEMPLATE_ID, TEMPLATE_VERSION)
        receiver_entry = receiver.services.get_template(TEMPLATE_ID, TEMPLATE_VERSION)
        assert receiver_entry.package_hash == sender_entry.package_hash
        assert result.templates[0].package_hash == sender_entry.package_hash

    def test_the_receiver_can_read_back_the_template_source(
        self, sender: Harness, receiver: Harness, template_bytes: bytes
    ) -> None:
        """原件落到了渲染期会去找的那个位置（``<package_dir>/source/<filename>``）。

        路径口径两处各拼一次就会出现"登记时放在 A、构建时去 B 找"，而那时报的是
        ``E_TEMPLATE_UNREADABLE``——离"谁拼错了"很远。
        """
        from server.templates import template_source_path
        from tests.fixtures.server_fixture import seed_domain_models

        payload, _ = prepared(sender, template_bytes)
        seed_domain_models(receiver)
        import_package(receiver, payload)
        entry = receiver.services.get_template(TEMPLATE_ID, TEMPLATE_VERSION)
        source = template_source_path(entry)
        assert source is not None and source.is_file()
        assert source.read_bytes() == template_bytes

    def test_a_second_import_of_the_same_package_is_idempotent(
        self, sender: Harness, receiver: Harness, template_bytes: bytes
    ) -> None:
        """同一份包导两次：内容相同 → 幂等，不是一次要人决策的冲突。

        判据与模板登记**共用** ``same_template_content``——两条导入路径对"这次算不算
        冲突"必须给同一个答案。
        """
        from tests.fixtures.server_fixture import seed_domain_models

        payload, _ = prepared(sender, template_bytes)
        seed_domain_models(receiver)
        first = import_package(receiver, payload)
        second = import_package(receiver, payload)
        assert first.templates[0].replaced is False
        assert second.templates[0].replaced is True
        assert second.registration.replaced is False
        assert len(receiver.services.list_templates()) == 1

    def test_the_bundled_template_is_registered_before_the_profile(
        self, sender: Harness, receiver: Harness, template_bytes: bytes
    ) -> None:
        """**引脚②**：顺序不可颠倒。

        本用例不给收件端预置任何模板，只调一次导入。若实现先登记档案，FR-M2-23 的
        "引用了未登记的模板"会把这次**正确**的导入拦下来——那时失败信息指向档案的
        ``bindings``，而问题在调用顺序上。
        """
        from tests.fixtures.server_fixture import seed_domain_models

        payload, _ = prepared(sender, template_bytes)
        seed_domain_models(receiver)
        result = import_package(receiver, payload)
        assert result.registration.skipped == []
        assert result.registration.findings == []

    def test_an_unused_bundle_is_reported_not_silently_installed(
        self, sender: Harness, receiver: Harness, template_bytes: bytes
    ) -> None:
        """包里带了档案**没引用**的模板包：如实列出，且不往库里塞。

        模板库里的每一条都是配置与历史产物的引用键；一次档案导入不该往库里塞这份档案
        根本用不上的东西。但多出来的那份也不能装看不见——它要么是发件方包打错了，
        要么是两边版本对不上。
        """
        from tests.fixtures.server_fixture import seed_domain_models

        payload, _ = prepared(sender, template_bytes)
        extra_ref = "TPL-EXTRA@1.0"
        rebuilt = _append_bundle(payload, extra_ref)
        seed_domain_models(receiver)
        result = import_package(receiver, rebuilt)
        assert result.unused_templates == [extra_ref]
        assert [entry.ref for entry in receiver.services.list_templates()] == [TEMPLATE_REF]

    def test_the_imported_profile_is_the_one_the_sender_exported(
        self, sender: Harness, receiver: Harness, template_bytes: bytes
    ) -> None:
        """收件端读回的档案本体与发件端逐字段相同（不是"看着差不多"）。"""
        from tests.fixtures.server_fixture import seed_domain_models

        payload, _ = prepared(sender, template_bytes)
        seed_domain_models(receiver)
        import_package(receiver, payload)
        sender_doc = sender.services.profile_document("api-reference")
        receiver_doc = receiver.services.profile_document("api-reference")
        assert receiver_doc == sender_doc


# ======================================================= 三、不可信输入
class TestHostilePackages:
    def test_a_non_zip_payload_is_rejected(self, receiver: Harness) -> None:
        expect_error(
            ErrorCode.PROFILE_PACKAGE_INVALID,
            lambda: import_package(receiver, b"not a zip at all"),
        )

    def test_a_package_without_the_profile_document_is_rejected(self, receiver: Harness) -> None:
        payload = _zip({"hello.txt": b"hi"})
        error = expect_error(
            ErrorCode.PROFILE_PACKAGE_INVALID,
            lambda: import_package(receiver, payload),
        )
        assert PROFILE_FILENAME in error.message

    def test_a_traversing_member_is_rejected(self, receiver: Harness) -> None:
        """zip-slip：条目名里的 ``..`` 一律拒，且要在**写盘之前**拒。"""
        payload = _zip({"../escape.json": b"{}", PROFILE_FILENAME: _profile_json()})
        error = expect_error(
            ErrorCode.PROFILE_PACKAGE_INVALID,
            lambda: import_package(receiver, payload),
        )
        assert "越界条目" in error.message

    def test_an_absolute_member_is_rejected(self, receiver: Harness) -> None:
        """绝对路径条目单独判——**不能靠分段判**。

        ``/etc/passwd`` 按 ``/`` 切开的分段是 ``["etc", "passwd"]``，一个可疑的段都没有；
        只看分段的话它会被当成一份叫 ``etc/passwd`` 的内包文件，而解压时会被写到哪，
        取决于平台对开头那个 ``/`` 怎么解释。
        """
        payload = _zip({"/etc/passwd": b"x", PROFILE_FILENAME: _profile_json()})
        error = expect_error(
            ErrorCode.PROFILE_PACKAGE_INVALID,
            lambda: import_package(receiver, payload),
        )
        assert "绝对路径条目" in error.message

    def test_a_profile_referencing_a_missing_bundle_is_rejected(self, receiver: Harness) -> None:
        """**引脚⑤**：档案引用了包里没有的模板 → 拒，且**模板库一个字节都没动**。

        "一半装进去再说"在收件端最难查：库里凭空多了一条没人引用的登记，而导入报的是
        失败——下一次导入会撞上它。
        """
        payload = _zip({PROFILE_FILENAME: _profile_json()})
        error = expect_error(
            ErrorCode.PROFILE_PACKAGE_INVALID,
            lambda: import_package(receiver, payload),
        )
        assert TEMPLATE_REF in json.dumps(error.where, ensure_ascii=False)
        assert receiver.services.list_templates() == []

    def test_a_bundle_without_a_source_is_rejected(
        self, sender: Harness, receiver: Harness, template_bytes: bytes
    ) -> None:
        """包里那个模板包缺原件（``source/``）：拒。

        允许它进去的话，收件端会登记出一个 ``parsed=True`` 却没有原件的模板包——
        界面看着正常，构建一路走到 ``render`` 才报 ``E_TEMPLATE_UNREADABLE``。
        """
        payload, _ = prepared(sender, template_bytes)
        thinned = _drop_under(payload, f"{TEMPLATE_BUNDLE_DIRNAME}/{TEMPLATE_REF}/source/")
        expect_error(
            ErrorCode.PROFILE_PACKAGE_INVALID,
            lambda: import_package(receiver, thinned),
        )

    def test_an_empty_bundle_directory_is_rejected(
        self, sender: Harness, receiver: Harness
    ) -> None:
        """只有 ``templates/<ref>/`` 一条目录项、里面什么都没有：拒。

        静默当成空包会让"引用的模板在不在包里"判成"在"，然后登记出一个没有契约的模板
        ——失败点被推到构建期。
        """
        payload = _zip(
            {
                PROFILE_FILENAME: _profile_json(),
                f"{TEMPLATE_BUNDLE_DIRNAME}/{TEMPLATE_REF}/": b"",
            }
        )
        expect_error(
            ErrorCode.PROFILE_PACKAGE_INVALID,
            lambda: import_package(receiver, payload),
        )

    def test_an_unknown_package_format_is_rejected(self, receiver: Harness) -> None:
        """包说明里写了**读不懂的**格式标识：拒（这正是那个字段存在的理由）。"""
        payload = _rewrite_notes(b"", format_value="docfidelity-profile-package/99")
        error = expect_error(
            ErrorCode.PROFILE_PACKAGE_INVALID,
            lambda: import_package(receiver, payload),
        )
        assert "格式" in error.message

    def test_a_package_without_notes_still_imports(
        self, sender: Harness, receiver: Harness, template_bytes: bytes
    ) -> None:
        """**对立断言**：没有包说明的老包仍要能导入。

        把"没写格式标识"当成"格式不认识"，会让上一版导出的包在新服务端上全部失效——
        而它们的内容一点问题都没有。版本号缺失时回落到 ``1.0``（与 ``POST /profiles``
        同口径）。
        """
        from tests.fixtures.server_fixture import seed_domain_models

        payload, _ = prepared(sender, template_bytes)
        without = _zip(
            {
                name: data
                for name, data in entries_of(payload).items()
                if name != PACKAGE_NOTES_FILENAME
            }
        )
        seed_domain_models(receiver)
        result = import_package(receiver, without)
        assert result.registration.version == "1.0"


# ======================================================= 四、HTTP 层
class TestHttp:
    """端点形状、状态码与 multipart 形态（DD-13.2）。"""

    @pytest.fixture
    def client(self, receiver: Harness) -> TestClient:
        """与 ``receiver`` **同一份状态**的 HTTP 壳：接口测的就是那个服务端。"""
        return TestClient(
            create_app(
                settings=receiver.settings,
                repository=receiver.repository,
                stages=build_registry(),
                root=receiver.root,
                start_worker=False,
            )
        )

    def test_import_over_http(
        self, sender: Harness, receiver: Harness, client: TestClient, template_bytes: bytes
    ) -> None:
        from tests.fixtures.server_fixture import seed_domain_models

        payload, fingerprint = prepared(sender, template_bytes)
        seed_domain_models(receiver)
        response = client.post(
            "/api/v1/profiles/import",
            files={"file": ("api-reference@1.0.zip", payload, "application/zip")},
            params={"author": "森"},
        )
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["profile"]["ref"] == "api-reference@1.0"
        assert body["profile"]["fingerprint"] == fingerprint
        assert [item["ref"] for item in body["templates"]] == [TEMPLATE_REF]
        assert body["unused_templates"] == []

    def test_the_import_route_is_declared_before_the_profile_route(self) -> None:
        """``/profiles/import`` 必须声明在 ``/profiles/{profile_id}`` **之前**。

        两者都是两段路径、都以 ``{profile_id}`` 那一格收尾。FastAPI 按**声明顺序**匹配，
        故字面量必须先声明——否则 ``POST /profiles/import`` 会被那个通配路由抢先匹配、
        把 ``import`` 当成一个档案标识，报出一句"找不到档案：import"这种让人一头雾水的
        404。与 ``/export`` 是同一条纪律（见模块文档）。
        """
        from server.api.profiles import router

        paths = [route.path for route in router.routes if hasattr(route, "path")]
        assert paths.index("/api/v1/profiles/import") < paths.index("/api/v1/profiles/{profile_id}")

    def test_the_import_route_is_in_the_openapi_document(self, client: TestClient) -> None:
        schema = client.get("/openapi.json").json()
        operation = schema["paths"]["/api/v1/profiles/import"]["post"]
        assert operation["tags"] == ["profiles"]
        # 收件方要的是"把包丢进去"，故契约上是 multipart 文件而不是裸 body。
        assert "multipart/form-data" in operation["requestBody"]["content"]

    def test_a_bad_package_over_http_reports_the_engine_error_code(
        self, client: TestClient
    ) -> None:
        """失败的包走 HTTP 时回的是**引擎错误码**，不是 500。

        500 让界面只能说"服务器错误"，而收件方真正需要的是"这个包不能用，去要一份新的"。
        """
        response = client.post(
            "/api/v1/profiles/import",
            files={"file": ("broken.zip", b"not a zip", "application/zip")},
        )
        assert response.status_code == 400 or response.status_code == 422, response.text
        assert ErrorCode.PROFILE_PACKAGE_INVALID in response.text


# ------------------------------------------------------------------ 造包小工具
def _zip(entries: dict[str, bytes]) -> bytes:
    """按给定条目造一个 zip（与导出端同一个时刻常量无关，这里只造测试输入）。"""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    return buffer.getvalue()


def _profile_json() -> bytes:
    """一份引用 :data:`TEMPLATE_REF` 的档案本体字节。"""
    return bound_profile().model_dump_json(indent=2).encode("utf-8")


def _append_bundle(payload: bytes, ref: str) -> bytes:
    """在原包上追加一个**空壳模板包**（用来验"未引用的包"那条路径）。"""
    entries = entries_of(payload)
    entries[f"{TEMPLATE_BUNDLE_DIRNAME}/{ref}/contract/manifest.json"] = b"{}"
    return _zip(entries)


def _drop_under(payload: bytes, prefix: str) -> bytes:
    """删掉所有以 ``prefix`` 开头的条目。"""
    return _zip(
        {name: data for name, data in entries_of(payload).items() if not name.startswith(prefix)}
    )


def _rewrite_notes(payload: bytes, *, format_value: str) -> bytes:
    """把包说明里的 ``format`` 换成一个**本端读不懂**的值。

    ``payload`` 留空时从一份最小包起造——"格式不认识"这条判据在读包的第一时间就要
    生效，不需要真资产，故不必为了它去装一份模板。
    """
    if payload:
        entries = entries_of(payload)
    else:
        entries = {
            PROFILE_FILENAME: _profile_json(),
            f"{TEMPLATE_BUNDLE_DIRNAME}/{TEMPLATE_REF}/contract/manifest.json": b"{}",
        }
    raw = entries.get(PACKAGE_NOTES_FILENAME)
    notes: dict[str, object] = json.loads(raw.decode("utf-8")) if raw else {}
    notes["format"] = format_value
    entries[PACKAGE_NOTES_FILENAME] = json.dumps(notes, ensure_ascii=False).encode("utf-8")
    return _zip(entries)
