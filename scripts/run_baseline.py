"""跑一轮配置模型逐点基线评测，并把结论落到存储根（TASK-P0-17 / NFR-23）。

它解决的是什么
--------------
``routing.resolve_routes(baseline=…)`` 早就支持"按通过率接管"——未达基线通过率的
决策点强制由规则接管。但在此之前**没有任何东西产出那个 ``baseline``**：机制在、
数据没人给，于是"决策点分级"永远只是目录里的一句声明。本脚本就是那一环的入口：
固定样本集 → 真端点 → 逐决策点通过率 → ``<storage_root>/baseline.json``。

服务端读同一份报告（``server.api.deps.session_factory_from`` 把它交给
``model_session_factory`` 的 ``baseline_path``，**每个作业现读**），所以跑完这个脚本，
**下一次构建**就按新数据路由，不必重启服务。

两条"拒绝"是刻意的
------------------
1. **模型不可用时不写报告**。此时每条样本都走规则兜底，通过率恒为 0——把这份
   "成绩单"写下去，等于声称"模型全错"，而实际上模型一次都没被问过。
   它的下游是每个模型档决策点都被强制接管：功能静默关闭，且界面上显示的是一份
   看起来合法的评测结果。宁可不产出，也不要产出一份假记录。
2. **零通过时不写报告**（除非显式 ``--allow-zero``）。全样本失败绝大多数是配置问题
   （模型名写错、网关 4xx、端点不是 chat-completions 形态），而不是"能力真的为零"。
   真为零时加 ``--allow-zero`` 是显式表态，留痕也留在命令里。

用法::

    # 沿用服务端的 DOCFIDELITY_MODEL_* 环境变量与当前目录 .env 配置
    PYTHONPATH="src;." python scripts/run_baseline.py

    # 指定端点与模型，报告写到自定义位置
    PYTHONPATH="src;." python scripts/run_baseline.py \\
        --model qwen2.5-7b-instruct --base-url http://127.0.0.1:8000/v1 \\
        --out var/baseline.json

    # 换一份样本集（形状见 src/docfidelity/agents/baseline_cases.json）
    PYTHONPATH="src;." python scripts/run_baseline.py --cases my_cases.json

    # 只看结果不写盘（试跑）
    PYTHONPATH="src;." python scripts/run_baseline.py --no-save

    # 单独观察无置信度网关的任务正确率；仅用于实验，不写入生产基线
    PYTHONPATH="src;." python scripts/run_baseline.py --confidence-threshold 0 --no-save

    # 按项目的单点路由配置评测（文件为 RoutingConfig 或含 routing 的项目配置 JSON）
    PYTHONPATH="src;." python scripts/run_baseline.py --routing project-routing.json

为什么门槛要能显式给
----------------------------------
通过率是"**在这个门槛下**能不能驱动"，门槛是结论的一部分。缺省门槛 0.7 之下，
一个不报置信度的 OpenAI 兼容网关会让每个点都判"不达标"：测出来的是"这个网关不给
置信度"，而不是"模型行不行"。拿那份结果去校准分级，会把五个模型档点**全部**误判为
不可驱动。故报告里记 ``routing_digest``（这次用的是什么姿态），命令行上则必须能
把姿态说清楚。阈值 0 实验用于区分任务正确率与置信度缺失，不能替代生产准入；
需求 5.2.4 要求替代准入有独立校验或人工确认依据，并重新逐点实测。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="run_baseline",
        description="跑一轮配置模型逐点基线评测并把结论落到存储根（TASK-P0-17 / NFR-23）",
    )
    parser.add_argument("--model", default=None, help="模型标识；缺省取配置里的 model_name")
    parser.add_argument("--base-url", default=None, help="模型端点；缺省取配置里的 model_base_url")
    parser.add_argument(
        "--cases",
        default=None,
        help="样本集 JSON 路径；缺省用随包分发的内置样本集",
    )
    parser.add_argument(
        "--out",
        default=None,
        help="报告落点；缺省 <storage_root>/baseline.json（即服务端会读的那一份）",
    )
    parser.add_argument(
        "--max-retries",
        type=int,
        default=None,
        help="每条样本的重试上限；缺省用内核默认值",
    )
    posture = parser.add_mutually_exclusive_group()
    posture.add_argument(
        "--routing",
        default=None,
        metavar="JSON",
        help="路由配置 JSON 文件：RoutingConfig，或含 routing 字段的项目配置；原样用于评测姿态",
    )
    posture.add_argument(
        "--confidence-threshold",
        type=float,
        default=None,
        help=(
            "给全部模型档决策点统一设的置信度门槛（0~1）；缺省不覆盖，用目录/部署默认。"
            " 缺失置信度仍待确认；章节放置可用--routing配置独立校验，二者互斥"
        ),
    )
    parser.add_argument("--no-save", action="store_true", help="只打印结论，不写报告")
    parser.add_argument(
        "--allow-zero",
        action="store_true",
        help="允许写入「零通过」的报告（默认拒绝：那多半是配置问题）",
    )
    return parser.parse_args(argv)


def _load_cases(spec: str | None) -> tuple[Any, ...]:
    from docfidelity.agents import cases_from_payload, default_cases

    if spec is None:
        return default_cases()
    path = Path(spec)
    if not path.is_file():
        raise SystemExit(f"样本集不存在：{path}")
    return cases_from_payload(json.loads(path.read_text(encoding="utf-8")))


def _routing_config(threshold: float | None, spec: str | None = None) -> Any:
    """读取项目路由姿态，或生成统一门槛；``None`` 表示不覆盖任何点。

    只覆盖**模型档**的点：规则 / 人工档根本不调模型，给它们写门槛是无声的噪声
    （`resolve_routes` 会忽略，但配置文本看起来像"给每个点都设了门槛"）。
    """
    from docfidelity.agents import build_routing
    from docfidelity.agents.catalog import DECISION_POINTS, MODEL_ROUTED
    from docfidelity.contracts.config import DecisionPointRoute, RoutingConfig
    from docfidelity.contracts.errors import EngineError

    if spec is not None:
        if threshold is not None:
            raise SystemExit("--routing 路由配置与 --confidence-threshold 不能同时使用")
        path = Path(spec)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise SystemExit("路由配置 JSON 顶层必须是对象")
            payload = payload.get("routing", payload)
            if not isinstance(payload, dict):
                raise SystemExit("路由配置的 routing 字段必须是对象")
            config = RoutingConfig.load(payload)
            build_routing(config, model_enabled=True)
        except (OSError, ValueError, EngineError) as exc:
            raise SystemExit(
                f"无法加载路由配置 {path}（{type(exc).__name__}）；请检查文件与 RoutingConfig schema"
            ) from exc
        return config
    if threshold is None:
        return None
    if not 0.0 <= threshold <= 1.0:
        raise SystemExit(f"--confidence-threshold 必须在 0~1 之间，得到 {threshold}")

    return RoutingConfig(
        decision_points={
            point.id: DecisionPointRoute(confidence_threshold=threshold)
            for point in DECISION_POINTS
            if point.kind in MODEL_ROUTED
        }
    )


def _build_adapter(model: str | None, base_url: str | None, api_key: str | None) -> Any:
    from docfidelity.agents import NullAdapter, OpenAIAgentsAdapter

    if model and base_url:
        return OpenAIAgentsAdapter(model=model, base_url=base_url, api_key=api_key)
    return NullAdapter()


def main(argv: list[str] | None = None) -> int:
    if str(ROOT / "src") not in sys.path:
        sys.path.insert(0, str(ROOT / "src"))
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))

    # 延迟导入：必须在上面把 ``src`` 与仓库根放进 ``sys.path`` 之后才成立。
    from docfidelity.agents import BASELINE_FILENAME, endpoint_digest, run_baseline, save_report
    from server.settings import ServerSettings

    args = _parse_args(argv)
    routing = _routing_config(args.confidence_threshold, args.routing)
    settings = ServerSettings()
    model = args.model or settings.model_name or ""
    base_url = args.base_url or settings.model_base_url or ""
    endpoint_fingerprint = endpoint_digest(base_url)

    cases = _load_cases(args.cases)
    # 密钥与 `session_factory_from` 同一处来源、同一步取出：脚本是**部署侧**入口，
    # 它沿用服务端的环境变量与 `.env` 配置，也不经过任何 HTTP 响应。
    # 忘了传的后果很隐蔽：适配层会给 SDK 一个"不需要认证"的占位串，
    # 于是**只在需要认证的端点上**表现为一次 `AuthenticationError`——而那看起来
    # 像是"模型不可用"，把排查方向带到网络上去。
    secret = settings.model_api_key
    adapter = _build_adapter(
        model or None, base_url or None, None if secret is None else secret.get_secret_value()
    )

    if not adapter.available():
        source = "自定义样本集" if args.cases else f"内置样本集（{len(cases)} 条）"
        print(f"模型不可用：model={model!r} endpoint_digest={endpoint_fingerprint}；{source}")
        print("评测没有意义——每条样本都会走规则兜底，写下去的报告会声称「模型全错」。")
        print("请先配置 DOCFIDELITY_MODEL_ENABLED / DOCFIDELITY_MODEL_BASE_URL /")
        print("DOCFIDELITY_MODEL_NAME（或加 --model / --base-url），再重跑本脚本。")
        print("**未写入任何报告。**")
        return 1

    posture = "目录/部署默认"
    if args.routing is not None:
        posture = f"路由配置文件 {args.routing}"
    elif args.confidence_threshold is not None:
        posture = f"统一置信度门槛 {args.confidence_threshold}"
    print(f"评测开始：model={model} endpoint_digest={endpoint_fingerprint} 样本 {len(cases)} 条")
    print(f"路由姿态：{posture}（通过率只在同一姿态下可比）")
    report = run_baseline(
        cases,
        adapter=adapter,
        model=model,
        endpoint_digest=endpoint_fingerprint,
        config=routing,
        **(  # 只在显式给出时覆盖，避免把内核默认值抄第二份
            {} if args.max_retries is None else {"max_retries": args.max_retries}
        ),
    )

    print(f"结果：{report.passed}/{report.total} 通过（{report.pass_rate:.2f}）")
    print(
        f"已采纳回答准确率：{report.accuracy:.2f}；回退：{report.fallback_count} 条；"
        f"缺失置信度：{report.missing_confidence_count} 次调用"
    )
    print(f"累计 token：输入 {report.tokens_in}，输出 {report.tokens_out}（含重试）")
    for point, rate in sorted(report.by_point.items()):
        print(f"  {point:<32} {rate:.2f}")
    if report.uncovered:
        print("未覆盖的模型档决策点（生产构建回退规则并标记待确认）：")
        for point in report.uncovered:
            print(f"  {point}")

    if report.failed:
        print(f"未通过的样本 {len(report.failed)} 条，前几条原因：")
        for item in report.failed[:5]:
            print(f"  {item.case_id}: {item.reason}")

    if args.no_save:
        print("--no-save：未写入报告。")
        return 0

    if report.passed == 0 and not args.allow_zero:
        print("零通过：拒绝写入。这多半不是「模型能力为零」，而是配置或端点形态的问题")
        print("（模型名、网关路由、是否 chat-completions 形态）。确认确实是零通过时，")
        print("加 --allow-zero 重跑。**未写入任何报告。**")
        return 1

    target = Path(args.out) if args.out else settings.storage_root / BASELINE_FILENAME
    saved = save_report(report, target)
    print(f"报告已写入：{saved}")
    print(f"样本集指纹：{report.cases_digest}（样本变了，这份结论就不可比）")
    print(f"姿态指纹：{report.routing_digest}（门槛 / 归属变了，这份结论同样不可比）")
    print("服务端在每个作业开跑时现读它，无需重启。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
