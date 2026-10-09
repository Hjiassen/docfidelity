"""模板库的读取与治理（P04 的八个页签 / 版本对比 / 废弃 / 导出；DD-13.2）。

与 ``test_server_templates.py`` 的分工
------------------------------------
那边测**一条流水线**走完（收件 → 解析 → 确认 → 登记，P05 第 1~5 步），它的状态是
"这次导入走到了哪一步"；本文件测**一座库**现在有什么（列出、看详情、比版本、废弃、
导出），它的状态是"库里现在有什么"。两者的寿命不同——一次导入会结束，库会一直在长。

三条引脚：

① **"未解析"不是"检查通过"**——``package=None`` 时两个问题清单都是空列表，界面必须
   按"未解析"渲染，而不是画成一张全绿的检查表（硬规则 11 的同一条理由）；
② **只标不删**——模板版本是配置与历史产物的引用键（``DocumentEntry.template_ref``），
   废弃只改登记元数据，包目录一个字节都不动，已产出的文档照旧可复现；
③ **导出件字节可复现**——同一份包两次导出完全相同（zip 时刻被钉死、条目按名排序），
   否则"你发我的就是你库里那一份"这句话没有凭据。
"""

from __future__ import annotations

import io
import zipfile
from collections.abc import Callable
from pathlib import Path
from xml.etree import ElementTree as etree

import pytest
from fastapi.testclient import TestClient

from docfidelity.contracts.errors import EngineError, ErrorCode
from docfidelity.pipeline import summarize
from server.main import create_app
from server.settings import ServerSettings
from server.storage import MemoryRepository, Repository
from tests.fixtures.docx_builder import rewrite_part
from tests.fixtures.server_fixture import Harness, build_harness, build_registry

pytestmark = pytest.mark.api

#: 夹具模板文件名。
TEMPLATE_NAME = "template.docx"

#: ``word`` 主命名空间（与 ``tests.fixtures.docx_builder`` 里那份是同一个串）。
W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"

#: 改写部件时用的部件名。
STYLES_PART = "word/styles.xml"


@pytest.fixture
def harness(tmp_path: Path, repo: Repository) -> Harness:
    """与其它接口测同一套装配：两种仓储下各跑一遍。"""
    return build_harness(tmp_path, repository=repo)


def expect_error(code: str, fn: Callable[[], object]) -> EngineError:
    with pytest.raises(EngineError) as caught:
        fn()
    assert caught.value.code == code, caught.value.message
    return caught.value


def _bump_heading_size(root: etree.Element) -> None:
    """把 Heading1 的字号从 14pt 改成 16pt（``w:sz`` 的单位是半磅）。

    只动这一个部位，是为了让"变了什么"完全可控：测试要能说清"这一处改动只该影响
    ``styles`` 分区"，任何顺带产生的差异都会让断言失去意义。
    """
    for style in root.iter(f"{{{W}}}style"):
        if style.get(f"{{{W}}}styleId") != "Heading1":
            continue
        for size in style.iter(f"{{{W}}}sz"):
            size.set(f"{{{W}}}val", "32")


