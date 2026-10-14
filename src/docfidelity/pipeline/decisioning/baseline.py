"""小模型基线评测（TASK-P0-17 / NFR-23）——"哪个决策点可以交给模型"的**证据来源**。

为什么需要这个模块
------------------
`routing.resolve_routes(baseline=…)` 早就支持"按通过率接管"（未达基线通过率的决策点
强制由规则接管）。但在此之前**没有任何东西产出那个 `baseline`**：机制在、数据没人给，
于是 NFR-23 的能力门槛永远走不到——每个点都按目录声明路由，"分级"停留在一句设计声称。

本模块补的就是那一环：一份**可声明**的样本集 + 一次**可复现**的评测 + 一份**可落盘**的结论。

四条纪律
--------
1. **评测器是纯的**（接受注入的适配层与样本集，返回报告），因此它既能在真端点上跑
   （`scripts/run_baseline.py`），也能在 CI 上用 `ScriptedAdapter` 离线跑出确定结论。
   否则"评测"只能人工做，而人工做的结论进不了门禁——那正是"基线数据没人给"的成因。
2. **判分口径只有一处**（:func:`verdict`）。通过 = 模型结论**被采纳**（未降级）
   且载荷命中期望。判反了的后果不是"评测错了"，而是 `baseline_pass_rate` 喂给路由表时
   把不可靠的点标成可靠——那才是真的放行。
3. **报告带样本集指纹**。换一批样本跑出的通过率与旧报告不可比；不记指纹的话，
   "某点通过率 0.88 达标"这句话在样本换过之后仍然成立，而它已经不成立了。
4. **报告带路由策略和端点指纹**。缺失置信度不能用门槛归零绕过；章节放置可显式采用
   独立安全约束校验，其语义正确性仍由样本期望判分。生产必须核对同一模型、端点、
   样本与准入策略，才能复用报告中的通过率。

评测**不带** baseline 跑自己
----------------------------
:func:`run_baseline` 造路由表时**刻意不传** `baseline`。带上的话，上一轮判定"不达标"
的点在本轮会被强制走规则，于是恒"通过"——评测变成自我实现，一个真实能力为零的模型
也能拿到满通过率。评测要回答的是"这个点交给模型行不行"，故必须让模型真的被问到。
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any, Final

from pydantic import Field

from docfidelity.contracts.base import FormatModel
from docfidelity.contracts.config import RoutingConfig
from docfidelity.contracts.decision_catalog import (
    DECISION_POINT_IDS,
    DECISION_POINTS,
    MODEL_ROUTED,
    decision_point,
)
from docfidelity.contracts.errors import EngineError, ErrorCode
from docfidelity.platform import fs as platform_fs
from docfidelity.platform.hashing import digest_payload

from .recorder import MemoryRecorder, now_utc
from .routing import DEFAULT_MAX_RETRIES, DecisionContext, build_routing, dispatch

__all__ = [
    "BASELINE_FILENAME",
    "CASE_SOURCE_VERSION",
    "BaselineCase",
    "BaselineReport",
    "BaselineStatus",
    "CaseOutcome",
    "baseline_digests",
    "cases_from_payload",
    "cases_to_payload",
    "default_cases",
    "endpoint_digest",
    "load_status",
    "report_to_payload",
    "run_baseline",
    "save_report",
    "uncovered_points",
    "verdict",
]

#: 样本集文件的宿主版本。与 `BaselineCase.format_version` 是**两件事**：前者管
#: "样本集这个容器的形状"，后者管"单条样本的形状"。混在一起会让"改一条样本"
#: 看起来像"换了一种样本集格式"。
CASE_SOURCE_VERSION: Final = "1.0.0"

#: 评测报告的落盘文件名（相对存储根）。与服务端的 `BASELINE_FILENAME` 同源，
#: 故在这里定一次、由服务端引用——两处各写一份字面量，改一处就会静默失联。
BASELINE_FILENAME: Final = "baseline.json"

#: 内置样本集（包内资源，随后发行版分发）。
#: 与词表（`qa/rules/forbidden_words.txt`）同一套做法：**数据放数据文件**，
#: 样本会被人反复增删，而改代码要走评审——两者的变更频率差一个量级。
_CASES_PATH: Final = Path(__file__).resolve().parent / "baseline_cases.json"

#: 报告里保留的**去重失败原因**条数上限。
#: 上限存在的意义是报告要能被界面整段显示；截断时会在最后一条里说明"还有更多"，
#: 而不是静默少几条——静默截断会让"只有一种失败原因"与"原因很多"看起来一样。
_DISTINCT_FAILURE_LIMIT: Final = 5


class BaselineCase(FormatModel):
    """一条评测样本：问某个决策点一个问题，并给出**可判分**的期望。

    ``expect`` 是载荷的**子集**（逐字段相等），不是整份载荷。两点理由：

    * 期望写在样本里是给人读的，逐字段列全等于把契约抄了第二份，而契约一改
      所有样本都要跟着改——那时人们的选择会是"不改，反正只看关键字段"，
      于是样本集悄悄失效；
    * 只写关键字段恰好对应真实的采纳判据："模型在这一件事上有没有答对"。

    ``expect`` 为空表示只要求"结论被采纳"（不校验取值）——适用于结论是自由文本的点。
    但**能校验的一定要校验**：一个只要求"别降级"的样本，会把"模型编了一个不存在的
    章节名"判成通过，而那正是候选注入要防的事（需求 5.2.2）。
    """

    case_id: str = Field(description="样本身份；同一决策点内唯一")
    decision_point: str = Field(description="被测决策点（必须是模型档，见 validate）")
    target: str = Field(default="", description="作用对象，进 DecisionRequest.target")
    candidates: list[str] = Field(default_factory=list, description="候选集（需求 5.2.2）")
    inputs: dict[str, Any] = Field(default_factory=dict)
    context: dict[str, Any] = Field(default_factory=dict)
    expect: dict[str, Any] = Field(default_factory=dict, description="期望的载荷子集")
    note: str = Field(default="", description="这条样本想验什么；评测报告会带上它")


class CaseOutcome(FormatModel):
    """一条样本的评测结果。

    ``adopted`` 与 ``passed`` 分开：前者说"模型结论被采纳了"（路由层没降级），
    后者说"采纳了**且答对了**"。合成一个的后果是诊断时分不清
    "模型答得不对"与"模型答对了但格式/置信度没过"——两者的处置完全不同
    （改样本 / 换模型或调阈值）。
    """

    case_id: str
    decision_point: str
    passed: bool
    adopted: bool
    chosen: str | None = None
    expected: dict[str, Any] = Field(default_factory=dict)
    reason: str = ""
    attempts: int = 0
    confidence: float | None = None
    admission_evidence: dict[str, Any] = Field(default_factory=dict)
    fallback_used: bool = False
    missing_confidence_count: int = 0
    tokens_in: int = 0
    tokens_out: int = 0


class BaselineReport(FormatModel):
    """一次评测的结论（落 `<storage_root>/baseline.json`，被服务端读回喂给路由表）。

    ``by_point`` 是**逐决策点**通过率，也正是 `routing.resolve_routes(baseline=…)`
    要的那个映射——不另做一个"从报告到映射"的转换步骤，避免出现第二份口径
    （例如某处四舍五入、某处不）。
    """

    model: str = Field(description="被测模型标识；换模型即换了一份证据")
    endpoint_digest: str = Field(default="", description="部署端点的不可逆指纹；换端点即换证据")
    cases_digest: str = Field(description="样本集指纹；样本变了则本报告不可比")
    routing_digest: str = Field(
        default="",
        description="路由表指纹（评测时的**姿态**）；姿态变了则本报告不可比，理由同 cases_digest",
    )
    created_at: datetime | None = None
    total: int = 0
    passed: int = 0
    pass_rate: float = Field(default=0.0, ge=0.0, le=1.0)
    accuracy: float = Field(default=0.0, ge=0.0, le=1.0, description="被采纳回答中的语义正确率")
    fallback_count: int = Field(default=0, ge=0)
    missing_confidence_count: int = Field(
        default=0, ge=0, description="未提供置信度的调用次数，含重试"
    )
    tokens_in: int = Field(default=0, ge=0, description="全部尝试累计输入 token")
    tokens_out: int = Field(default=0, ge=0, description="全部尝试累计输出 token")
    by_point: dict[str, float] = Field(default_factory=dict)
    uncovered: list[str] = Field(
        default_factory=list,
        description="**没有被样本覆盖**的模型档决策点（见 uncovered_points）",
    )
    failures: list[str] = Field(
        default_factory=list,
        description="去重后的失败原因（**逐字转发路由层措辞**，见 _distinct_failures）",
    )
    outcomes: list[CaseOutcome] = Field(default_factory=list)

    @property
    def failed(self) -> tuple[CaseOutcome, ...]:
        """未通过的样本（诊断入口：先看它们，再看通过率）。"""
        return tuple(item for item in self.outcomes if not item.passed)


def _distinct_failures(outcomes: Sequence[CaseOutcome]) -> list[str]:
    """去重后的失败原因，**原样转发**（不重写、不归类）。

    为什么不在这里"归个类"（比如 low_confidence / schema_invalid）而只转发原文：
    "为什么这一条没过"的判据在路由层（``RoutingTable._try_model`` 的失败分支），
    在这里按字符串再判一次，就是同一份口径的第二处实现——而它的症状是
    "报告说格式错、路由层记的是低置信"，两边都像是真的。措辞由路由层给，
    这里只负责让它在**不翻报告 JSON** 的情况下看得见（7.1.1 的镜射要求）。
    """
    seen: list[str] = []
    truncated = False
    for outcome in outcomes:
        if outcome.passed or not outcome.reason or outcome.reason in seen:
            continue
        if len(seen) >= _DISTINCT_FAILURE_LIMIT:
            truncated = True
            break
        seen.append(outcome.reason)
    if truncated:
        seen.append("（另有更多失败原因，完整清单见报告 outcomes）")
    return seen


def uncovered_points(cases: Sequence[BaselineCase]) -> tuple[str, ...]:
    """模型档决策点里**一条样本都没有**的那些。**这是"分级有没有依据"的可见面**。

    为什么必须显式列出来，而不是靠"``by_point`` 里没有它就是没测过"去推：
    ``routing`` 对"没有基线数据"的处理是**按目录声明路由**（见 `_resolve_one` 的
    ``rate is None`` 分支）——那是有意为之（否则一接上模型、评测还没跑，全部点都会被
    规则接管，功能等于没接）。于是一个没测过的点在界面上与一个测过且达标的点长得一样。
    把"没测过"单列出来，才是"未测 ≠ 已验证"这句话在数据上的落点。
    """
    covered = {case.decision_point for case in cases}
    return tuple(
        point.id
        for point in DECISION_POINTS
        if point.kind in MODEL_ROUTED and point.id not in covered
    )


class BaselineStatus(FormatModel):
    """基线数据的**加载状态**（服务端把它下发到设置页，使"基线生效了吗"可见）。

    为什么不是"有数据 / 没数据"的布尔：没加载的原因有几类，处置也不同——
    "还没跑过评测"是正常状态（按目录声明路由），"报告存在但读不动"则是资产或
    部署出了问题。把两者显示成同一句话，会让人去查错的地方。
    """

    path: str = ""
    loaded: bool = False
    problem: str = Field(default="", description="未加载的**原文原因**（服务端措辞）")
    model: str = ""
    endpoint_digest: str = ""
    cases_digest: str = ""
    routing_digest: str = Field(
        default="", description="评测时的路由姿态指纹；与 by_point 一起看才不会被误读"
    )
    by_point: dict[str, float] = Field(
        default_factory=dict, description="逐决策点通过率；正是 routing 要喂的那张表"
    )
    uncovered: list[str] = Field(
        default_factory=list, description="模型档里**没被测过**的点（未测 ≠ 已验证）"
    )
    failures: list[str] = Field(
        default_factory=list, description="去重后的失败原因（逐字转发，界面直接显示）"
    )


# ------------------------------------------------------------------ 判分
def verdict(case: BaselineCase, outcome: Any) -> tuple[bool, bool, str]:
    """判定一条样本：``(通过, 被采纳, 原因)``。**判分口径的唯一实现**。

    通过的必要条件是"被采纳"（``source == "model"``，即路由层没降级）：本评测要回答
    的问题就是"这个点交给模型能不能驱动"，而规则兜底接住的样本恰恰说明**不能**。
    """
    adopted = outcome.source == "model"
    if not adopted:
        return False, False, outcome.reason or "模型结论未被采纳（已降级到规则默认值）"
    payload = outcome.output
    if not isinstance(payload, Mapping):
        return False, True, "结论被采纳但载荷为空，无法判分"
    for key, want in case.expect.items():
        if key not in payload:
            return False, True, f"载荷缺少期望字段 {key}"
        got = payload[key]
        if got != want:
            return False, True, f"字段 {key} 期望 {want!r}，实际 {got!r}"
    return True, True, "结论被采纳且命中期望"


# ------------------------------------------------------------------ 评测
def run_baseline(
    cases: Sequence[BaselineCase],
    *,
    adapter: Any,
    model: str = "",
    endpoint_digest: str = "",
    max_retries: int = DEFAULT_MAX_RETRIES,
    config: RoutingConfig | None = None,
) -> BaselineReport:
    """跑一轮评测。**不改任何东西**——只读样本、问模型、统计。

    ``model`` 由调用方给（适配层不一定知道自己被配置成什么标识），故不做成
    "从 adapter 里猜"：猜出来的模型名会让报告看起来像另一份证据。

    ``config`` 是评测时的**姿态**（按配置解析出的路由表），缺省为不覆盖任何点。
    为什么要收这个参数：通过率是"在这个门槛下能不能驱动"的结论，门槛本身就是结论的
    一部分。不报置信度的网关在默认策略下回退待确认；只有已实现独立安全校验的
    章节放置点可以显式配置替代准入依据。评测与部署使用同一策略，策略指纹进入
    ``routing_digest``，端点指纹进入 ``endpoint_digest``，避免复用其他姿态的结论。
    """
    _validate(cases)
    # 不传 baseline：见模块文档末段（带上的话评测会自我实现）。
    table = build_routing(config, model_enabled=True)
    recorder = MemoryRecorder(job_id="baseline")
    ctx = DecisionContext(
        table=table,
        recorder=recorder,
        adapter=adapter,
        job_id="baseline",
        max_retries=max_retries,
    )

    outcomes: list[CaseOutcome] = []
    for case in cases:
        decision = dispatch(
            case.decision_point,
            case.inputs,
            ctx,
            target=case.target,
            candidates=case.candidates,
            context=case.context,
        )
        passed, adopted, reason = verdict(case, decision)
        outcomes.append(
            CaseOutcome(
                case_id=case.case_id,
                decision_point=case.decision_point,
                passed=passed,
                adopted=adopted,
                chosen=decision.chosen,
                expected=dict(case.expect),
                reason=reason,
                attempts=decision.attempts,
                confidence=decision.confidence,
                admission_evidence=dict(decision.admission_evidence),
                fallback_used=decision.fallback_used,
                missing_confidence_count=decision.missing_confidence_count,
                tokens_in=decision.tokens_in,
                tokens_out=decision.tokens_out,
            )
        )

    return _report(
        cases, outcomes, model=model, endpoint_digest=endpoint_digest, routes=table.describe()
    )


def _report(
    cases: Sequence[BaselineCase],
    outcomes: Sequence[CaseOutcome],
    *,
    model: str,
    endpoint_digest: str = "",
    routes: Sequence[Mapping[str, Any]] = (),
) -> BaselineReport:
    """按样本与逐条结果算出报告（逐点通过率在这里**只算一次**）。

    ``routes`` 是这次评测实际用的路由表（``RoutingTable.describe()``）。它只用来算
    ``routing_digest``：把姿态本身变成报告的一部分，使"这份通过率是在什么门槛下测的"
    不必靠人记。缺省空序列表示调用方没给姿态（例如手工构造报告做单测），指纹为空。
    """
    totals: dict[str, int] = {}
    passes: dict[str, int] = {}
    for case, outcome in zip(cases, outcomes, strict=True):
        totals[case.decision_point] = totals.get(case.decision_point, 0) + 1
        if outcome.passed:
            passes[case.decision_point] = passes.get(case.decision_point, 0) + 1
    passed = sum(passes.values())
    adopted = sum(outcome.adopted for outcome in outcomes)
    cases_digest, routing_digest = baseline_digests(cases, routes)
    return BaselineReport(
        model=model,
        endpoint_digest=endpoint_digest,
        cases_digest=cases_digest,
        routing_digest=routing_digest,
        created_at=now_utc(),
        total=len(cases),
        passed=passed,
        pass_rate=0.0 if not cases else passed / len(cases),
        accuracy=0.0 if not adopted else passed / adopted,
        fallback_count=sum(outcome.fallback_used for outcome in outcomes),
        missing_confidence_count=sum(outcome.missing_confidence_count for outcome in outcomes),
        tokens_in=sum(outcome.tokens_in for outcome in outcomes),
        tokens_out=sum(outcome.tokens_out for outcome in outcomes),
        by_point={point: passes.get(point, 0) / total for point, total in sorted(totals.items())},
        uncovered=list(uncovered_points(cases)),
        failures=_distinct_failures(outcomes),
        outcomes=list(outcomes),
    )


def baseline_digests(
    cases: Sequence[BaselineCase], routes: Sequence[Mapping[str, Any]]
) -> tuple[str, str]:
    """评测与生产准入共用的样本、路由姿态指纹。"""
    return (
        digest_payload([case.model_dump(mode="json") for case in cases]),
        digest_payload(list(routes)) if routes else "",
    )


def endpoint_digest(base_url: str) -> str:
    """评测与生产共用端点指纹，输出不包含 URL 或密钥。"""
    return digest_payload({"base_url": base_url.rstrip("/")})


def _validate(cases: Sequence[BaselineCase]) -> None:
    """样本集自检：指向不存在的点、或指向**规则档**的点，都必须当场可见。

    规则 / 人工档的点不会走模型（``RoutingTable.resolve`` 直接给规则结论），故给它们
    写样本必然恒不过——而恒不过的通过率会反过来让路由表"强制规则接管"一个本来就
    是规则的档位。数字看着正常，过程全错。
    """
    seen: set[str] = set()
    for case in cases:
        if case.decision_point not in DECISION_POINT_IDS:
            raise EngineError(
                ErrorCode.CONFIG_INVALID,
                f"评测样本指向未登记的决策点：{case.decision_point}",
                where={"case_id": case.case_id},
                remediation=["先把该点登记到 agents/catalog.py 的 DECISION_POINTS"],
            )
        kind = decision_point(case.decision_point).kind
        if kind not in MODEL_ROUTED:
            raise EngineError(
                ErrorCode.CONFIG_INVALID,
                f"评测样本指向规则档决策点：{case.decision_point}",
                where={"case_id": case.case_id, "kind": kind},
                remediation=[
                    "规则 / 人工档由确定性路径给出，不会走模型，评测它们没有意义",
                    "删除这条样本，或改报告一个模型档的点",
                ],
            )
        if case.case_id in seen:
            raise EngineError(
                ErrorCode.CONFIG_INVALID,
                f"评测样本身份重复：{case.case_id}",
                where={"case_id": case.case_id},
                remediation=["同一决策点内 case_id 必须唯一（报告按它定位到具体样本）"],
            )
        seen.add(case.case_id)


# ------------------------------------------------------------------ 编解码
def cases_from_payload(payload: Any) -> tuple[BaselineCase, ...]:
    """把样本集载荷（``{"cases": [...]}``）解成样本元组。形状不对即抛。

    刻不容缓地抛而不是"跳过坏的那几条"：静默跳过会让一次"样本少了一条"的评测
    看起来像是"跑过了"，而通过率随之变化——没人会去数样本条数。
    """
    if not isinstance(payload, Mapping):
        raise EngineError(
            ErrorCode.CONFIG_INVALID,
            "样本集顶层必须是对象",
            where={"actual": type(payload).__name__},
        )
    items = payload.get("cases")
    if not isinstance(items, list):
        raise EngineError(
            ErrorCode.CONFIG_INVALID,
            "样本集缺少 cases 数组",
            remediation=['期望形如 {"cases": [{"case_id": ..., "decision_point": ...}]}'],
        )
    return tuple(BaselineCase.load(item) for item in items)


def cases_to_payload(cases: Iterable[BaselineCase]) -> dict[str, Any]:
    """样本集载荷（`cases_from_payload` 的逆）。"""
    return {
        "case_source_version": CASE_SOURCE_VERSION,
        "cases": [case.model_dump(mode="json") for case in cases],
    }


def default_cases() -> tuple[BaselineCase, ...]:
    """随包分发的内置样本集。缺文件即抛——它是**发行版的一部分**，不是可选件。"""
    if not _CASES_PATH.is_file():
        raise EngineError(
            ErrorCode.INTERNAL,
            f"内置样本集缺失：{_CASES_PATH}",
            remediation=["确认打包包含 agents/baseline_cases.json（见 pyproject 的 wheel 配置）"],
        )
    return cases_from_payload(json.loads(platform_fs.read_text(_CASES_PATH)))


def report_to_payload(report: BaselineReport) -> dict[str, Any]:
    """评测报告载荷（**落盘用的就是它**）。"""
    return report.model_dump(mode="json")


def save_report(report: BaselineReport, path: str | Path) -> Path:
    """原子写报告（`platform.fs.atomic_write_text`：不留半成品，换行不做平台转换）。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    return platform_fs.atomic_write_text(
        target, json.dumps(report_to_payload(report), ensure_ascii=False, indent=2) + "\n"
    )


