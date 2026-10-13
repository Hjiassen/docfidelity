"""参考构建包：把内核各层接成流水线的五个阶段体（DD-13.1 的 ``docfidelity.build_stages``）。

它在架构里的位置
----------------
``pipeline`` 只定义"有哪些阶段、按什么顺序跑、失败了怎么办"，**不知道任何阶段内部
在做什么**；阶段体由调用方注册。本包就是那个"调用方"的**参考实现**：它认识
``ingest`` / ``compose`` / ``render`` / ``registry`` / ``qa`` 五层，并把它们各自的
入口函数接成一个 :class:`~docfidelity.pipeline.StageRegistry`。

为什么"参考"而不是"唯一"
------------------------
服务端发现阶段体走的是**入口点**（``docfidelity.build_stages``）而不是硬编码 import
——"这颗内核的构建阶段体由谁提供"是**部署选择**（DD-13.1）。本包作为随内核分发的
那一个提供者，让"``git clone`` → 装 → 就能构建"成立（需求 场景 E 第 1 步 / 验收 31）；
换一套渲染策略或换一个事实源接法，装另一个提供者并把本包卸掉即可，``server`` 一行
都不用改。

它为什么在 ``docfidelity`` 里而不在 ``server`` 里
-------------------------------------------------
``server`` 被禁止直接 import 内核实现（``.importlinter`` 的 ``server-has-no-ooxml``），
所以"把五层接起来"这件事只能在核心里做。而"接起来"必然要 import 内核各层，故本包
位于 ``pipeline`` **之上**——.importlinter 里为它单列一条契约。

阶段之间的数据怎么传（这里有个容易踩的坑）
------------------------------------------
``run_stage`` 只把 ``payload`` 递给阶段体，**不递上游阶段的输出**。所以"上游产出的
文件在哪"由 :mod:`.inputs` 的常量回答（``work/facts.json`` / ``work/ir.json`` …），
而 ``payload`` 只回答"这次构建的输入是什么"。五个阶段体共用同一份布局，别在某个
阶段里另拼一份路径。

``refresh`` 阶段为什么如实返回"不适用"
--------------------------------------
DD-9.6 的两步（目录重建、域刷新）已经收在 ``render()`` 的第 10 / 11 步里——渲染结束时
产物**已经是刷新过的**（有 Word 时真刷新、没有就降级，见 ``render.toc``）。故独立的
``refresh`` 阶段没有第二个动作可做，如实报 ``skipped`` 而不是再刷一遍：再刷一遍
既让"产物摘要在两次刷新之间变了一次、最终值取哪个"没法回答，也会让"降级"这件事
在阶段列表上多出一个假的失败点。真去掉了 ``render`` 里的刷新之后，这里才是它的落点。
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Final, cast

from docfidelity.compose import compose
from docfidelity.contracts.errors import ConfigurationError, EngineError, ErrorCode
from docfidelity.contracts.fact import FactSnapshot, SourceConfig
from docfidelity.contracts.ir import ContentIR, ContentUnit
from docfidelity.contracts.job import Decision, ModelCall, StageName
from docfidelity.contracts.qa import (
    Finding,
    Location,
    RuleSet,
    RuleSeverity,
    TraceMatrix,
)
from docfidelity.ingest import SourceGuard, default_registry, merge, run_connector
from docfidelity.ooxml import DocxPackage
from docfidelity.pipeline import (
    ARTIFACTS_DIRNAME,
    STAGE_ORDER,
    StageContext,
    StageFunc,
    StageOutcome,
    StageRegistry,
    domain_registry,
)
from docfidelity.platform.fs import ensure_dir
from docfidelity.qa import QaContext, RuleSpec, default_catalog, rules_for
from docfidelity.qa import run as run_gate
from docfidelity.qa.type_agnostic import ir_words
from docfidelity.registry import read_package
from docfidelity.render import (
    RenderPolicy,
    RenderResult,
    open_working_copy,
    output_filename,
    render,
)

from .inputs import (
    DECISIONS_REL,
    FACTS_REL,
    FINDINGS_DIRNAME,
    IR_REL,
    RENDER_REL,
    REPORT_REL,
    TRACES_REL,
    BuildInputs,
    findings_rel,
)

__all__ = [
    "INGEST_RULE_ID",
    "REFRESH_SKIP_REASON",
    "build_registry",
    "compose_stage",
    "ingest_stage",
    "provide",
    "qa_stage",
    "refresh_stage",
    "render_stage",
]

#: 本包在 **ingest** 阶段所判之事的需求条目：FR-M1-12「接入校验」。
#: 它明确写着"事实源缺项、格式异常、编码问题在接入阶段即报错，**不进入编排**"——
#: 这就是本阶段对来源失败采取"整阶段失败"而不是"跳过后继续"的依据。
INGEST_RULE_ID: Final = "FR-M1-12"

#: ``refresh`` 阶段如实报"不适用"的原因（见模块文档）。写成常量而不是就地写字面量：
#: 阶段列表页要照抄这句话，两处不一致时"界面说的原因"与"服务端说的原因"就分叉了。
REFRESH_SKIP_REASON: Final = (
    "目录重建与域刷新已并入 render 阶段（DD-9.6 的第 10/11 步），本阶段无独立动作"
)


# ================================================================== ingest
def ingest_stage(ctx: StageContext) -> StageOutcome:
    """事实源声明 → 多源合并后的事实快照（DD-5）。

    **这是"来源声明被真实消费"的落点**：本阶段取冻结进来的 ``SourceConfig`` 声明，
    经 ``ingest.SourceGuard`` 解析 ``root``、真跑连接器、再 ``merge`` 成一个快照。
    在此之前，``sources/*`` 那组端点只能回答"这份来源配置对不对"，构建路径上没有任何
    一步会把连接器**真的跑一遍**。

    失败策略：**整阶段失败**，不"跳过后继续"
    ----------------------------------------
    FR-M1-12 写的是"接入阶段即报错，**不进入编排**"。跳过一个坏来源继续往下走，
    产出的是一份**少了一整章**的文档，而报告里只有一条 major——那正是本项目反复
    防的那个形态（"文档少内容而报告看起来还行"）。所以这里第一处失败即返回，
    错误载荷里带着出问题的 ``source_id`` 与接入器给的定位。
    """
    inputs = BuildInputs.of(ctx.payload)
    enabled = [config for config in inputs.sources if config.enabled]
    table = default_registry()
    # 解析基点由编排冻进来（``BuildInputs.source_root``），**不是**作业工作区：
    # 「试接入」按存储根解析，构建期必须与它同源，否则界面说通了、构建说找不到。
    guard = SourceGuard(workspace_root=inputs.source_root)

    bundles = []
    for config in enabled:
        try:
            bundles.append(run_connector(table.get(config.source_type), config, guard=guard))
        except EngineError as exc:
            # 不带 findings 返回：这一阶段没有"部分结论"，只有一次失败。
            return StageOutcome.from_error(_localized(exc, config))

    snapshot, findings = merge(bundles, project_id=inputs.project_id)
    findings.extend(_empty_source_findings(bundles))
    rel = ctx.workspace.put_json(FACTS_REL, snapshot.model_dump(mode="json"))
    _write_findings(ctx, findings)

    if not enabled:
        # "这份文档不引用任何来源"是**不适用**，不是成功：快照照写（下游要读文件），
        # 但阶段状态如实记 skipped。它不会静默通过——``compose`` 会按档案的骨架去要
        # 事实，槽位填不上就报 blocker（FR-M3-15），门禁再据此阻断。
        return StageOutcome(
            skipped_reason="这份文档没有声明启用的事实源（DocumentEntry.source_ids 为空）",
            detail={"declared": len(inputs.sources), "loaded": 0},
        )

    return StageOutcome(
        outputs={"facts": rel},
        findings=list(findings),
        detail={
            "declared": len(inputs.sources),
            "loaded": len(bundles),
            "facts": len(snapshot.merged),
            "conflicts": len(snapshot.conflicts),
        },
    )


def _localized(error: EngineError, config: SourceConfig) -> EngineError:
    """把"是哪一条来源出的问题"补进 ``where``。

    必须补在这一层——``run_connector`` 之上的任何位置都只知道"某份文档的 ingest 阶段
    失败了"。把关（``SourceGuard``）抛的错只带得出发出问题的那个**文件名**：
    ``E_UPLOAD_TYPE 扩展名不在白名单内：(无扩展名)`` 放在一份引用了 3 条来源的文档上，
    使用者无从知道该去修哪一条。

    键名取 ``source`` 而不是 ``source_id``：ingest 层既有约定就是 ``source``
    （``loader.skip_warning`` 与 ``_support.root_path`` 都这么写），另立一个键只会让
    同一条来源在错误载荷里有两个名字。**不覆盖**已有的 ``source``——接入器自己填的
    比这里更具体。
    """
    where = {
        "source": config.source_id,
        "source_type": config.source_type,
        **error.where,
    }
    return EngineError(
        error.code,
        error.message,
        where=where,
        remediation=list(error.remediation),
        fatal=error.fatal,
    )


def _empty_source_findings(bundles: Sequence[Any]) -> list[Finding]:
    """接入成功但**一条事实都没有**的来源——记 major，不记 blocker。

    为什么不失败：空文件、空目录、过滤规则把内容全滤掉了，这三种都可能是**使用者
    有意为之**（例如这一轮还没往来源里放东西）。但"配置对了、接入通了、结果是零条"
    几乎总是配置错了一处（指向了空目录），所以必须报出来，而且必须让人在报告里看得见。
    """
    return [
        Finding(
            rule_id=INGEST_RULE_ID,
            severity="major",
            message=f"来源 {bundle.source_id!r} 接入成功但没有产出任何事实",
            location=Location(part=str(bundle.source_type)),
            evidence=f"source_type={bundle.source_type}",
            suggestion="确认该来源声明里的 root 指向的文件或目录不是空的，且过滤规则没有滤掉全部内容",
        )
        for bundle in bundles
        if not bundle.facts
    ]


# ================================================================= compose
def compose_stage(ctx: StageContext) -> StageOutcome:
    """事实快照 + 档案 → 内容 IR（DD-8.1）。

    ``domain_registry`` 从冻结的领域模型声明装配：``for_each`` 指向的 ``kind`` 必须在
    这里面（FR-M2-23）。**空注册表不等于拿不到注册表**——库是空的就去报
    ``E_DOMAIN_KIND_UNKNOWN``，而不是悄悄装配出一份零单元的 IR。
    """
    inputs = BuildInputs.of(ctx.payload)
    snapshot = FactSnapshot.model_validate(ctx.workspace.read_json(FACTS_REL))
    placements: list[dict[str, Any]] = []
    decision_findings: list[Finding] = []

    def resolve_placement(unit: ContentUnit, candidates: Mapping[str, list[str]]) -> str | None:
        summary = _placement_summary(unit)
        semantic_candidates = list(candidates) if summary else []
        outcome = ctx.decide(
            "placement.section_slot",
            {
                "unit_ref": unit.unit_id,
                "summary": summary,
                "chapters": semantic_candidates,
            },
            target=unit.unit_id,
            candidates=semantic_candidates,
            context={"placement_constraints": dict(candidates)},
        )
        selected = (
            outcome.chosen if outcome.source == "model" and outcome.status == "auto" else None
        )
        applied = selected if selected in candidates else next(iter(candidates), None)
        placements.append(
            {
                "unit_id": unit.unit_id,
                "chosen": outcome.chosen,
                "applied": applied,
                "source": outcome.source,
                "status": outcome.status,
            }
        )
        if outcome.status == "pending":
            decision_findings.append(
                Finding(
                    rule_id="NFR-26",
                    severity="major",
                    message="章节落位待确认；已使用档案候选顺序的规则默认值",
                    location=Location(unit_id=unit.unit_id),
                    evidence=json.dumps(placements[-1], ensure_ascii=False),
                    suggestion="核对落位决策并裁决，或在档案中显式绑定投影槽位",
                )
            )
        return selected

    decision_start = len(ctx.decisions.decisions())
    call_start = len(ctx.decisions.model_calls())
    result = compose(
        snapshot,
        inputs.profile,
        domain=domain_registry(inputs.domain_models),
        doc_key=inputs.doc_key,
        template_ref=inputs.template_ref,
        terminology=inputs.terminology,
        purity_words=_purity_words(inputs),
        placement_resolver=resolve_placement,
    )
    evidence_rel = ctx.workspace.put_json(
        DECISIONS_REL,
        {
            "placements": placements,
            "decisions": [
                item.model_dump(mode="json") for item in ctx.decisions.decisions()[decision_start:]
            ],
            "model_calls": [
                item.model_dump(mode="json") for item in ctx.decisions.model_calls()[call_start:]
            ],
        },
    )
    ir_rel = ctx.workspace.put_json(IR_REL, result.ir.model_dump(mode="json"))
    traces_rel = ctx.workspace.put_json(TRACES_REL, result.traces.model_dump(mode="json"))
    findings = [*result.findings, *decision_findings]
    _write_findings(ctx, findings)
    return StageOutcome(
        outputs={"ir": ir_rel, "traces": traces_rel, "decisions": evidence_rel},
        findings=findings,
        detail={
            "doc_key": inputs.doc_key,
            "template_ref": inputs.template_ref,
            "blockers": len(result.blockers()),
            "units": len(result.ir.units),
        },
    )


def _placement_summary(unit: ContentUnit) -> str:
    """裁剪正文与结构化可见内容，缺少语义依据时不请求模型。"""
    from itertools import islice

    excerpts: list[str] = []
    for item in islice(unit.walk(), 8):
        if item.text and item.text.strip():
            excerpts.append(item.text[:512])
        caption = item.fields.get("caption")
        if isinstance(caption, str) and caption.strip():
            excerpts.append(caption[:512])
        for key in ("columns", "items", "rows"):
            values = item.fields.get(key)
            if not isinstance(values, list):
                continue
            for value in values[: 3 if key == "rows" else 8]:
                cells = value[:8] if isinstance(value, list) else [value]
                texts = [cell[:128] for cell in cells if isinstance(cell, str) and cell.strip()]
                if texts:
                    excerpts.append(json.dumps({key: texts}, ensure_ascii=False))
    return "\n".join(excerpts)[:4096]


def _purity_words(inputs: BuildInputs) -> tuple[str, ...]:
    """IR 纯净度自检用的外部词表。

    冻结输入里没有声明时，回落到内核自带的**唯一**词表（``qa.type_agnostic`` 用的
    那一份，见 ``qa/rules/forbidden_words.txt``）。回落是刻意的：词表是"引擎不得
    出现的文档类型名与领域概念名"，属于内核自带的纪律，而不是部署参数——不回落会
    让"没配词表"安静地等于"不做这项检查"。

    **回落的是 ``ir_words()`` 而不是整份词表**：词表分三组，``branch_literal`` 那一组
    只对源码 AST 有意义（"``x.kind == "..."`` 形式的类型分支"）。IR 检查只能做子串
    匹配，把 ``endpoint`` / ``table`` 这类词并进来会让**每一份合法 IR** 都被判 blocker
    ——``unit_id`` 由档案的 ``for_each`` 派生、``type`` 就是封闭枚举的取值。词表口径
    与 ``qa.type_agnostic.scan_ir`` 共用同一个入口，不各写一份。

    安全性由 ``compose.purity`` 的口径保证：它只查**引擎表面**（单元键名与
    ``unit_id`` / ``type`` / ``level``），事实数据的**值**不查——所以事实里出现了
    一个文档类型名不会被误判成 blocker（见该模块的说明）。
    """
    if inputs.purity_words:
        return inputs.purity_words
    return tuple(sorted(ir_words()))


# ================================================================== render
def render_stage(ctx: StageContext) -> StageOutcome:
    """内容 IR + 模板包 → ``.docx``（DD-9.1 的 13 步）。

    产物落在 ``artifacts/``（工作区里的交付物目录）——服务端的产物登记是**扫目录**
    完成的（``JobRunner._collect_artifacts``），阶段体只要把文件放对地方，不必自己
    去登记，少一处"要记得登记"的手工步骤。

    写盘护栏命中时**不写盘、整阶段失败**
    -------------------------------------
    ``render`` 的护栏拦的是"产物已坏但看不出来"（样式引用悬挂等，DD-10.7）：命中时
    ``RenderResult`` 里没有 ``artifact_path``。此时阶段必须失败，因为**没有文件可交付**；
    这与"门禁判定为 blocked"是两件事——后者是"文件写出来了但不合规"，报告负责说，
    作业本身照常成功。
    """
    inputs = BuildInputs.of(ctx.payload)
    ir = ContentIR.model_validate(ctx.workspace.read_json(IR_REL))
    template = read_package(inputs.template_dir())

    policy = RenderPolicy(pagination=inputs.pagination)
    # 文件名由**契约**派生（``output_filename`` 只用 IR 里的 project_id / doc_key），
    # 故续跑与本次必然一致；清单里再记一遍是给下游一条事实，而不是一份推导。
    out_rel = f"{ARTIFACTS_DIRNAME}/{output_filename(ir, policy=policy.properties)}"
    out_path = ctx.workspace.resolve(out_rel)
    ensure_dir(out_path.parent)

    result = render(
        pkg=open_working_copy(inputs.docx_path()),
        template=template,
        profile=inputs.profile,
        ir=ir,
        out_path=out_path,
        policy=policy,
        office=inputs.office_enabled,
        office_reason=inputs.office_reason,
    )
    findings = result.findings()
    _write_findings(ctx, findings)

    if result.guardrail_blockers:
        return StageOutcome(
            findings=findings,
            error=EngineError(
                ErrorCode.RENDER_BLOCKED,
                f"渲染写盘护栏命中 {len(result.guardrail_blockers)} 项，产物未写盘",
                where={
                    "job_id": ctx.job.job_id,
                    "rules": sorted({item.rule_id for item in result.guardrail_blockers}),
                },
                remediation=[
                    "按报告里的 blocker 逐条修正（多为模板样式缺失或档案引用了不存在的样式）",
                    "产物**没有**生成，修好后重跑本作业",
                ],
            ).to_info(),
            detail={"written": False, "out": out_rel},
        )

    manifest_rel = ctx.workspace.put_json(
        RENDER_REL,
        {
            "docx": out_rel,
            "filename": result.filename,
            "package_hash": result.package_hash,
            "profile_id": result.profile_id,
            "template_ref": result.template_ref,
            "steps": len(result.steps),
            "deferred_steps": list(result.deferred_steps),
        },
    )
    return StageOutcome(
        outputs={"docx": out_rel, "render": manifest_rel},
        findings=findings,
        detail={
            "filename": result.filename,
            "package_hash": result.package_hash,
            "refreshed": _refreshed(result),
        },
    )


def _refreshed(result: RenderResult) -> bool:
    """本轮的域刷新步骤是否真跑了。取不到就报 ``False``（"不知道"不冒充"刷过了"）。

    参数写成 ``RenderResult`` 而不是 ``Any``：字段名猜错（``step.name``）本该被 mypy
    在写下的那一刻拦下，写成 ``Any`` 就只在真跑到这一步时才炸——而它正好在阶段体收尾，
    异常冒到 ``run_stage`` 之外，作业看起来只是"卡在 render"。
    """
    for step in result.steps:
        if step.step == "refresh":
            return bool(step.checkpoint_outputs.get("refreshed"))
    return False


# ================================================================= refresh
def refresh_stage(ctx: StageContext) -> StageOutcome:
    """目录与域刷新——**已并入 render**，故如实报"不适用"（理由见模块文档）。"""
    return StageOutcome(
        skipped_reason=REFRESH_SKIP_REASON,
        detail={"where": "render", "step": "refresh"},
    )


# ====================================================================== qa
def qa_stage(ctx: StageContext) -> StageOutcome:
    """产物 + 契约 + IR → 门禁报告（DD-10.3）。

    报告的"能不能定稿"由 ``Report.verdict`` 说了算，本阶段**不重新推导**一遍
    （FR-M5-16：``can_finalize`` 是唯一出口）。门禁判定为 ``blocked`` **不是阶段失败**：
    文件已经产出、报告已经给出，作业照常成功——是否定稿是人的事。

    上游阶段的判定经 ``QaContext.upstream`` 并进报告（``qa.run`` 会按同一套去重规则
    合并）。这些判定只落在事件流里是**不够**的：报告才是人看的那一页，一份"报告里
    干干净净、事件流里有一条 blocker"的结果会让人放心地定稿。
    """
    inputs = BuildInputs.of(ctx.payload)
    ir = ContentIR.model_validate(ctx.workspace.read_json(IR_REL))
    traces = TraceMatrix.model_validate(ctx.workspace.read_json(TRACES_REL))
    template = read_package(inputs.template_dir())
    manifest = ctx.workspace.read_json(RENDER_REL)
    artifact = ctx.workspace.resolve(str(manifest["docx"]))

    with DocxPackage.open(artifact, read_only=True) as pkg:
        report = run_gate(
            context=QaContext(
                pkg=pkg,
                template=template,
                ir=ir,
                profile=inputs.profile,
                traces=traces,
                terminology=_terminology_of(inputs),
                pagination=inputs.pagination,
                upstream=tuple(_upstream_findings(ctx)),
            ),
            rules=_rules_of(inputs),
            job_id=ctx.job.job_id,
        )

    if ctx.workspace.resolve(DECISIONS_REL).is_file():
        evidence = ctx.workspace.read_json(DECISIONS_REL)
        report.decisions = [Decision.model_validate(item) for item in evidence["decisions"]]
        report.model_calls = [ModelCall.model_validate(item) for item in evidence["model_calls"]]
    rel = ctx.workspace.put_json(REPORT_REL, report.model_dump(mode="json"))
    findings = list(report.findings)
    _write_findings(ctx, findings)
    return StageOutcome(
        outputs={"report": rel},
        findings=findings,
        detail={
            "report_id": report.report_id,
            "verdict": report.verdict,
            "findings": len(report.findings),
            "skipped_rules": list(report.skipped),
        },
    )


def _terminology_of(inputs: BuildInputs) -> Any:
    """术语表；档案声明 ``terminology_scope == "none"`` 时**不给**。

    这一条必须在这里判，而不是留给 ``qa``：``QaContext.capabilities()`` 是"有没有
    给对应输入"推出来的，给了术语表就等于宣告"本上下文具备术语能力"，
    于是一条本该跳过的术语规则会被执行（FR-M5-08）。
    """
    if inputs.profile.bindings.terminology_scope == "none":
        return None
    return inputs.terminology


def _rules_of(inputs: BuildInputs) -> list[RuleSpec]:
    """按档案声明的规则集选出规则（带项目级覆盖）。

    档案**没声明**规则集时取**全部内置**规则集，而不是"不跑门禁"：后者会让"忘了声明"
    安静地等于"这门构建没有门禁"，而报告照样写着 ``pass``。规则集标识不存在时
    ``default_catalog().rule_set`` 会抛 ``E_PROFILE_RULESET_MISSING``——那是对的，
    作业失败比"少跑一套规则"诚实。
    """
    loaded = default_catalog()
    declared = {ref.rule_set_id: ref for ref in inputs.rule_sets}
    ids = sorted(declared) if declared else sorted(loaded.files)
    catalog = loaded.catalog()
    selected: dict[str, RuleSpec] = {}
    for rule_set_id in ids:
        loaded.rule_set(rule_set_id)  # 不存在的规则集在这里就报错（含可选清单）
        ref = declared.get(rule_set_id)
        rule_set = (
            RuleSet(rule_set_id=rule_set_id)
            if ref is None
            else RuleSet(
                rule_set_id=rule_set_id,
                included_rules=list(ref.included_rules),
                excluded_rules=list(ref.excluded_rules),
                # 配置层把严重度写成裸 ``str``（``RuleSetRef`` 不做枚举校验），而契约
                # 层要求 ``RuleSeverity``。cast 是**如实**的：值仍会被 ``RuleSet``
                # （pydantic）再校验一次，写错的值在那里报错，不在这里静默通过。
                severity_overrides=cast("dict[str, RuleSeverity]", dict(ref.severity_overrides)),
                param_overrides={key: dict(value) for key, value in ref.param_overrides.items()},
            )
        )
        for spec in rules_for(rule_set, catalog):
            selected.setdefault(spec.rule_id, spec)
    return [selected[rule_id] for rule_id in sorted(selected)]


# ================================================================ 判定落盘
def _write_findings(ctx: StageContext, findings: Sequence[Finding]) -> str:
    """把本阶段的判定**落盘**——``qa`` 阶段从这里把它们并进报告（见 ``inputs`` 说明）。

    空列表也照写（文件内容是 ``[]``）：文件在 = "这个阶段跑过、结论是没什么可说的"；
    文件不在 = "这个阶段没跑"。两者在报告里是同一句话，而它们该被分开。
    """
    return ctx.workspace.put_json(
        findings_rel(ctx.stage), [item.model_dump(mode="json") for item in findings]
    )


def _upstream_findings(ctx: StageContext) -> Sequence[Finding]:
    """收集上游各阶段自报的判定（**排除本阶段自己的**）。

    排除自己那一份是必须的：续跑时 ``qa`` 上一次留下的文件还在，读进来会把它当成
    "上游的结论"再并一次——同一条判定在报告里出现两次，而人只会以为它更严重。
    """
    directory = ctx.workspace.resolve(FINDINGS_DIRNAME)
    if not directory.is_dir():
        return ()
    mine = f"{ctx.stage}.json"
    collected: list[Finding] = []
    for path in sorted(directory.glob("*.json")):
        if path.name == mine:
            continue
        collected.extend(_read_findings(path))
    return collected


def _read_findings(path: Path) -> list[Finding]:
    raw: Any = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        return []
    return [Finding.model_validate(item) for item in raw]


# ================================================================ 注册
#: 阶段名 → 实现。**五个都提供**：``pipeline`` 的"缺阶段就拒绝发起构建"是保证
#: "能发起就一定能跑完"的实现，半个注册表会让那句话不成立。
_STAGES: Final[Mapping[StageName, StageFunc]] = {
    "ingest": ingest_stage,
    "compose": compose_stage,
    "render": render_stage,
    "refresh": refresh_stage,
    "qa": qa_stage,
}


def provide(registry: StageRegistry) -> None:
    """``docfidelity.build_stages`` 入口点的目标：把五个阶段体登记进给定注册表。

    入口点的 ``value`` 形状是 ``module:callable``，而服务端对它的要求是
    "``register(registry) -> None`` 的一个可调用对象"（见 ``server.api.deps``）。
    故本函数**不造注册表**、也不返回它——注册表由调用方持有，重复登记照
    ``StageRegistry.register`` 的默认语义报错（静默覆盖会让"替换阶段"变成事故）。
    """
    missing = [name for name in STAGE_ORDER if name not in _STAGES]
    if missing:  # pragma: no cover - 只在"契约新增了阶段而本包没跟上"时触发
        raise ConfigurationError(
            f"参考构建包缺少阶段体：{missing}",
            where={"missing": missing},
            remediation=[
                "合同新增了阶段，本参考实现需要补上对应阶段体",
                "这是发行版自身的不一致，不是使用者配置的问题",
            ],
        )
    for stage in STAGE_ORDER:
        registry.register(stage, _STAGES[stage])


def build_registry() -> StageRegistry:
    """自己造一份装好五个阶段体的注册表（测试与嵌入方用）。

    服务端**不用**它：那边走入口点发现（"由谁提供阶段体"是部署选择）。
    """
    registry = StageRegistry()
    provide(registry)
    return registry
