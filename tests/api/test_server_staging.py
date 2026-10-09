"""导入暂存目录的回收（``imports/<import_id>/``；``GET/POST /maintenance/staging*``）。

这一组用例要守住的是**判据本身**，而不是"能不能删掉一个目录"：

① **未登记的暂存不回收**——它带着"稍后重试解析"的承诺（``import_template`` 把原件另存
   一份正是为它）。这条必须有对立断言：同一个目录，登记之后就必须变成可回收。
② **回收只动暂存**——``templates/<tid>/<ver>/`` 与仓储里的登记行都不动，所以回收之后再
   列模板库，那一条仍在。少了这条断言，"清理"就变成了一次没有人察觉的退库。
③ **必须逐个列出**——沿用 DD-13.2 的清理纪律：空请求是"把能删的都删了吧"，拒。
④ **读不出来的目录要列出来但不能删**——它恰恰是最该被看见的那一项，过滤掉等于藏起来。

与 ``test_server_templates.py`` 的分界：那边验导入流水线（收件 / 确认 / 登记），
这边只验暂存目录的**回收**。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from docfidelity.contracts.errors import EngineError, ErrorCode
from server.main import create_app
from server.settings import ServerSettings
from server.staging import (
    NOT_REGISTERED_REASON,
    RECLAIMABLE_NOTE,
    UNREADABLE_REASON,
)
from server.storage import MemoryRepository, Repository
from tests.fixtures.server_fixture import Harness, build_harness, build_registry

pytestmark = pytest.mark.api

TEMPLATE_NAME = "template.docx"


@pytest.fixture
def harness(tmp_path: Path, repo: Repository) -> Harness:
    return build_harness(tmp_path, repository=repo)


def expect_error(code: str, fn: Callable[[], object]) -> EngineError:
    with pytest.raises(EngineError) as caught:
        fn()
    assert caught.value.code == code, caught.value.message
    return caught.value


def _import(harness: Harness, template_bytes: bytes) -> str:
    record = harness.services.import_template(
        filename=TEMPLATE_NAME,
        payload=template_bytes,
        template_id="TPL-1",
        version="1.0",
        author="森",
    )
    return record.import_id


def _register(harness: Harness, import_id: str) -> None:
    harness.services.register_template(import_id, template_id="TPL-1", version="1.0", operator="森")


def _staging_dir(harness: Harness, import_id: str) -> Path:
    return harness.settings.imports_root / import_id


# ==================================================== 一、判据：登记了没有
class TestReclaimability:
    def test_an_unregistered_import_is_listed_but_blocked(
        self, harness: Harness, template_bytes: bytes
    ) -> None:
        import_id = _import(harness, template_bytes)
        (item,) = harness.services.staging_imports()
        assert item.import_id == import_id
        assert item.readable is True
        assert item.registered is False
        assert item.reclaimable is False
        # 措辞也由服务端给：界面直接显示这句话，不自己归纳规则（硬规则 15）。
        assert item.blocked_reason == NOT_REGISTERED_REASON
        assert item.size_bytes > 0

    def test_registering_makes_the_same_directory_reclaimable(
        self, harness: Harness, template_bytes: bytes
    ) -> None:
        """对立断言：同一个目录，登记之后判据必须翻转。"""
        import_id = _import(harness, template_bytes)
        _register(harness, import_id)
        (item,) = harness.services.staging_imports()
        assert item.registered is True
        assert item.reclaimable is True
        assert item.blocked_reason is None
        assert item.template_ref == "TPL-1@1.0"

    def test_an_unreadable_record_is_listed_with_its_own_reason(
        self, harness: Harness, template_bytes: bytes
    ) -> None:
        """记录坏了 → 仍在清单里，但理由是**另一句**（不是"未登记"）。

        两种"不能回收"的成因不同，界面上要能分开看：一个是"还没走完"，一个是"目录被
        外部动过"。用同一句话会把后者藏进前者里。
        """
        import_id = _import(harness, template_bytes)
        _register(harness, import_id)
        # 把记录写坏（只留一个残缺 JSON）——模拟目录被外部动过。
        (_staging_dir(harness, import_id) / "import.json").write_text("{", encoding="utf-8")
        (item,) = harness.services.staging_imports()
        assert item.import_id == import_id
        assert item.readable is False
        assert item.reclaimable is False
        assert item.blocked_reason == UNREADABLE_REASON
        # 读不出来时退回目录 mtime，仍然给得出一个时间（界面上只是"大概什么时候来的"）。
        assert item.created_at is not None

    def test_a_directory_without_a_record_is_still_listed(
        self, harness: Harness, template_bytes: bytes
    ) -> None:
        """只有目录、没有 ``import.json``：扫得出来、不可回收。"""
        import_id = _import(harness, template_bytes)
        (harness.settings.imports_root / "i_orphan").mkdir(parents=True)
        listed = {item.import_id: item for item in harness.services.staging_imports()}
        assert set(listed) == {import_id, "i_orphan"}
        assert listed["i_orphan"].readable is False
        assert listed["i_orphan"].size_bytes == 0

    def test_no_staging_root_yields_an_empty_list(self, harness: Harness) -> None:
        """一个导入都没有时不许报错——空态要能渲染。"""
        assert harness.settings.imports_root.is_dir() is False
        assert harness.services.staging_imports() == []


# ==================================================== 二、回收：只动暂存
class TestPurge:
    def test_purge_removes_the_staging_directory(
        self, harness: Harness, template_bytes: bytes
    ) -> None:
        import_id = _import(harness, template_bytes)
        _register(harness, import_id)
        reclaimed = harness.services.purge_staging(requested=[import_id])
        assert [item.import_id for item in reclaimed] == [import_id]
        assert reclaimed[0].size_bytes > 0
        assert _staging_dir(harness, import_id).exists() is False
        assert harness.services.staging_imports() == []

    def test_purge_leaves_the_template_library_alone(
        self, harness: Harness, template_bytes: bytes
    ) -> None:
        """回收只删暂存那份副本：库里的包与登记行都不动。

        少了这条，"清理"就变成一次没有人察觉的退库——而历史产物还引用着这个版本。
        """
        import_id = _import(harness, template_bytes)
        _register(harness, import_id)
        before = harness.services.list_templates()
        harness.services.purge_staging(requested=[import_id])
        after = harness.services.list_templates()
        assert [item.ref for item in after] == [item.ref for item in before]
        package_dir = harness.settings.templates_root / "TPL-1" / "1.0"
        assert package_dir.is_dir()
        assert any(package_dir.iterdir())

    def test_the_import_record_is_gone_after_the_purge(
        self, harness: Harness, template_bytes: bytes
    ) -> None:
        """回收之后读导入记录就是 404——暂存没了，报告自然也没了（如实回答）。"""
        import_id = _import(harness, template_bytes)
        _register(harness, import_id)
        harness.services.purge_staging(requested=[import_id])
        expect_error(ErrorCode.NOT_FOUND, lambda: harness.services.get_import(import_id))

    def test_an_empty_request_is_rejected(self, harness: Harness, template_bytes: bytes) -> None:
        """空请求 = "把能删的都删了吧"，正是最危险的那次调用（沿用 DD-13.2 的纪律）。"""
        import_id = _import(harness, template_bytes)
        _register(harness, import_id)
        error = expect_error(
            ErrorCode.CONFIRM_REQUIRED, lambda: harness.services.purge_staging(requested=[])
        )
        assert _staging_dir(harness, import_id).is_dir()
        # 补救项里要带上判据原文，否则使用者只知道"被拦了"。
        assert RECLAIMABLE_NOTE in error.remediation

    def test_an_unregistered_import_cannot_be_purged(
        self, harness: Harness, template_bytes: bytes
    ) -> None:
        import_id = _import(harness, template_bytes)
        error = expect_error(
            ErrorCode.CONFIRM_REQUIRED,
            lambda: harness.services.purge_staging(requested=[import_id]),
        )
        assert _staging_dir(harness, import_id).is_dir()
        # 拒绝的理由要说清是**哪一个**、以及为什么——不能只说"有的不能删"。
        assert import_id in error.message
        assert NOT_REGISTERED_REASON in error.message

    def test_an_unknown_import_is_not_found(self, harness: Harness, template_bytes: bytes) -> None:
        _import(harness, template_bytes)
        expect_error(
            ErrorCode.NOT_FOUND,
            lambda: harness.services.purge_staging(requested=["i_missing"]),
        )

    def test_a_half_blocked_request_deletes_nothing(
        self, harness: Harness, template_bytes: bytes
    ) -> None:
        """一项可删、一项不可删 → **整体拒绝**，可删的那项也不动。

        先删一半再报错会留下一个谁也说不清的状态；"要么都成、要么都不成"在这里是
        可以做到的，那就该做到。
        """
        ready = _import(harness, template_bytes)
        _register(harness, ready)
        blocked = _import(harness, template_bytes)
        expect_error(
            ErrorCode.CONFIRM_REQUIRED,
            lambda: harness.services.purge_staging(requested=[ready, blocked]),
        )
        assert _staging_dir(harness, ready).is_dir()
        assert _staging_dir(harness, blocked).is_dir()

    def test_duplicate_ids_are_treated_as_a_set(
        self, harness: Harness, template_bytes: bytes
    ) -> None:
        import_id = _import(harness, template_bytes)
        _register(harness, import_id)
        reclaimed = harness.services.purge_staging(requested=[import_id, import_id])
        assert [item.import_id for item in reclaimed] == [import_id]


# ================================================================= 三、HTTP
class TestHttp:
    @pytest.fixture
    def client(self, tmp_path: Path) -> TestClient:
        root = tmp_path / "http"
        app = create_app(
            settings=ServerSettings(_env_file=None, storage_root=root),
            repository=MemoryRepository(),
            stages=build_registry(),
            root=root,
            start_worker=False,
        )
        return TestClient(app)

    def _import_over_http(self, client: TestClient, template_bytes: bytes) -> str:
        response = client.post(
            "/api/v1/templates/imports",
            files={
                "file": (
                    TEMPLATE_NAME,
                    template_bytes,
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                )
            },
            data={"template_id": "TPL-1", "version": "1.0", "author": "森"},
        )
        assert response.status_code == 201, response.text
        return str(response.json()["import_id"])

    def test_the_listing_carries_both_counts_and_the_note(
        self, client: TestClient, template_bytes: bytes
    ) -> None:
        response = client.get("/api/v1/maintenance/staging")
        assert response.status_code == 200, response.text
        assert response.json() == {
            "imports": [],
            "count": 0,
            "reclaimable_count": 0,
            "total_bytes": 0,
            "reclaimable_bytes": 0,
            "note": RECLAIMABLE_NOTE,
        }

        import_id = self._import_over_http(client, template_bytes)
        body = client.get("/api/v1/maintenance/staging").json()
        assert body["count"] == 1
        assert body["reclaimable_count"] == 0
        assert body["reclaimable_bytes"] == 0
        assert body["total_bytes"] > 0
        (item,) = body["imports"]
        assert item["import_id"] == import_id
        assert item["blocked_reason"] == NOT_REGISTERED_REASON
        # 落点不外泄的纪律在维护面上不适用：这一页的用途就是让人看见哪个目录。
        assert Path(item["path"]).name == import_id

        registered = client.post(
            f"/api/v1/templates/imports/{import_id}/register",
            json={
                "template_id": "TPL-1",
                "version": "1.0",
                "operator": "森",
                "on_conflict": "abort",
            },
        )
        assert registered.status_code == 200, registered.text
        body = client.get("/api/v1/maintenance/staging").json()
        assert body["count"] == 1
        assert body["reclaimable_count"] == 1
        assert body["reclaimable_bytes"] == body["total_bytes"]

    def test_purge_over_http_returns_a_receipt(
        self, client: TestClient, template_bytes: bytes
    ) -> None:
        import_id = self._import_over_http(client, template_bytes)
        client.post(
            f"/api/v1/templates/imports/{import_id}/register",
            json={"template_id": "TPL-1", "version": "1.0", "operator": "森"},
        )
        response = client.post("/api/v1/maintenance/staging/purge", json={"objects": [import_id]})
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["count"] == 1
        assert body["total_bytes"] > 0
        (item,) = body["reclaimed"]
        assert item["import_id"] == import_id
        assert item["reclaimable"] is True
        assert client.get(f"/api/v1/templates/imports/{import_id}").status_code == 404

    def test_an_empty_request_is_a_422_over_http(self, client: TestClient) -> None:
        response = client.post("/api/v1/maintenance/staging/purge", json={"objects": []})
        assert response.status_code == 400
        assert response.json()["code"] == ErrorCode.CONFIRM_REQUIRED

    def test_a_blocked_request_is_a_4xx_with_the_reason(
        self, client: TestClient, template_bytes: bytes
    ) -> None:
        import_id = self._import_over_http(client, template_bytes)
        response = client.post("/api/v1/maintenance/staging/purge", json={"objects": [import_id]})
        assert response.status_code == 400
        assert NOT_REGISTERED_REASON in response.json()["message"]

    def test_unknown_fields_in_the_purge_body_are_rejected(self, client: TestClient) -> None:
        """请求体基类 ``extra="forbid"``：多传的字段往往意味着前端版本不匹配。"""
        response = client.post(
            "/api/v1/maintenance/staging/purge",
            json={"objects": [], "confirm": True},
        )
        assert response.status_code == 422

    def test_the_report_codec_round_trips_registration_timestamps(self) -> None:
        """回归：登记回执里的 ``registered_at`` 必须还原成**时间**。

        只解析一部分字段的编解码器有个很难查的表现——第一次读没问题（响应模型会把 ISO
        字符串当时间解析），**再写一次**才炸在 ``.isoformat()`` 上，而那时离"哪个字段
        漏了"已经很远。这里直接压在编解码器上，与用例层解耦。
        """
        from datetime import datetime, timezone

        from server.import_record import TemplateRegistration, read_record, record_payload
        from server.templates import TemplateImportRecord

        record = TemplateImportRecord(
            import_id="i_x",
            created_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
            source_filename="x.docx",
            source_size_bytes=1,
            registration=TemplateRegistration(
                template_id="TP",
                version="1",
                ref="TP@1",
                registered_at=datetime(2026, 9, 2, tzinfo=timezone.utc),
            ),
        )
        # 写 → 读 → 写：第二次写盘才是真正的判据。
        twice = read_record(
            json.loads(json.dumps(record_payload(read_record(record_payload(record)))))
        )
        assert twice.registration is not None
        assert isinstance(twice.registration.registered_at, datetime)
        assert twice.registration.registered_at == record.registration.registered_at