def load_status(path: str | Path) -> BaselineStatus:
    """读评测报告的**加载状态**。**永不抛**。

    三条路径各有各的话说，因为它们指向不同的处置：

    * 文件不存在 —— 正常状态（还没跑过评测，按目录声明路由）；
    * 文件在但解析不了 —— 资产或部署出了问题，值得去查；
    * 文件在且能解析 —— 报告摘要（模型、样本指纹、逐点通过率）。

    这个函数**不做"是否达标"的判断**：达标是 `routing` 的事（阈值在目录里声明），
    在这里再判一次就成了第二份口径，而两份口径的分歧表现为"设置页说生效了、
    实际被规则接管"。
    """
    target = Path(path)
    if not target.is_file():
        return BaselineStatus(
            path=str(target),
            loaded=False,
            problem="未找到评测报告：尚无可用的逐点评测证据。",
        )
    try:
        report = BaselineReport.load(json.loads(platform_fs.read_text(target)))
    except Exception as exc:  # 报告是人手工产出的资产，坏掉的原因不值得分类
        return BaselineStatus(
            path=str(target),
            loaded=False,
            problem=f"评测报告无法解析：{type(exc).__name__}: {exc}",
        )
    return BaselineStatus(
        path=str(target),
        loaded=True,
        problem="",
        model=report.model,
        endpoint_digest=report.endpoint_digest,
        cases_digest=report.cases_digest,
        routing_digest=report.routing_digest,
        by_point=report.by_point,
        uncovered=list(report.uncovered),
        failures=list(report.failures),
    )
