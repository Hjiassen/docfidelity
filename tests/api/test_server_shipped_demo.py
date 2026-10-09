"""发行版演示入口必须从空库走到三份真实产物。"""

from __future__ import annotations

import json
import shutil
import zipfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

import pytest
from fastapi.testclient import TestClient

from docfidelity.builder import build_registry
from server.api.deps import AppState
from server.main import APP_STATE_ATTRIBUTE, create_app
from server.seed import seed_demo_templates
from server.settings import ServerSettings
from server.storage import Repository

pytestmark = pytest.mark.api
REPO_ROOT = Path(__file__).resolve().parents[2]
DOC_KEYS = {"api-sectioned", "api-summary", "procedure-sop"}


@pytest.fixture
def demo_app(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, repo: Repository
) -> Iterator[tuple[TestClient, AppState]]:
    """真实库与参考阶段；禁止模型外呼和本机 Office 参与。"""
    from docfidelity.render import toc

    def unexpected_office(*_args: Any, **_kwargs: Any) -> None:
        pytest.fail("发行版演示测试不得探测或启动 Office")

    monkeypatch.setattr(toc, "office_available", unexpected_office)
    monkeypatch.setattr(toc, "refresh_with_office", unexpected_office)
    app = create_app(
        settings=ServerSettings(
            _env_file=None,
            storage_root=tmp_path / "storage",
            demo_templates_dir=REPO_ROOT / "demo-templates",
            seed_demo_templates=False,
            model_enabled=False,
            deterministic_mode=True,
            office_enabled=False,
        ),
        repository=repo,
        stages=build_registry(),
        start_worker=False,
    )
    with TestClient(app) as client:
        yield client, getattr(app.state, APP_STATE_ATTRIBUTE)


def _create(client: TestClient, name: str = "自制演示") -> str:
    response = client.post("/api/v1/projects", json={"name": name, "demo": True})
    assert response.status_code == 201, response.text
    return str(response.json()["project_id"])


def _document_text(path: Path) -> str:
    with zipfile.ZipFile(path) as archive:
        root = ElementTree.fromstring(archive.read("word/document.xml"))
    return "".join(root.itertext())


def test_empty_library_demo_creates_three_real_documents(
    demo_app: tuple[TestClient, AppState],
) -> None:
    client, state = demo_app
    assert state.services.list_templates() == []
    project_id = _create(client)

    response = client.get(f"/api/v1/projects/{project_id}/config")
    assert response.status_code == 200, response.text
    config = response.json()
    assert {entry["doc_key"] for entry in config["documents"]} == DOC_KEYS
    assert all(entry["source_ids"] for entry in config["documents"])
    check = client.post(f"/api/v1/projects/{project_id}/config/validate")
    assert check.status_code == 200, check.text
    assert check.json()["ok"] is True, check.text

    response = client.post(f"/api/v1/projects/{project_id}/jobs", json={})
    assert response.status_code == 201, response.text
    orchestration_id = response.json()["job_id"]
    state.runner.run_until_idle()
    orchestration = state.services.get_job(orchestration_id)
    assert len(orchestration.child_job_ids) == 3
    artifacts: dict[str, str] = {}
    for child_id in orchestration.child_job_ids:
        child = state.services.get_job(child_id)
        assert child.status == "succeeded", child.model_dump_json()
        job_dir = state.root / "jobs" / child_id
        delivered = list((job_dir / "artifacts").glob("*.docx"))
        assert len(delivered) == 1
        artifacts[child.doc_key or ""] = _document_text(delivered[0])
        snapshot = json.loads((job_dir / "work" / "facts.json").read_text(encoding="utf-8"))
        assert snapshot["merged"]
    assert set(artifacts) == DOC_KEYS
    for key in ("api-sectioned", "api-summary"):
        assert "/orders" in artifacts[key]
        assert "创建订单" in artifacts[key]
    assert "运行空白校准" in artifacts["procedure-sop"]


def test_browser_demo_init_uses_shipped_assets(demo_app: tuple[TestClient, AppState]) -> None:
    client, state = demo_app
    response = client.post("/api/v1/projects", json={"name": "浏览器初始化"})
    project_id = response.json()["project_id"]
    initialized = client.post(f"/api/v1/projects/{project_id}/demo-init")
    assert initialized.status_code == 200, initialized.text
    assert {entry["doc_key"] for entry in initialized.json()["documents"]} == DOC_KEYS
    assert state.services.precheck(project_id).ok
    assert state.repository.get_project(project_id).demo is True
    repeated = client.post(f"/api/v1/projects/{project_id}/demo-init")
    assert repeated.json() == initialized.json()
    assert len(state.services.list_config_versions(project_id)) == 1


