"""跨实例事务、取消迟到和恢复行为。"""

import json
import multiprocessing
import os
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from docfidelity.contracts.documents import DraftBlock, OutlineSection, SectionDraft
from docfidelity.contracts.generation import WritingResult
from server.jobs import WorkerLoop
from tests.api.test_documents_v2 import client_at, drain, setup_document, submit

pytestmark = pytest.mark.api


class Generator:
    def __init__(self):
        self.calls = []

    def generate(self, request):
        self.calls.append(request)
        if request.task == "outline":
            return WritingResult(
                outline=[
                    OutlineSection(
                        section_id="chapter",
                        title="模型章",
                        source_refs=[
                            f"{m.material_id}/{b.block_id}"
                            for m in request.materials
                            for b in m.blocks
                        ],
                    )
                ],
                tokens_in=11,
                tokens_out=7,
            )
        outline = next(s for s in request.outline if s.section_id == request.section_id)
        blocks = [
            DraftBlock(kind="para", text=b.text, source_refs=[f"{m.material_id}/{b.block_id}"])
            for m in request.materials
            for b in m.blocks
            if b.kind == "para" and f"{m.material_id}/{b.block_id}" in outline.source_refs
        ]
        return WritingResult(
            section=SectionDraft(section_id=request.section_id, blocks=blocks),
            tokens_in=12,
            tokens_out=8,
        )


def enabled_document(tmp_path, generator):
    client, document, material = setup_document(
        tmp_path, mode="organize", content_generator=generator
    )
    state = client.app.state.docfidelity_state
    state.settings.model_enabled = True
    return client, document, material


