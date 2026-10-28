"""真实客户端仅在 HTTP 传输边界替身，语义解析与错误处理保持真实。"""

from __future__ import annotations

import json

import httpx
import pytest

from docfidelity.agents.writing import OpenAIContentGenerator
from docfidelity.contracts.generation import WritingRequest
from docfidelity.ingest.materials import ingest_text

OpenAI = pytest.importorskip("openai").OpenAI
pytestmark = pytest.mark.unit


def request():
    return WritingRequest(
        task="outline",
        name="新项目",
        instructions="保留材料事实",
        materials=[ingest_text("新材料事实。", name="新.txt", material_id="m1")],
        capabilities=["para"],
    )


def generator(handler):
    return OpenAIContentGenerator(
        model="chosen-model",
        base_url="https://local.invalid/v1",
        api_key="private-key",
        timeout=0.1,
        client_factory=lambda **kwargs: OpenAI(
            **kwargs, http_client=httpx.Client(transport=httpx.MockTransport(handler))
        ),
    )


def response(content, *, finish="stop"):
    return httpx.Response(
        200,
        json={
            "id": "offline",
            "object": "chat.completion",
            "created": 0,
            "model": "chosen-model",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": finish,
                }
            ],
            "usage": {"prompt_tokens": 23, "completion_tokens": 11, "total_tokens": 34},
        },
    )


def test_actual_client_adopts_bounded_json_and_tracks_actual_usage():
    calls = []

    def handler(req):
        calls.append(req)
        body = json.loads(req.content)
        assert body["model"] == "chosen-model"
        assert body["response_format"] == {"type": "json_object"}
        assert "新材料事实" in body["messages"][1]["content"]
        return response(
            json.dumps(
                {"outline": [{"section_id": "s1", "title": "新大纲", "source_refs": ["m1/b0001"]}]}
            )
        )

    result = generator(handler).generate(request())
    assert result.outline[0].title == "新大纲" and not result.error
    assert result.model == "chosen-model" and result.tokens_in == 23 and result.tokens_out == 11
    assert len(calls) == 1 and str(calls[0].url) == "https://local.invalid/v1/chat/completions"


@pytest.mark.parametrize(
    "payload",
    [
        "not json",
        '{"outline": [], "layout": {"styleId": "old"}}',
        '{"outline": [{"section_id": "s1", "title": "x", "source_refs": ["m1/b0001"], "font": "secret"}]}',
        '{"outline": [], "model": "forged", "tokens_in": 100}',
    ],
)
def test_invalid_json_and_extra_layout_or_audit_fields_are_rejected(payload):
    result = generator(lambda req: response(payload)).generate(request())
    assert result.error and not result.outline
    assert result.tokens_in == 23 and result.model == "chosen-model"
    assert "secret" not in result.model_dump_json()


def test_truncated_output_and_timeout_do_not_retry_or_leak_exception_body():
    for finish in ["length", "content_filter"]:
        result = generator(
            lambda req, finish=finish: response('{"outline": []}', finish=finish)
        ).generate(request())
        assert result.error and not result.outline
    calls = []

    def timeout(req):
        calls.append(req)
        raise httpx.ReadTimeout("private-key response-material", request=req)

    result = generator(timeout).generate(request())
    assert result.error and "private-key" not in result.model_dump_json()
    assert "response-material" not in result.model_dump_json() and len(calls) == 1


def test_discovery_uses_only_entrypoint_provider_and_keeps_config_explicit(monkeypatch):
    import docfidelity.pipeline.content_provider as provider

    class EntryPoint:
        def load(self):
            return OpenAIContentGenerator

    monkeypatch.setattr(provider, "entry_points", lambda **kwargs: [EntryPoint()])
    created = provider.create_content_generator(
        model="chosen", base_url="https://local.invalid/v1", api_key="secret", timeout=7
    )
    assert created.model == "chosen" and created.timeout == 7 and "secret" not in repr(created)
    monkeypatch.setattr(provider, "entry_points", lambda **kwargs: [])
    assert (
        provider.create_content_generator(model="chosen", base_url="https://local.invalid/v1")
        is None
    )


def test_missing_optional_sdk_returns_none_and_rules_still_work(monkeypatch):
    import docfidelity.agents.writing as adapter
    import docfidelity.pipeline.content_provider as provider
    from docfidelity.compose.outline import generate_outline
    from docfidelity.contracts.documents import DocumentDraft

    class EntryPoint:
        def load(self):
            return OpenAIContentGenerator

    monkeypatch.setattr(provider, "entry_points", lambda **kwargs: [EntryPoint()])
    monkeypatch.setattr(adapter, "find_spec", lambda name: None, raising=False)
    unavailable = provider.create_content_generator(
        model="chosen", base_url="https://local.invalid/v1"
    )
    assert unavailable is None
    doc = DocumentDraft(
        document_id="d1",
        revision=1,
        name="项目",
        template_id="t1",
        template_version=1,
        material_ids=["m1"],
        mode="organize",
    )
    result = generate_outline(doc, request().materials, unavailable)
    assert result.error and result.outline[0].source_refs == ["m1/b0001"]