# ======================================================= 一、详情与版本列表
class TestDetail:
    @pytest.fixture
    def resized_bytes(self, template_docx: Path, tmp_path: Path) -> bytes:
        """改过一处样式的对照件（当作"版本 2.0"）。"""
        target = rewrite_part(
            template_docx, tmp_path / "resized.docx", STYLES_PART, _bump_heading_size
        )
        return target.read_bytes()

    def _register(self, harness: Harness, payload: bytes, *, version: str) -> str:
        record = harness.services.import_template(
            filename=TEMPLATE_NAME, payload=payload, template_id="TPL-LIB", version=version
        )
        done = harness.services.register_template(record.import_id, operator="森")
        assert done.registration is not None, [
            stage.stage for stage in record.stages if not stage.ok
        ]
        return done.registration.ref

    def test_carries_the_contract_and_both_checks(
        self, harness: Harness, template_bytes: bytes
    ) -> None:
        """两条检查各查一件事，都要有结论：包与清单是否一致、指纹与包内容是否一致。"""
        self._register(harness, template_bytes, version="1.0")
        entry, package, integrity, fingerprint_problems = harness.services.template_detail(
            "TPL-LIB"
        )
        assert entry.ref == "TPL-LIB@1.0"
        assert package is not None, "已解析的登记必须能读回整份契约"
        assert summarize(package).style_count > 0
        assert integrity == []
        assert fingerprint_problems == []

    def test_of_an_unparsed_registration_has_no_package(self, harness: Harness) -> None:
        """**空清单不是"检查通过"**，而是"没有可查的东西"——界面必须按未解析渲染。

        画成一张全绿的检查表就是"缺能力跳过伪装成校验通过"（硬规则 11）。
        """
        record = harness.services.import_template(filename="broken.docx", payload=b"PK\x03\x04nope")
        harness.services.register_template(
            record.import_id, template_id="BROKEN-LIB", version="0.1"
        )
        entry, package, integrity, fingerprint_problems = harness.services.template_detail(
            "BROKEN-LIB"
        )
        assert entry.parsed is False
        assert package is None
        assert integrity == [] and fingerprint_problems == []

    def test_takes_the_latest_version_by_default(
        self, harness: Harness, template_bytes: bytes, resized_bytes: bytes
    ) -> None:
        """``version`` 留空 = 当前有效的那一版；"最新"由仓储的排序给出（两实现同一次序）。"""
        self._register(harness, template_bytes, version="1.0")
        harness.clock.advance(minutes=5)
        self._register(harness, resized_bytes, version="2.0")
        assert [item.ref for item in harness.services.list_templates()] == [
            "TPL-LIB@1.0",
            "TPL-LIB@2.0",
        ]
        assert harness.services.get_template("TPL-LIB").version == "2.0"
        assert harness.services.get_template("TPL-LIB", "1.0").version == "1.0"

    def test_unknown_template_is_not_found(self, harness: Harness) -> None:
        expect_error(ErrorCode.NOT_FOUND, lambda: harness.services.get_template("nope"))
        expect_error(ErrorCode.NOT_FOUND, lambda: harness.services.get_template("nope", "1.0"))

    def test_unknown_version_is_not_found(self, harness: Harness, template_bytes: bytes) -> None:
        self._register(harness, template_bytes, version="1.0")
        expect_error(ErrorCode.NOT_FOUND, lambda: harness.services.get_template("TPL-LIB", "9.9"))


# =============================================================== 二、版本对比
class TestDiff:
    @pytest.fixture
    def two_versions(
        self, harness: Harness, template_bytes: bytes, tmp_path: Path, template_docx: Path
    ) -> Harness:
        """1.0 = 原样，2.0 = 只改了一处样式。"""
        changed = rewrite_part(
            template_docx, tmp_path / "resized.docx", STYLES_PART, _bump_heading_size
        ).read_bytes()
        for version, payload in (("1.0", template_bytes), ("2.0", changed)):
            record = harness.services.import_template(
                filename=TEMPLATE_NAME, payload=payload, template_id="TPL-LIB", version=version
            )
            harness.services.register_template(record.import_id, operator="森")
        return harness

    def test_reports_the_changed_section(self, two_versions: Harness) -> None:
        """分区由**服务端**算好（``changed_sections`` / ``blocking_sections``），
        界面不做筛选（NFR-22：判定与统计都在服务端）。"""
        diff = two_versions.services.template_diff("TPL-LIB", "1.0", "2.0")
        assert diff.identical is False
        assert diff.ok is True
        assert "styles" in diff.changed_sections
        assert diff.before_ref == "TPL-LIB@1.0"
        assert diff.after_ref == "TPL-LIB@2.0"
        assert diff.summary

    def test_the_same_package_has_no_diff(self, harness: Harness, template_bytes: bytes) -> None:
        """同一份内容登记成两个版本号，对比结果必须是"没有差异"。"""
        for version in ("1.0", "2.0"):
            record = harness.services.import_template(
                filename=TEMPLATE_NAME,
                payload=template_bytes,
                template_id="TPL-LIB",
                version=version,
            )
            harness.services.register_template(record.import_id, operator="森")
        diff = harness.services.template_diff("TPL-LIB", "1.0", "2.0")
        assert diff.identical is True
        assert diff.changed_sections == []

    def test_refuses_a_version_that_was_only_registered_as_an_original(
        self, harness: Harness, template_bytes: bytes
    ) -> None:
        """未解析的登记**没有契约可比**，故报 ``E_NO_BASELINE`` 而不是给一份"无差异"。

        回一份空的差异表会让"这一版根本没解析"看起来像"两版一模一样"。
        """
        record = harness.services.import_template(
            filename=TEMPLATE_NAME, payload=template_bytes, template_id="TPL-LIB", version="1.0"
        )
        harness.services.register_template(record.import_id, operator="森")
        broken = harness.services.import_template(filename="broken.docx", payload=b"PK\x03\x04nope")
        harness.services.register_template(broken.import_id, template_id="TPL-LIB", version="2.0")
        expect_error(
            ErrorCode.NO_BASELINE,
            lambda: harness.services.template_diff("TPL-LIB", "1.0", "2.0"),
        )


