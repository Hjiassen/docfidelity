"""``docfidelity`` 命令行入口（``[project.scripts]`` 的目标）。

它为什么在 ``builder`` 里而不是 ``platform``
--------------------------------------------
``pyproject.toml`` 原先写着 ``docfidelity = "docfidelity.platform.cli:main"``，而那个
模块**不存在**。这不是"忘了写文件"，是**目标位置错了**：``platform`` 是内核最底层
（只允许标准库 + ``pydantic``，见 ``.importlinter``），而"跑一遍构建"必须认识
``pipeline``。把 CLI 放进 ``platform`` 等于要求 ``platform`` 反向依赖整个内核。

放在 ``builder`` 则是顺理成章：它本来就是内核的**组合根**——认识各层、把五层接起来。
CLI 只是这个组合根的另一个消费者（服务端走入口点发现，命令行走这里），
两者共用同一份 :func:`~docfidelity.builder.stages.build_registry`。

只做一件事
----------
``build``：拿一份**冻结输入**（``payload`` JSON，即 ``server.jobs`` 冻结的那个形状），
在给定工作区根下跑完五个阶段，产出 ``.docx``。还有 ``stages`` 用来回答
"这台机器上装好的阶段体是哪些"——排查"为什么构建发起不了"时第一个要问的问题。

**它不做编排**：不排队、不并发、不做前置检查（那是 ``server`` 的事）。
一份输入跑一遍，退出码说明结果。
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from docfidelity.contracts.errors import EngineError
from docfidelity.contracts.job import STAGE_NAMES, Job
from docfidelity.pipeline import (
    EVENTS_FILENAME,
    STAGE_ORDER,
    EventLog,
    FileCheckpointStore,
    JsonlEventSink,
    Workspace,
    read_events,
    run_document,
)
from docfidelity.platform.hashing import digest_payload

from .stages import build_registry

__all__ = ["main"]

#: 退出码。``1`` 与 ``2`` 分开：前者是"这份输入跑失败了"（阶段体报的），
#: 后者是"命令行本身用错了"（参数缺、文件读不了）。脚本里 `if [ $? -eq 2 ]`
#: 因此能区分"我的命令写错了"与"我的文档有问题"。
EXIT_OK = 0
EXIT_BUILD_FAILED = 1
EXIT_USAGE = 2


def main(argv: Sequence[str] | None = None) -> int:
    """命令行入口。返回**退出码**而不是 ``sys.exit``——便于测试直接调用。"""
    parser = _parser()
    args = parser.parse_args(argv)
    if args.command == "stages":
        return _cmd_stages()
    if args.command == "build":
        return _cmd_build(args)
    parser.print_help()
    return EXIT_USAGE


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="docfidelity",
        description="模板与体例驱动的通用文档撰写引擎",
    )
    sub = parser.add_subparsers(dest="command")

    build = sub.add_parser("build", help="按一份冻结输入跑完五个阶段，产出一份 .docx")
    build.add_argument(
        "payload",
        type=Path,
        help="冻结输入 JSON（server.jobs 的 build_payload 形状）",
    )
    build.add_argument(
        "--root",
        type=Path,
        default=Path("var"),
        help="工作区根；作业目录落在 <root>/jobs/<job_id>（默认 var）",
    )
    build.add_argument("--job-id", default=None, help="作业标识；默认按输入摘要派生")
    build.add_argument("--json", action="store_true", help="把结果打成一行 JSON")

    sub.add_parser("stages", help="列出本包提供的阶段体")
    return parser


def _cmd_stages() -> int:
    registry = build_registry()
    for stage in STAGE_ORDER:
        resolved = registry.resolve(stage)
        print(f"{stage}\t{resolved.__module__}.{resolved.__qualname__}")
    missing = [name for name in STAGE_NAMES if name not in STAGE_ORDER]
    if missing:  # pragma: no cover - 契约新增阶段而 STAGE_ORDER 没跟上
        print(f"（契约里还有未编排的阶段：{missing}）", file=sys.stderr)
        return EXIT_USAGE
    return EXIT_OK


def _cmd_build(args: argparse.Namespace) -> int:
    try:
        payload = _load_payload(args.payload)
    except (OSError, ValueError) as exc:
        print(f"读不了输入快照 {args.payload}：{exc}", file=sys.stderr)
        return EXIT_USAGE
    except EngineError as exc:
        print(f"输入快照不成立：{exc.message}", file=sys.stderr)
        return EXIT_USAGE

    root = Path(args.root)
    job_id = args.job_id or _job_id_of(payload)
    inputs_hash = digest_payload(payload)
    job = Job(
        job_id=job_id,
        kind="document",
        project_id=str(payload["project_id"]),
        doc_key=str(payload.get("doc_key") or "") or None,
        doc_type=payload.get("doc_type"),
        status="queued",
        created_at=datetime.now(tz=timezone.utc),
        inputs_hash=inputs_hash,
    )
    workspace = Workspace(root, job_id)
    event_path = workspace.resolve(EVENTS_FILENAME)
    events = EventLog(JsonlEventSink(event_path))
    if event_path.exists():
        events.restore(job_id, read_events(event_path))
    result = run_document(
        job=job,
        registry=build_registry(),
        store=FileCheckpointStore(root),
        workspace=workspace,
        inputs_hash=inputs_hash,
        payload=payload,
        events=events,
    )

    summary = {
        "job_id": job_id,
        "root": str(root),
        "stages": [
            {
                "stage": run.stage,
                "status": run.status,
                "reused": run.reuse.reusable,
                "findings": len(run.findings),
                "error": None if run.error is None else run.error.code,
            }
            for run in result.runs
        ],
        "artifact": _artifact_of(root, job_id),
    }
    failed = [run for run in result.runs if run.status == "failed"]
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    else:
        _print_summary(summary)
    return EXIT_BUILD_FAILED if failed or len(result.runs) < len(STAGE_ORDER) else EXIT_OK


def _load_payload(path: Path) -> dict[str, Any]:
    raw: Any = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("输入快照必须是一个 JSON 对象")
    if not raw.get("project_id"):
        raise ValueError("输入快照缺少 project_id")
    return raw


def _job_id_of(payload: dict[str, Any]) -> str:
    """按输入摘要派生作业标识。

    派生而不是随机：同一个作业标识 = 同一份输入跑出来的工作区，故重复执行会**复用断点**
    （阶段实现版本与输入摘要都没变时），这正是"续跑"该有的样子。内核禁 ``random``
    （NFR-02），这里也守同一条。
    """
    return f"cli-{digest_payload(payload)[:12]}"


def _artifact_of(root: Path, job_id: str) -> str | None:
    """本次产出的 ``.docx``（如果写出来了）。"""
    directory = Workspace(root, job_id).artifacts_dir
    if not directory.is_dir():
        return None
    found = sorted(directory.glob("*.docx"))
    return None if not found else str(found[0])


def _print_summary(summary: dict[str, Any]) -> None:
    for stage in summary["stages"]:
        marker = "（复用）" if stage["reused"] else ""
        error = "" if stage["error"] is None else f"  {stage['error']}"
        print(f"{stage['stage']:<8} {stage['status']}{marker}{error}")
    artifact = summary["artifact"]
    print(f"产物：{artifact}" if artifact else "产物：（没有写出 .docx）")


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
