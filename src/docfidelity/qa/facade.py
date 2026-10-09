"""qa 的工具门面（DD-12.1 / 需求 5.5 的 ``qa_run`` / ``style_audit`` / ``drift_check``）。

门禁的常规入口是 ``builder.stages.qa_stage``——它从工作区读文件、构造 ``QaContext``、
跑规则、把报告写回工作区。那是**流水线内部**的入口，参数是运行时对象（已打开的产物
包、内存里的 IR / 追溯）。

工具面（``agents.tools``）只认扁平、可序列化的入参。本模块补的就是那层门面：入口是
"产物路径 + 契约对象"，出口是契约报告，**只读、不落盘、不进工作区**。

为什么门面住在 qa 层而不是 agents 层
------------------------------------
构造 ``QaContext`` 必须打开产物包（``ooxml``），而 ``.importlinter`` 的
``agents-has-no-ooxml`` 禁止 agents 直接或间接依赖 ooxml。门面放在这里，``agents``
只留一个 ``handler_ref`` 字符串——惰性点路径解析不产生静态 import 边，契约不破。
``render`` 的六个工具同理，只是它们的门面只能住在 render 层（见
``agents/tools/library.py`` 的说明）。

与 ``qa_stage`` 不构成"第二套门禁"
----------------------------------
本模块**不重写任何判定**：规则选择复用 ``default_catalog`` + ``rules_for``，执行复用
:func:`~docfidelity.qa.engine.run` / :func:`~docfidelity.qa.engine.run_rule_set`，
比对复用 :func:`~docfidelity.qa.compare.compare`。它只多做一件事——把"路径与契约
对象"装进 ``QaContext``，那正是工具面与流水线之间唯一不同的部分。
"""

from __future__ import annotations

import zipfile
from collections.abc import Sequence
from pathlib import Path
from typing import Final, cast

from docfidelity.contracts.config import Terminology
from docfidelity.contracts.errors import EngineError, ErrorCode
from docfidelity.contracts.ir import ContentIR
from docfidelity.contracts.profile import DocTypeProfile
from docfidelity.contracts.qa import Report, RuleSet, SnapshotDiff, TraceMatrix
from docfidelity.contracts.template import TemplatePackage
from docfidelity.ooxml import DocxPackage

from .compare import DiffMode, compare
from .engine import QaContext, RuleSpec, default_catalog, rules_for, run, run_rule_set

__all__ = ["audit_document", "audit_rule_set", "compare_documents"]

#: 可用的比对分辨率（与 ``pipeline.artifacts.COMPARE_MODES`` 同一取值）。
_MODES: Final[tuple[DiffMode, ...]] = ("structure", "text", "contract")

#: "打不开 / 不是 docx"这一类异常。报 ``PACKAGE_INCOMPLETE`` 而不是 500：点名的产物
#: 里有一份坏了，这是**可操作**的事实（重新产出那一份），而 500 只会说"服务器内部错误"。
_BROKEN_PACKAGE: Final = (OSError, KeyError, ValueError, zipfile.BadZipFile)


def audit_document(
    artifact: str | Path,
    template: TemplatePackage,
    ir: ContentIR,
    *,
    profile: DocTypeProfile | None = None,
    traces: TraceMatrix | None = None,
    terminology: Terminology | None = None,
    rule_set_ids: Sequence[str] | None = None,
    job_id: str | None = None,
) -> Report:
    """按内置规则集复查一份产物，返回门禁报告（需求 5.5 的 ``qa_run``）。

    ``rule_set_ids`` 留空即跑**全部内置**规则集；给了就只跑指定的那几套。判定能力随
    输入自动推导（``QaContext.capabilities``）：没给 ``profile`` / ``traces`` 就不会跑
    依赖它们的那几条规则，且会在报告的 ``skipped`` 里如实列出——不会悄悄"少跑一套、
    报告照写通过"。

    可选参数一律按 ``None`` 归一（``job_id or ""``）：工具面的"未传可选参数"就是
    ``None``，门面得把它折成实现层的默认值，而不是把 ``None`` 转发给 ``Report``。
    """
    rules = _select_rules(rule_set_ids)
    with DocxPackage.open(artifact, read_only=True) as pkg:
        return run(
            context=QaContext(
                pkg=pkg,
                template=template,
                ir=ir,
                profile=profile,
                traces=traces,
                terminology=terminology,
            ),
            rules=rules,
            job_id=job_id or "",
        )