# =================================================================== 三、废弃
class TestDeprecate:
    @pytest.fixture
    def registered(self, harness: Harness, template_bytes: bytes) -> Harness:
        record = harness.services.import_template(
            filename=TEMPLATE_NAME, payload=template_bytes, template_id="TPL-LIB", version="1.0"
        )
        harness.services.register_template(record.import_id, operator="森")
        return harness

    def test_requires_a_reason(self, registered: Harness) -> None:
        expect_error(
            ErrorCode.CONFIG_INVALID,
            lambda: registered.services.deprecate_template("TPL-LIB", version="1.0", reason="   "),
        )

    def test_marks_the_entry_and_keeps_the_package(self, registered: Harness) -> None:
        """只标不删：模板版本是配置与历史产物的引用键，包目录一个字节都不动。"""
        entry = registered.services.deprecate_template(
            "TPL-LIB", version="1.0", reason="被 2.0 取代", operator="森"
        )
        assert entry.deprecated is True
        assert entry.deprecated_reason == "被 2.0 取代"
        assert entry.deprecated_by == "森"
        assert Path(entry.package_dir).is_dir(), "已产出的文档照旧可复现"
        assert registered.services.list_templates(include_deprecated=False) == []
        assert len(registered.services.list_templates()) == 1

    def test_is_idempotent_and_keeps_the_first_reason(self, registered: Harness) -> None:
        registered.services.deprecate_template("TPL-LIB", version="1.0", reason="第一次")
        again = registered.services.deprecate_template("TPL-LIB", version="1.0", reason="第二次")
        assert again.deprecated_reason == "第一次"


# =================================================================== 四、导出
class TestExport:
    def test_is_byte_identical_and_carries_the_manifest(
        self, harness: Harness, template_bytes: bytes
    ) -> None:
        """导出件是"交给第三方复现"的凭据：同一份包两次导出**字节相同**。"""
        record = harness.services.import_template(
            filename=TEMPLATE_NAME, payload=template_bytes, template_id="TPL-LIB", version="1.0"
        )
        harness.services.register_template(record.import_id, operator="森")
        name, first = harness.services.export_template("TPL-LIB")
        assert name == "TPL-LIB@1.0.zip"
        _, second = harness.services.export_template("TPL-LIB")
        assert first == second
        with zipfile.ZipFile(io.BytesIO(first)) as archive:
            assert "contract/manifest.json" in archive.namelist()

    def test_refuses_an_unparsed_registration(self, harness: Harness) -> None:
        record = harness.services.import_template(filename="broken.docx", payload=b"PK\x03\x04nope")
        harness.services.register_template(
            record.import_id, template_id="BROKEN-LIB", version="0.1"
        )
        expect_error(
            ErrorCode.PACKAGE_INCOMPLETE, lambda: harness.services.export_template("BROKEN-LIB")
        )