def test_demo_projects_isolate_sources_and_initialization_is_idempotent(
    demo_app: tuple[TestClient, AppState],
) -> None:
    from server.demo import initialize_demo_project

    client, state = demo_app
    first = _create(client, "演示一")
    second = _create(client, "演示二")
    first_sources = state.services.list_sources(first)
    second_sources = state.services.list_sources(second)
    assert {record.source_id for record in first_sources} == {
        "interface-surface",
        "operations-workbook",
    }
    for left, right in zip(first_sources, second_sources, strict=True):
        left_path = state.settings.storage_root / left.config.root
        right_path = state.settings.storage_root / right.config.root
        assert left_path != right_path
        assert left_path.read_bytes() == right_path.read_bytes()
        assert first in left_path.parts
        assert second in right_path.parts

    first_path = state.settings.storage_root / first_sources[0].config.root
    second_path = state.settings.storage_root / second_sources[0].config.root
    shipped_source = second_path.read_bytes()
    first_path.write_bytes(b"local edit")
    before = state.services.get_config(first)
    assert initialize_demo_project(state.services, first) == before
    assert len(state.services.list_config_versions(first)) == 1
    assert len(state.services.list_sources(first)) == 2
    assert len(state.services.list_templates()) == 2
    assert len(state.services.list_profiles()) == 3
    assert len(state.services.list_domain_models()) == 6
    assert first_path.read_bytes() == b"local edit"
    assert second_path.read_bytes() == shipped_source


def test_demo_reuses_templates_already_seeded_at_startup(
    demo_app: tuple[TestClient, AppState], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, state = demo_app
    seeded = seed_demo_templates(state.services, root=state.settings.demo_templates_dir)
    assert seeded.ok and len(seeded.seeded) == 2
    before = state.services.list_templates()

    def unexpected_import(*_args: Any, **_kwargs: Any) -> Any:
        pytest.fail("已播种的演示模板不得重复入库解析")

    monkeypatch.setattr(state.services, "import_template", unexpected_import)
    project_id = _create(client)
    assert state.services.precheck(project_id).ok
    assert state.services.list_templates() == before


def test_demo_does_not_overwrite_a_conflicting_user_domain_model(
    demo_app: tuple[TestClient, AppState],
) -> None:
    from docfidelity.contracts.domain import DomainModel

    client, state = demo_app
    declaration = json.loads((REPO_ROOT / "examples" / "domains.json").read_text(encoding="utf-8"))
    declaration[0]["title"] = "用户维护的声明"
    model = DomainModel.model_validate(declaration[0])
    state.services.register_domain_model(model=model, author="user")
    before = state.services.get_domain_model(model.kind)

    response = client.post("/api/v1/projects", json={"name": "已有声明", "demo": True})
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "E_VERSION_CONFLICT"
    assert response.json()["remediation"]
    assert state.services.get_domain_model(model.kind) == before
    assert state.services.list_projects() == []


def test_demo_rejects_a_different_template_with_the_same_shipped_identity(
    demo_app: tuple[TestClient, AppState], template_docx: Path
) -> None:
    client, state = demo_app
    imported = state.services.import_template(
        filename=template_docx.name,
        payload=template_docx.read_bytes(),
        template_id="demo-software-design",
        version="1",
        author="user",
    )
    state.services.register_template(imported.import_id, operator="user")
    before = state.services.get_template("demo-software-design", "1")

    response = client.post("/api/v1/projects", json={"name": "同名模板", "demo": True})
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "E_VERSION_CONFLICT"
    assert response.json()["remediation"]
    assert state.services.get_template("demo-software-design", "1") == before
    assert state.services.list_projects() == []


def test_demo_rejects_a_conflicting_active_newer_profile_version(
    demo_app: tuple[TestClient, AppState],
) -> None:
    client, state = demo_app
    _create(client)
    profile = state.services.profile_document("api-sectioned").model_copy(deep=True)
    profile.projections[0].fields = ["summary"]
    state.services.register_profile(
        document=profile,
        version="2.0",
        author="user",
        known_template_refs=[entry.ref for entry in state.services.list_templates()],
        domain_models=state.services.domain_declarations(),
    )
    before = state.services.get_profile("api-sectioned", "2.0")
    response = client.post("/api/v1/projects", json={"name": "已有新档案", "demo": True})
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "E_VERSION_CONFLICT"
    assert response.json()["remediation"]
    assert state.services.get_profile("api-sectioned", "2.0") == before
    assert len(state.services.list_projects()) == 1


def test_missing_shipped_data_returns_an_actionable_error(
    demo_app: tuple[TestClient, AppState], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server import demo

    client, state = demo_app
    examples = tmp_path / "examples"
    shutil.copytree(REPO_ROOT / "examples", examples)
    (examples / "data" / "operations.xlsx").unlink()
    monkeypatch.setattr(demo, "EXAMPLES_ROOT", examples)

    response = client.post("/api/v1/projects", json={"name": "缺资产", "demo": True})
    assert response.status_code == 422, response.text
    error = response.json()
    assert error["code"] == "E_CONFIG_INVALID"
    assert "operations.xlsx" in error["message"]
    assert error["remediation"]
    assert any("examples" in action for action in error["remediation"])
    assert state.services.list_projects() == []
    assert state.services.list_templates() == []


def test_legacy_demo_init_keeps_its_fixture_contract(
    demo_app: tuple[TestClient, AppState],
) -> None:
    client, state = demo_app
    created = client.post("/api/v1/projects", json={"name": "旧演示"})
    project_id = created.json()["project_id"]
    first = state.services.demo_init(project_id)
    second = state.services.demo_init(project_id)
    assert first == second
    assert {entry.doc_key for entry in first.documents} == {"overview", "detail"}