def audit_rule_set(
    artifact: str | Path,
    template: TemplatePackage,
    ir: ContentIR,
    *,
    rule_set_id: str,
    profile: DocTypeProfile | None = None,
    traces: TraceMatrix | None = None,
    terminology: Terminology | None = None,
    job_id: str | None = None,
) -> Report:
    """按指定规则集复查一份产物（需求 5.5 的 ``style_audit``）。

    与 :func:`audit_document` 的分工：后者按"跑哪几套规则集"选择，本函数按**一套**
    规则集选择。``RuleSet`` 自带 included / excluded / 严重度覆盖，是档案声明规则集
    的落地形态，故这里按 ``rule_set_id`` 构造空覆盖的 ``RuleSet`` 再交给引擎——
    项目级覆盖属于流水线的 ``qa_stage``（从 ``BuildInputs`` 读），工具面不重复这条。
    """
    with DocxPackage.open(artifact, read_only=True) as pkg:
        return run_rule_set(
            context=QaContext(
                pkg=pkg,
                template=template,
                ir=ir,
                profile=profile,
                traces=traces,
                terminology=terminology,
            ),
            rule_set=RuleSet(rule_set_id=rule_set_id),
            job_id=job_id or "",
        )


def compare_documents(
    baseline: str | Path,
    candidate: str | Path,
    *,
    mode: str | None = None,
    baseline_ref: str | None = None,
    candidate_ref: str | None = None,
) -> SnapshotDiff:
    """比对两份产物，返回结构快照差异（需求 5.5 的 ``drift_check``）。

    与 ``pipeline.artifacts.compare_documents`` 是同一件事的两个入口：那个在 pipeline
    层（服务端用），这个在 qa 层（工具面用）——因为 agents 不得依赖 pipeline。两者
    唯一的不同是包装：本函数直接返回契约模型 ``SnapshotDiff``（工具面要对它做
    ``model_dump``），而 pipeline 版本再包一层 ``DocumentDiff`` 供服务端复用。

    可选参数按 ``None`` 归一（``mode or "structure"`` 等）：工具面的"未传可选参数"
    就是 ``None``，而 ``SnapshotDiff`` 的字段都是裸 ``str``，把 ``None`` 转发进去会
    在模型构造期抛 ``ValidationError``——那是"参数没给"被误报成"包坏了"。

    Raises
    ------
    EngineError
        模式不认识 → ``E_SCHEMA_INVALID``；两份产物里有一份打不开或不是完整 docx →
        ``E_PACKAGE_INCOMPLETE``。
    """
    resolved = mode or "structure"
    if resolved not in _MODES:
        raise EngineError(
            ErrorCode.SCHEMA_INVALID,
            f"不支持的比对模式：{resolved}",
            where={"mode": resolved, "supported": list(_MODES)},
            remediation=["改用 structure（结构快照）/ text（文本）/ contract（契约字段）"],
        )
    base_ref = baseline_ref or ""
    cand_ref = candidate_ref or ""
    try:
        with (
            DocxPackage.open(baseline, read_only=True) as base_pkg,
            DocxPackage.open(candidate, read_only=True) as cand_pkg,
        ):
            result = compare(
                base_pkg,
                cand_pkg,
                mode=cast(DiffMode, resolved),
                baseline_ref=base_ref,
                candidate_ref=cand_ref,
            )
    except _BROKEN_PACKAGE as exc:
        raise EngineError(
            ErrorCode.PACKAGE_INCOMPLETE,
            "两份产物中有一份不是可解析的 docx，无法比对",
            where={
                "baseline_ref": base_ref,
                "candidate_ref": cand_ref,
                "reason": type(exc).__name__,
            },
            remediation=["确认这两份都是本系统产出的 docx", "或重新产出坏掉的那一份"],
        ) from exc
    return result.snapshot


def _select_rules(rule_set_ids: Sequence[str] | None) -> list[RuleSpec]:
    """按规则集标识选出规则；留空即全部内置（与 ``qa_stage`` 的取法同源）。

    规则集标识不存在时 ``LoadedRules.rule_set`` 会抛 ``E_PROFILE_RULESET_MISSING``——
    那是对的：作业失败比"少跑一套规则、报告照写通过"诚实。
    """
    loaded = default_catalog()
    ids = sorted(rule_set_ids) if rule_set_ids else sorted(loaded.files)
    catalog = loaded.catalog()
    selected: dict[str, RuleSpec] = {}
    for rule_set_id in ids:
        loaded.rule_set(rule_set_id)
        for spec in rules_for(RuleSet(rule_set_id=rule_set_id), catalog):
            selected.setdefault(spec.rule_id, spec)
    return [selected[rule_id] for rule_id in sorted(selected)]