def confirmed(client, document):
    submit(client, document, "outline")
    drain(client)
    path = f'/api/v2/documents/{document["document_id"]}'
    document = client.get(path).json()
    response = client.patch(
        path, json={"outline_confirmed": True}, headers={"If-Match": str(document["revision"])}
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_injected_model_without_model_field_is_adopted_and_input_frozen(tmp_path):
    generator = Generator()
    client, document, material = enabled_document(tmp_path, generator)
    run = submit(client, document, "outline")
    client.patch(
        f'/api/v2/documents/{document["document_id"]}',
        json={"name": "用户新修订"},
        headers={"If-Match": "1"},
    )
    drain(client)
    finished = client.get(f'/api/v2/runs/{run["run_id"]}').json()
    assert finished["state"] == "succeeded"
    assert finished["attempts"][0]["adopted"] is True
    assert finished["attempts"][0]["model"] is None
    assert finished["document_snapshot"] == run["document_snapshot"]
    assert finished["result_document"]["outline"][0]["title"] == "模型章"
    assert finished["result_revision"] is None
    assert client.get(f'/api/v2/documents/{document["document_id"]}').json()["name"] == "用户新修订"
    assert generator.calls[0].name == document["name"]
    assert generator.calls[0].materials[0].digest == material["digest"]


def test_cancel_during_model_call_never_adopts_late_result(tmp_path):
    entered, released = threading.Event(), threading.Event()

    class Blocking(Generator):
        def generate(self, request):
            entered.set()
            assert released.wait(15)
            return super().generate(request)

    generator = Blocking()
    client, document, _ = enabled_document(tmp_path, generator)
    run = submit(client, document, "outline")
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(drain, client)
        assert entered.wait(10)
        assert client.post(f'/api/v2/runs/{run["run_id"]}/cancel').json()["state"] == "canceled"
        released.set()
        future.result(timeout=10)
    result = client.get(f'/api/v2/runs/{run["run_id"]}').json()
    assert result["result_document"] is None
    assert result["attempts"] == []
    assert client.get(f'/api/v2/documents/{document["document_id"]}').json()["revision"] == 1


def _process_patch(root, document_id, output):
    client = client_at(Path(root))
    output.put(
        client.patch(
            f"/api/v2/documents/{document_id}", json={"name": "并发编辑"}, headers={"If-Match": "1"}
        ).status_code
    )


def test_cross_process_patch_has_one_winner(tmp_path):
    client, document, _ = setup_document(tmp_path)
    context = multiprocessing.get_context("spawn")
    output = context.Queue()
    processes = [
        context.Process(
            target=_process_patch, args=(str(tmp_path / "store"), document["document_id"], output)
        )
        for _ in range(2)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(25)
        assert process.exitcode == 0
    assert sorted(output.get(timeout=3) for _ in processes) == [200, 409]


def test_cross_instance_idempotency_and_busy_run_does_not_starve_queue(tmp_path):
    client, document, _ = setup_document(tmp_path)

    def create(_):
        return submit(
            client_at(tmp_path / "store"),
            document,
            "outline",
            headers={"Idempotency-Key": "shared"},
        )["run_id"]

    with ThreadPoolExecutor(max_workers=2) as pool:
        ids = list(pool.map(create, range(2)))
    assert ids[0] == ids[1]
    second = submit(client, document, "outline")
    runner = client.app.state.docfidelity_state.document_runs
    with runner.execution.acquire(ids[0]) as acquired:
        assert acquired
        result = client_at(tmp_path / "store").app.state.docfidelity_state.document_runs.run_once()
        assert result.run_id == second["run_id"]
    assert runner.get(ids[0]).state == "queued"


def test_checkpoint_and_attempt_are_atomic_and_recovery_skips_success(tmp_path, monkeypatch):
    generator = Generator()
    client, document, material = enabled_document(tmp_path, generator)
    document = confirmed(client, document)
    # 两个章节分别引用标题和正文，正文分开可观察第二章崩溃后的恢复。
    path = f'/api/v2/documents/{document["document_id"]}'
    refs = document["outline"][0]["source_refs"]
    document = client.patch(
        path,
        json={
            "outline": [
                {"section_id": "first", "title": "甲", "source_refs": [refs[1]]},
                {"section_id": "second", "title": "乙", "source_refs": [refs[1]]},
            ],
            "outline_confirmed": True,
        },
        headers={"If-Match": str(document["revision"])},
    ).json()
    run = submit(client, document, "content")
    runner = client.app.state.docfidelity_state.document_runs
    original = runner._save

    class Crash(BaseException):
        pass

    def crash_after_checkpoint(record, kind, payload):
        original(record, kind, payload)
        if payload.get("section_id") == "first":
            raise Crash()

    monkeypatch.setattr(runner, "_save", crash_after_checkpoint)
    with pytest.raises(Crash):
        runner.run_once()
    partial = runner.get(run["run_id"])
    assert len(partial.section_checkpoints) == len(partial.attempts) == 1
    assert partial.document_snapshot.model_dump(mode="json") == run["document_snapshot"]
    restarted = client_at(tmp_path / "store", content_generator=generator)
    restarted.app.state.docfidelity_state.settings.model_enabled = True
    drain(restarted)
    finished = restarted.get(f'/api/v2/runs/{run["run_id"]}').json()
    assert finished["state"] == "succeeded"
    assert len(finished["section_checkpoints"]) == len(finished["attempts"]) == 2
    assert [r.section_id for r in generator.calls if r.task == "section"] == ["first", "second"]
    assert finished["attempts"][0]["adopted"]
    events = restarted.get(f'/api/v2/runs/{run["run_id"]}/events?cursor=2').json()
    assert all(e["event_id"] > 2 for e in events)


def test_changed_model_identity_falls_back_without_network_or_secret(tmp_path):
    generator = Generator()
    client, document, _ = enabled_document(tmp_path, generator)
    run = submit(client, document, "outline")
    state = client.app.state.docfidelity_state
    state.settings.model_base_url = "http://changed.example/private"
    drain(client)
    finished = state.document_runs.get(run["run_id"])
    assert finished.state == "succeeded"
    assert generator.calls == []
    assert any("配置已变化" in p for p in finished.result_document.pending)
    assert "changed.example" not in finished.model_dump_json()


def test_independent_worker_once_consumes_v2_queue(tmp_path):
    client, document, _ = setup_document(tmp_path)
    run = submit(client, document, "outline")
    env = dict(
        os.environ,
        PYTHONPATH="src;.",
        PYTHONUTF8="1",
        DOCFIDELITY_STORAGE_ROOT=str(tmp_path / "store"),
        DOCFIDELITY_MODEL_ENABLED="false",
        DOCFIDELITY_SEED_DEMO_TEMPLATES="false",
    )
    process = subprocess.run(
        [sys.executable, "-X", "utf8", "-m", "server.worker", "--once"],
        env=env,
        capture_output=True,
        text=True,
        timeout=35,
    )
    assert process.returncode == 0, process.stderr
    assert client.get(f'/api/v2/runs/{run["run_id"]}').json()["state"] == "succeeded"


def test_edited_section_is_retained_without_actual_model_call(tmp_path):
    generator = Generator()
    client, document, _ = enabled_document(tmp_path, generator)
    document = confirmed(client, document)
    path = f'/api/v2/documents/{document["document_id"]}'
    manual = {
        "section_id": document["outline"][0]["section_id"],
        "edited": True,
        "blocks": [{"kind": "para", "text": "人工保留"}],
    }
    document = client.patch(
        path, json={"sections": [manual]}, headers={"If-Match": str(document["revision"])}
    ).json()
    run = submit(client, document, "content")
    drain(client)
    finished = client.get(f'/api/v2/runs/{run["run_id"]}').json()
    assert finished["state"] == "succeeded"
    assert len(generator.calls) == 1
    assert finished["attempts"] == []
    assert finished["result_document"]["sections"][0]["blocks"][0]["text"] == "人工保留"


def test_model_error_keeps_previous_success_without_new_adoption(tmp_path):
    generator = Generator()
    client, document, _ = enabled_document(tmp_path, generator)
    document = confirmed(client, document)
    submit(client, document, "content")
    drain(client)
    path = f'/api/v2/documents/{document["document_id"]}'
    document = client.get(path).json()

    class Failed:
        def generate(self, request):
            return WritingResult(error="private key failure", tokens_in=3)

    client.app.state.docfidelity_state.document_runs.generator = Failed()
    run = submit(client, document, "content")
    drain(client)
    finished = client.get(f'/api/v2/runs/{run["run_id"]}').json()
    assert finished["state"] == "succeeded"
    assert finished["result_document"]["sections"] == document["sections"]
    assert finished["attempts"][0]["fallback_used"]
    assert not finished["attempts"][0]["adopted"]
    assert "private key failure" not in json.dumps(finished)


def test_late_render_cancel_hides_artifacts(tmp_path, monkeypatch):
    client, document, _ = setup_document(tmp_path)
    document = confirmed(client, document)
    state = client.app.state.docfidelity_state
    run = submit(client, document, "render")
    original = state.template_imports.render

    def cancel_after_render(*args, **kwargs):
        path = original(*args, **kwargs)
        state.document_runs.cancel(run["run_id"])
        return path

    monkeypatch.setattr(state.template_imports, "render", cancel_after_render)
    drain(client)
    assert state.document_runs.get(run["run_id"]).state == "canceled"
    assert state.document_runs.artifacts(run["run_id"]) == []


def test_worker_loop_advances_old_and_new_queues_in_one_tick(tmp_path):
    client, document, _ = setup_document(tmp_path)
    state = client.app.state.docfidelity_state
    run = submit(client, document, "outline")

    class OldQueue:
        calls = 0

        def run_once(self):
            self.calls += 1
            return object()

    old = OldQueue()
    assert WorkerLoop(old, additional_runner=state.document_runs.run_once).tick()
    assert old.calls == 1
    assert state.document_runs.get(run["run_id"]).state == "succeeded"


def test_selected_content_section_only_calls_and_preserves_other_chapter(tmp_path):
    generator = Generator()
    client, document, _ = enabled_document(tmp_path, generator)
    document = confirmed(client, document)
    path = f'/api/v2/documents/{document["document_id"]}'
    refs = document["outline"][0]["source_refs"]
    document = client.patch(
        path,
        json={
            "outline": [
                {"section_id": "first", "title": "甲", "source_refs": [refs[1]]},
                {"section_id": "second", "title": "乙", "source_refs": [refs[1]]},
            ],
            "outline_confirmed": True,
            "sections": [
                {
                    "section_id": "first",
                    "edited": True,
                    "blocks": [{"kind": "para", "text": "用户首章"}],
                }
            ],
        },
        headers={"If-Match": str(document["revision"])},
    ).json()
    response = client.post(
        path + "/content-runs",
        json={"section_id": "second"},
        headers={"Idempotency-Key": "chapter"},
    )
    assert response.status_code == 202, response.text
    assert (
        client.post(
            path + "/content-runs",
            json={"section_id": "first"},
            headers={"Idempotency-Key": "chapter"},
        ).status_code
        == 409
    )
    assert client.post(path + "/outline-runs", json={"section_id": "first"}).status_code == 422
    drain(client)
    result = client.get(f'/api/v2/runs/{response.json()["run_id"]}').json()
    assert result["state"] == "succeeded"
    assert [r.section_id for r in generator.calls if r.task == "section"] == ["second"]
    assert result["result_document"]["sections"][0]["blocks"][0]["text"] == "用户首章"
    assert len(result["attempts"]) == 1


def test_restart_finishes_prepared_revision_after_pointer_crash(tmp_path, monkeypatch):
    from server import document_store

    client, document, _ = setup_document(tmp_path)
    run = submit(client, document, "outline")
    runner = client.app.state.docfidelity_state.document_runs
    original = document_store.atomic_write_text

    class Crash(BaseException):
        pass

    def crash_document_pointer(path, text, **kwargs):
        if path.name == "current.json" and path.parent.name == document["document_id"]:
            raise Crash()
        return original(path, text, **kwargs)

    monkeypatch.setattr(document_store, "atomic_write_text", crash_document_pointer)
    with pytest.raises(Crash):
        runner.run_once()
    assert runner.get(run["run_id"]).result_document is not None
    assert client.get(f'/api/v2/documents/{document["document_id"]}').json()["revision"] == 1
    monkeypatch.setattr(document_store, "atomic_write_text", original)
    restarted = client_at(tmp_path / "store")
    drain(restarted)
    result = restarted.get(f'/api/v2/runs/{run["run_id"]}').json()
    assert result["state"] == "succeeded"
    assert (
        restarted.get(f'/api/v2/documents/{document["document_id"]}').json()["revision"]
        == result["result_revision"]
    )


@pytest.mark.parametrize("partial", [False, True])
def test_prompt_version_change_falls_back_and_preserves_checkpoints(tmp_path, monkeypatch, partial):
    from server import document_runs

    generator = Generator()
    client, document, _ = enabled_document(tmp_path, generator)
    document = confirmed(client, document)
    path = f'/api/v2/documents/{document["document_id"]}'
    refs = document["outline"][0]["source_refs"]
    document = client.patch(
        path,
        json={
            "outline": [
                {"section_id": "first", "title": "甲", "source_refs": [refs[1]]},
                {"section_id": "second", "title": "乙", "source_refs": [refs[1]]},
            ],
            "outline_confirmed": True,
        },
        headers={"If-Match": str(document["revision"])},
    ).json()
    run = submit(client, document, "content")
    runner = client.app.state.docfidelity_state.document_runs
    if partial:
        original = runner._save

        class Crash(BaseException):
            pass

        def crash_checkpoint(record, kind, payload):
            original(record, kind, payload)
            if payload.get("section_id") == "first":
                raise Crash()

        monkeypatch.setattr(runner, "_save", crash_checkpoint)
        with pytest.raises(Crash):
            runner.run_once()
    before = len(generator.calls)
    frozen = runner.get(run["run_id"])
    monkeypatch.setattr(document_runs, "PROMPT_VERSION", "writing-v2-next")
    restarted = client_at(tmp_path / "store", content_generator=generator)
    restarted.app.state.docfidelity_state.settings.model_enabled = True
    drain(restarted)
    result = restarted.get(f'/api/v2/runs/{run["run_id"]}').json()
    assert result["state"] == "succeeded"
    assert len(generator.calls) == before
    assert result["prompt_version"] == run["prompt_version"]
    assert any("提示版本已变化" in p for p in result["result_document"]["pending"])
    assert len(result["attempts"]) == int(partial)
    if partial:
        assert result["section_checkpoints"][0] == frozen.section_checkpoints[0].model_dump(
            mode="json"
        )
        assert result["attempts"][0]["adopted"]


@pytest.mark.parametrize("edited_first", [False, True])
def test_generated_outline_reused_ids_with_new_sources_invalidates_old_body(tmp_path, edited_first):
    class SwitchingGenerator(Generator):
        swapped = False

        def generate(self, request):
            if request.task != "outline":
                return super().generate(request)
            self.calls.append(request)
            groups = [request.materials[0].blocks[index : index + 2] for index in (0, 2, 4)]
            if self.swapped:
                groups[0], groups[1] = groups[1], groups[0]
            return WritingResult(
                outline=[
                    OutlineSection(
                        section_id=section_id,
                        title=blocks[0].text,
                        source_refs=[
                            f"{request.materials[0].material_id}/{b.block_id}" for b in blocks
                        ],
                    )
                    for section_id, blocks in zip(("first", "second", "third"), groups, strict=True)
                ]
            )

    generator = SwitchingGenerator()
    client, previous, _ = enabled_document(tmp_path, generator)
    material = client.post(
        "/api/v2/materials",
        json={
            "name": "three.md",
            "text": "# 甲章\n甲事实。\n# 乙章\n乙事实。\n# 丙章\n丙事实。",
            "markdown": True,
        },
    ).json()
    document = client.post(
        "/api/v2/documents",
        json={
            "name": "新文档",
            "template_id": previous["template_id"],
            "template_version": 1,
            "material_ids": [material["material_id"]],
            "mode": "organize",
        },
    ).json()
    document = confirmed(client, document)
    submit(client, document, "content")
    drain(client)
    path = f'/api/v2/documents/{document["document_id"]}'
    document = client.get(path).json()
    sections = document["sections"]
    sections[2] = {
        "section_id": "third",
        "edited": True,
        "blocks": [{"kind": "para", "text": "人工第三章"}],
    }
    if edited_first:
        sections[0] = {
            "section_id": "first",
            "edited": True,
            "blocks": [{"kind": "para", "text": "人工首章"}],
        }
    document = client.patch(
        path, json={"sections": sections}, headers={"If-Match": str(document["revision"])}
    ).json()
    old_revision = document["revision"]
    generator.swapped = True
    submit(client, document, "outline")
    drain(client)
    changed = client.get(path).json()
    assert [s["section_id"] for s in changed["sections"]] == ["third"]
    assert changed["sections"][0]["blocks"][0]["text"] == "人工第三章"
    assert any("大纲来源变化" in p for p in changed["pending"])
    assert (
        client.get(path + f"/revisions/{old_revision}").json()["sections"] == document["sections"]
    )
    changed = client.patch(
        path, json={"outline_confirmed": True}, headers={"If-Match": str(changed["revision"])}
    ).json()

    class Failed:
        def generate(self, request):
            return WritingResult(error="unavailable")

    client.app.state.docfidelity_state.document_runs.generator = Failed()
    submit(client, changed, "content")
    drain(client)
    changed = client.get(path).json()
    assert [s["blocks"][0]["text"] for s in changed["sections"]] == [
        "乙事实。",
        "甲事实。",
        "人工第三章",
    ]
    render = submit(client, changed, "render")
    drain(client)
    assert client.get(f'/api/v2/runs/{render["run_id"]}').json()["state"] == "succeeded"
