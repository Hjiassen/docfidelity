"""在临时部署中创建演示项目，验证独立 API / worker 的完整构建。"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import time
from typing import Any
from urllib.request import Request, urlopen
from zipfile import ZipFile


def fetch(url: str, body: dict[str, Any] | None = None) -> bytes:
    """读取接口；提供正文时发送 JSON POST。"""
    request = Request(
        url,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json"},
    )
    with urlopen(request, timeout=20) as response:
        return response.read()


def check_events(url: str) -> None:
    """补推必须包含连续序号及无序号的明确结束帧。"""
    frames = fetch(url).decode().split("\n\n")
    ids = [
        int(line.removeprefix("id: "))
        for frame in frames
        for line in frame.splitlines()
        if line.startswith("id: ")
    ]
    assert ids and ids == list(range(1, len(ids) + 1)), ids
    assert frames[-2] == "event: stream_end\ndata: {}", frames[-2:]


def smoke(url: str, *, timeout: float) -> None:
    """验证真实演示产物；运行会在目标部署留下一个演示项目。"""
    base = url.rstrip("/")
    health = json.loads(fetch(f"{base}/health"))
    assert health["build_ready"] and health["model_enabled"] is False, health
    project = json.loads(
        fetch(f"{base}/api/v1/projects", {"name": f"部署自检-{time.time_ns()}", "demo": True})
    )
    parent = json.loads(fetch(f"{base}/api/v1/projects/{project['project_id']}/jobs", {}))
    deadline = time.monotonic() + timeout
    while True:
        detail = json.loads(fetch(f"{base}/api/v1/jobs/{parent['job_id']}"))
        status = detail["job"]["status"]
        if status in {"succeeded", "failed", "partial_failure", "canceled", "awaiting_decision"}:
            break
        assert time.monotonic() < deadline, f"构建超时：{parent['job_id']} / {status}"
        time.sleep(0.5)
    assert status == "succeeded", detail
    assert len(detail["children"]) == 3, detail
    check_events(f"{base}/api/v1/jobs/{parent['job_id']}/events")
    documents = 0
    for child in detail["children"]:
        job_id = child["job_id"]
        assert child["status"] == "succeeded", child
        report = json.loads(fetch(f"{base}/api/v1/jobs/{job_id}/report"))
        assert report["verdict"] in {"pass", "warning"}, report
        check_events(f"{base}/api/v1/jobs/{job_id}/events")
        artifacts = json.loads(fetch(f"{base}/api/v1/jobs/{job_id}/artifacts"))
        docx = [item for item in artifacts if item["kind"] == "docx"]
        assert len(docx) == 1, artifacts
        payload = fetch(f"{base}/api/v1/artifacts/{docx[0]['artifact_id']}/download")
        assert len(payload) == docx[0]["size_bytes"], docx[0]
        assert hashlib.sha256(payload).hexdigest() == docx[0]["sha256"], docx[0]
        with ZipFile(io.BytesIO(payload)) as archive:
            assert archive.testzip() is None
            assert b"<w:t" in archive.read("word/document.xml")
        documents += 1
    print(json.dumps({"project_id": project["project_id"], "docx": documents, "status": status}))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--timeout", type=float, default=120.0)
    args = parser.parse_args()
    smoke(args.url, timeout=args.timeout)


if __name__ == "__main__":
    main()
