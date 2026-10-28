"""渲染待确认随结果持久化，输入和草稿修订保持不变。"""

import pytest

from tests.api.test_documents_v2 import client_at, drain, setup_document, submit

pytestmark = pytest.mark.api


@pytest.mark.parametrize(
    "kind,warning", [("empty", "缺少正文"), ("manual", "人工编辑"), ("rewrite", "含改写")]
)
def test_render_persists_assembly_pending_without_publishing_revision(tmp_path, kind, warning):
    client, document, material = setup_document(tmp_path)
    path = f'/api/v2/documents/{document["document_id"]}'
    refs = [f'{material["material_id"]}/{b["block_id"]}' for b in material["blocks"]]
    text = "人工正文。" if kind == "manual" else "补充说明。新材料 42。"
    sections = (
        []
        if kind == "empty"
        else [
            {
                "section_id": "chapter",
                "edited": kind == "manual",
                "blocks": [{"kind": "para", "text": text, "source_refs": [refs[-1]]}],
            }
        ]
    )
    changed = client.patch(
        path,
        headers={"If-Match": "1"},
        json={
            "outline": [
                {
                    "section_id": "chapter",
                    "title": "新章",
                    "source_refs": [] if kind == "empty" else refs,
                }
            ],
            "outline_confirmed": True,
            "sections": sections,
        }
        if kind != "rewrite"
        else {
            "outline": [{"section_id": "chapter", "title": "新章", "source_refs": refs}],
            "outline_confirmed": True,
        },
    )
    assert changed.status_code == 200, changed.text
    document = changed.json()
    if kind == "rewrite":
        # 模拟已采用的模型改写；正常 PATCH 只接受人工标记。
        repo = client.app.state.docfidelity_state.documents
        candidate = repo.get(document["document_id"]).model_copy(update={"revision": 3})
        from docfidelity.contracts.documents import SectionDraft

        candidate.sections = [SectionDraft.model_validate(sections[0])]
        repo.commit(candidate)
        document = client.get(path).json()
    run = submit(client, document, "render")
    drain(client)
    restored = client_at(tmp_path / "store")
    finished = restored.get(f'/api/v2/runs/{run["run_id"]}').json()
    assert finished["state"] == "succeeded", finished
    pending = finished["result_document"]["pending"]
    assert any(warning in p for p in pending)
    assert len(pending) == len(set(pending))
    assert finished["document_snapshot"] == run["document_snapshot"]
    assert finished["result_document"]["revision"] == document["revision"]
    assert finished["result_revision"] is None
    assert restored.get(path).json() == document
    assert len(restored.get(f'/api/v2/runs/{run["run_id"]}/artifacts').json()) == 1


@pytest.mark.parametrize("counts", [(501,), (300, 201)])
def test_overflow_rejected_before_submission_with_specific_http_error(tmp_path, counts):
    client, document, _ = setup_document(tmp_path)
    materials = []
    for index, count in enumerate(counts):
        response = client.post(
            "/api/v2/materials",
            json={
                "name": f"m{index}.md",
                "text": "\n".join(f"# 章节{n}" for n in range(count)),
                "markdown": True,
            },
        )
        assert response.status_code == 201, response.text
        materials.append(response.json())
    created = client.post(
        "/api/v2/documents",
        json={k: document[k] for k in ["name", "template_id", "template_version", "mode"]}
        | {"material_ids": [m["material_id"] for m in materials]},
    )
    assert created.status_code == 422, created.text
    assert "E_SCHEMA_INVALID" in created.text
    assert all(client.get(f'/api/v2/materials/{m["material_id"]}').json() == m for m in materials)


def test_clean_render_does_not_claim_or_publish_a_revision(tmp_path):
    client, document, _ = setup_document(tmp_path)
    submit(client, document, "outline")
    drain(client)
    path = f'/api/v2/documents/{document["document_id"]}'
    document = client.get(path).json()
    document = client.patch(
        path, headers={"If-Match": str(document["revision"])}, json={"outline_confirmed": True}
    ).json()
    run = submit(client, document, "render")
    drain(client)
    result = client.get(f'/api/v2/runs/{run["run_id"]}').json()
    assert result["state"] == "succeeded"
    assert result["result_revision"] is None
    assert result["result_document"]["revision"] == document["revision"]
    assert client.get(path).json() == document