# =============================================================== 五、HTTP 层
class TestGovernanceHttp:
    """治理类端点的形状与状态码（P04 要用的那四条）。"""

    @pytest.fixture
    def client(self, tmp_path: Path) -> TestClient:
        root = tmp_path / "http-gov"
        app = create_app(
            settings=ServerSettings(_env_file=None, storage_root=root),
            repository=MemoryRepository(),
            stages=build_registry(),
            root=root,
            start_worker=False,
        )
        return TestClient(app)

    def _register(self, client: TestClient, payload: bytes, version: str) -> None:
        created = client.post(
            "/api/v1/templates/imports",
            files={"file": (TEMPLATE_NAME, payload, "application/octet-stream")},
            data={"template_id": "TPL-D", "version": version},
        )
        assert created.status_code == 201, created.text
        registered = client.post(
            f"/api/v1/templates/imports/{created.json()['import_id']}/register",
            json={"on_conflict": "abort", "operator": "森"},
        )
        assert registered.status_code == 200, registered.text

    def test_detail_diff_package_and_deprecate(
        self, client: TestClient, template_bytes: bytes
    ) -> None:
        self._register(client, template_bytes, "1.0")
        self._register(client, template_bytes, "2.0")

        detail = client.get("/api/v1/templates/TPL-D", params={"version": "1.0"})
        assert detail.status_code == 200, detail.text
        body = detail.json()
        assert body["entry"]["version"] == "1.0"
        assert body["parsed"] is True
        assert body["package"] is not None
        assert body["summary"]["style_count"] > 0
        assert [item["version"] for item in body["versions"]] == ["1.0", "2.0"]

        diff = client.get("/api/v1/templates/TPL-D/diff", params={"from": "1.0", "to": "2.0"})
        assert diff.status_code == 200, diff.text
        assert diff.json()["identical"] is True
        assert diff.json()["before_ref"] == "TPL-D@1.0"

        exported = client.get("/api/v1/templates/TPL-D/package", params={"version": "2.0"})
        assert exported.status_code == 200, exported.text
        assert exported.headers["content-type"] == "application/zip"
        assert "TPL-D@2.0.zip" in exported.headers["content-disposition"]

        deprecated = client.post(
            "/api/v1/templates/TPL-D/deprecate",
            json={"version": "1.0", "reason": "被 2.0 取代", "operator": "森"},
        )
        assert deprecated.status_code == 200, deprecated.text
        assert deprecated.json()["deprecated"] is True
        assert deprecated.json()["deprecated_by"] == "森"

        hidden = client.get("/api/v1/templates", params={"include_deprecated": False})
        assert hidden.json()["count"] == 1
        assert hidden.json()["items"][0]["version"] == "2.0"

    def test_deprecate_without_a_reason_is_a_422(
        self, client: TestClient, template_bytes: bytes
    ) -> None:
        self._register(client, template_bytes, "1.0")
        response = client.post(
            "/api/v1/templates/TPL-D/deprecate",
            json={"version": "1.0", "reason": ""},
        )
        assert response.status_code == 422, response.text

    def test_diff_of_the_same_version_is_identical(
        self, client: TestClient, template_bytes: bytes
    ) -> None:
        """``from`` / ``to`` 是 Python 关键字，线上名字靠 ``alias`` 对齐设计文档——
        这条顺带证明那两个查询参数真的叫 ``from`` / ``to``（而不是被改名成了
        ``from_version``）。"""
        self._register(client, template_bytes, "1.0")
        response = client.get("/api/v1/templates/TPL-D/diff", params={"from": "1.0", "to": "1.0"})
        assert response.status_code == 200, response.text
        assert response.json()["identical"] is True

    def test_diff_without_both_bounds_is_a_422(
        self, client: TestClient, template_bytes: bytes
    ) -> None:
        self._register(client, template_bytes, "1.0")
        response = client.get("/api/v1/templates/TPL-D/diff", params={"from": "1.0"})
        assert response.status_code == 422, response.text

    def test_governance_paths_are_published(self, client: TestClient) -> None:
        paths = client.get("/openapi.json").json()["paths"]
        assert "/api/v1/templates/{template_id}/versions" in paths
        assert "/api/v1/templates/{template_id}/diff" in paths
        assert "/api/v1/templates/{template_id}/deprecate" in paths
        assert "/api/v1/templates/{template_id}/package" in paths
