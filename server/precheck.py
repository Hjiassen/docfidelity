"""构建前置检查（DD-13.2 的 ``POST /projects/{pid}/config/validate``；P08 的"为什么不能发起"）。

**唯一实现**
------------
``Services.submit_build`` 与 ``POST /config/validate`` 必须给出同一份判据：前者据此
拒绝，后者据此渲染"不能发起的原因"。两处各写一份的症状很具体——界面说"可以发起"
而提交被拒（P08 页面原先那份 `missingRefs()` 就是一处已经存在的重复实现），或者
反过来：界面把按钮置灰，而服务端其实会放行。

故本模块是那份唯一实现，且**只收集、不抛错**。"拒绝"留在调用方（``submit_build``
把问题清单转成 ``E_PRECHECK_FAILED``）：同一个检查还要回答"只差哪一项"，
把它写成"遇错即抛"就没法回答了。

**做不了的检查要显式跳过，不能静默**
------------------------------------
需求给 P08 列的检查项是"配置必填 / 模板包存在 / 事实源非空"。三条的**引用是否已声明**
在这里做；**引用指向的东西是否真的在库里**由 :class:`ReferenceIndex` 带来
（``Services`` 从仓储读好，以纯参传入——本模块不认识仓储）。

剩下一条永远做不了：**"事实源非空"**。它不是查引用，是"这份来源到底产出了几条事实"，
而那要真跑一次接入（``ingest`` 的 I/O）。跳过项进 :attr:`PrecheckReport.skipped`，
界面照实标注（与门禁的"缺能力跳过必须进 summary"同一口径）。把它写成"检查通过"
是最坏的一种：使用者以为系统已经确认过，直到构建跑到 ingest 才失败。

调用方**没给** :class:`ReferenceIndex` 时（``refs=None``），三条引用检查一并进
``skipped``。**不给**与"给了但是空的"必须区分：前者是"这次没能力查"，后者是
"查了，一个都没登记"。混同会让缺能力伪装成校验通过（硬规则 11）——
而在这个具体场景里，表现是"预检说可以发起、构建跑到取模板包时才报缺"。

**问题清单为什么要沿用既有错误载荷的形状**
------------------------------------------
``where`` 里的 ``missing`` / ``problems`` / ``missing_stages`` 不是给日志看的，
是**既有的错误契约**（接口测试逐字断言过）。检查结果与错误载荷描述的是同一件事，
分叉出第二套字段名只会让"接口说缺 A、检查说缺 B"。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final, Literal

from docfidelity.contracts.config import DocumentEntry
from docfidelity.contracts.errors import EngineError, ErrorCode

__all__ = [
    "PRECHECK_RULE_ID",
    "UNAVAILABLE_CHECKS",
    "UNAVAILABLE_WITHOUT_REFS",
    "PrecheckIssue",
    "PrecheckReport",
    "ReferenceIndex",
    "collect",
]

#: 需求条目：FR-M7-15 的发动前置 + P08「无法发起的原因必须显式给出」。
PRECHECK_RULE_ID: Final = "FR-M7-15"

PrecheckKind = Literal[
    "unknown_doc_keys",
    "no_documents",
    "missing_references",
    "unregistered_references",
    "missing_stages",
]

#: **永远**做不了的检查项。列在这里而不是省略：省略会让"没查"
#: 与"查过没问题"在响应里长得一样。
UNAVAILABLE_CHECKS: Final[tuple[str, ...]] = (
    "事实源是否非空（登记已落地；「产出 0 条事实」要真跑一次接入才能判定，本检查只校验引用是否已登记）",
)

#: 调用方**没给** :class:`ReferenceIndex` 时额外做不了的检查项（见 :func:`collect`）。
#:
#: 与 :data:`UNAVAILABLE_CHECKS` 分开而不是合并：前者是能力边界（永远），
#: 后者是调用方漏传（可以修）。合成一个常量之后，"为什么这一条在跳过里"
#: 就再也答不出是哪一类原因。
UNAVAILABLE_WITHOUT_REFS: Final[tuple[str, ...]] = (
    "模板包引用是否均已入库（未提供模板库登记）",
    "档案引用是否均已登记（未提供档案库登记）",
    "事实源引用是否均已登记（未提供事实源登记）",
)


@dataclass(frozen=True)
class ReferenceIndex:
    """已登记引用的快照（``Services`` 从仓储读好后以**纯参**传入）。

    三个集合的**粒度不同**，这不是笔误——而是契约本身如此：

    * ``templates``：``"<template_id>@<version>"``。``DocumentEntry.template_ref``
      指的是具体一版（改版本号就是改输入），故必须精确匹配；
    * ``profiles``：可解析的配置原引用，支持裸 ``profile_id`` 与
      ``profile_id@version``；调用方与构建冻结共用同一版本解析策略；
    * ``sources``：``source_id``，项目作用域内匹配。

    空集合是**有意义的结论**（"查了，库里一个都没有"），故它与"没给索引"（``None``）
    严格区分。
    """

    templates: frozenset[str] = frozenset()
    profiles: frozenset[str] = frozenset()
    sources: frozenset[str] = frozenset()


@dataclass(frozen=True)
class PrecheckIssue:
    """一项"不能发起"的原因。"""

    kind: PrecheckKind
    message: str
    remediation: tuple[str, ...]
    where: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PrecheckReport:
    """前置检查结果。**空 ``issues`` 就是"可以发起"**，没有第三种状态。"""

    project_id: str
    #: 这次检查针对的文档键（界面上"要发起哪些"的确定答案）。
    doc_keys: tuple[str, ...] = ()
    issues: tuple[PrecheckIssue, ...] = ()
    #: 实际检查了几份文档。
    checked_documents: int = 0
    registered_stages: tuple[str, ...] = ()
    missing_stages: tuple[str, ...] = ()
    #: 因能力未落地而**跳过**的检查项（见模块文档）。
    skipped: tuple[str, ...] = field(default_factory=lambda: UNAVAILABLE_CHECKS)

    @property
    def ok(self) -> bool:
        return not self.issues

    def as_error(self) -> EngineError:
        """转成 ``E_PRECHECK_FAILED``。

        多条问题时 ``message`` 取**第一条**（收集顺序 = 从"最靠前的阻塞"到"最靠后"），
        其余条只进 ``where``：把 N 条拼成一句话会让界面上的主提示变成一段散文，
        而使用者真正需要的是"先解决这一个"。条数在 ``where`` 里给足，界面可展开。
        """
        first = self.issues[0]
        message = first.message
        if len(self.issues) > 1:
            message = f"{message}（另有 {len(self.issues) - 1} 项）"
        # 合并各问题的 ``where`` 片段（``missing`` / ``problems`` / ``missing_stages`` …）
        # ——它们是**既有错误契约**，形状必须原样保留（接口测试逐字断言过）。
        # 问题**分组**另开 ``issues`` 键，而不是复用 ``problems``：后者是
        # ``missing_references`` 自己的逐字段清单，两者粒度不同。
        merged: dict[str, Any] = {"project_id": self.project_id}
        for issue in self.issues:
            merged.update(issue.where)
        merged["issues"] = [
            {"kind": issue.kind, "message": issue.message, **dict(issue.where)}
            for issue in self.issues
        ]
        return EngineError(
            ErrorCode.PRECHECK_FAILED,
            message,
            where=merged,
            remediation=_merged_remediation(self.issues),
        )


def _merged_remediation(issues: Sequence[PrecheckIssue]) -> list[str]:
    """去重但**保序**：后出现的问题不该把先出现者的首要动作挤到后面。"""
    seen: set[str] = set()
    ordered: list[str] = []
    for issue in issues:
        for item in issue.remediation:
            if item not in seen:
                seen.add(item)
                ordered.append(item)
    return ordered


def collect(
    *,
    project_id: str,
    entries: Sequence[DocumentEntry],
    selected: Sequence[str] | None,
    registered_stages: Sequence[str],
    missing_stages: Sequence[str],
    refs: ReferenceIndex | None = None,
) -> PrecheckReport:
    """收集全部问题（**不抛错**）。

    Parameters
    ----------
    entries:
        配置里**启用的**文档条目。
    selected:
        本次要构建的文档键；``None`` = 全部启用文档。
    registered_stages / missing_stages:
        阶段实现的就绪情况（``StageRegistry`` 给的）。
    refs:
        已登记引用的快照。``None`` = 调用方拿不到（三条引用检查进 ``skipped``）；
        给了（哪怕是空集）= 真查。见 :class:`ReferenceIndex`。

    检查顺序刻意固定为"文档 → 引用 → 阶段"：``as_error`` 拿第一条当主提示，而
    "这份文档没配模板"比"这台服务器没装阶段"更接近使用者能立刻动手的那件事。
    同类里再按"没声明 → 声明了但库里没有"排：先把配置补全，再去补库里的资产。
    """
    issues: list[PrecheckIssue] = []
    chosen = list(entries)
    if selected is not None:
        wanted = set(selected)
        chosen = [entry for entry in entries if entry.doc_key in wanted]
        missing_keys = sorted(wanted - {entry.doc_key for entry in entries})
        if missing_keys:
            issues.append(
                PrecheckIssue(
                    kind="unknown_doc_keys",
                    message=f"配置里没有启用的文档：{missing_keys}",
                    where={"missing": missing_keys},
                    remediation=(
                        "检查 doc_key 拼写",
                        "或先在该项目的配置里启用这些文档",
                    ),
                )
            )

    if not chosen:
        issues.append(
            PrecheckIssue(
                kind="no_documents",
                message="项目没有可构建的文档",
                where={"enabled_documents": []},
                remediation=("在项目配置里登记至少一份启用的文档",),
            )
        )
    else:
        problems = _reference_problems(chosen)
        if problems:
            issues.append(
                PrecheckIssue(
                    kind="missing_references",
                    message="配置缺少必填引用，无法发起构建",
                    where={"problems": problems},
                    remediation=(
                        "在项目配置里为这些文档选择模板包与档案",
                        "或先把模板入库（POST /api/v1/templates/imports）",
                    ),
                )
            )
        if refs is not None:
            unregistered = _unregistered_problems(chosen, refs=refs)
            if unregistered:
                issues.append(
                    PrecheckIssue(
                        kind="unregistered_references",
                        message=f"配置引用了尚未登记的资产（{len(unregistered)} 项）",
                        # 用 ``unregistered`` 而不是复用一个 ``problems`` 键：``as_error``
                        # 合并各问题的 ``where`` 是 ``dict.update``，两个同键问题会让先
                        # 出现的那一份被后一份**盖掉**。分键之后每条问题都留得住。
                        where={"unregistered": unregistered},
                        remediation=_unregistered_remediation(unregistered),
                    )
                )

    if missing_stages:
        issues.append(
            PrecheckIssue(
                kind="missing_stages",
                message=f"流水线阶段未注册，无法发起构建：{sorted(missing_stages)}",
                where={
                    "missing_stages": list(missing_stages),
                    "registered_stages": list(registered_stages),
                },
                remediation=(
                    "在应用启动时注册阶段体：显式传入 StageRegistry，",
                    "或安装提供 docfidelity.build_stages 入口点的内核构建包",
                ),
            )
        )

    skipped = (
        UNAVAILABLE_CHECKS if refs is not None else UNAVAILABLE_CHECKS + UNAVAILABLE_WITHOUT_REFS
    )
    return PrecheckReport(
        project_id=project_id,
        doc_keys=tuple(entry.doc_key for entry in chosen),
        issues=tuple(issues),
        checked_documents=len(chosen),
        registered_stages=tuple(registered_stages),
        missing_stages=tuple(missing_stages),
        skipped=skipped,
    )


def _reference_problems(entries: Sequence[DocumentEntry]) -> list[dict[str, str]]:
    """逐份文档检查必填引用（缺一项就报出**是哪一份**的哪一项）。

    判空用 ``not entry.template_ref`` 而不是 ``== ""``：契约允许 ``None``，
    而"没选模板"与"选了个空字符串"是同一种缺陷——界面的判空口径必须与这里一致
    （``if not x`` 把空串也算缺），否则会出现"界面说没问题、提交却被拒"。
    """
    problems: list[dict[str, str]] = []
    for entry in entries:
        if not entry.template_ref:
            problems.append({"doc_key": entry.doc_key, "field": "template_ref"})
        if not entry.profile_id:
            problems.append({"doc_key": entry.doc_key, "field": "profile_id"})
    return problems


def _unregistered_problems(
    entries: Sequence[DocumentEntry], *, refs: ReferenceIndex
) -> list[dict[str, str]]:
    """逐份文档检查"引用的东西在不在库里"。

    与 :func:`_reference_problems` 的分工：那边管"**没填**"，这边管"**填了、库里没有**"。
    两条的修复动作不同（前者去补配置，后者去补库），所以必须分成两个 kind——
    合成一条"引用有问题"之后，界面就只能给出一句含糊的补救话术。

    判空用 ``if entry.template_ref``：空串在这里**跳过**（"没填"由另一条报），
    而不是被当成"引用了一个空标识"再报一次。
    """
    problems: list[dict[str, str]] = []
    for entry in entries:
        if entry.template_ref and entry.template_ref not in refs.templates:
            problems.append(
                {
                    "doc_key": entry.doc_key,
                    "field": "template_ref",
                    "ref": entry.template_ref,
                }
            )
        if entry.profile_id and entry.profile_id not in refs.profiles:
            problems.append(
                {"doc_key": entry.doc_key, "field": "profile_id", "ref": entry.profile_id}
            )
        for source_id in entry.source_ids:
            if source_id and source_id not in refs.sources:
                problems.append({"doc_key": entry.doc_key, "field": "source_ids", "ref": source_id})
    return problems


def _unregistered_remediation(problems: Sequence[Mapping[str, str]]) -> tuple[str, ...]:
    """按字段给出补救动作，顺序固定为 模板包 → 档案 → 事实源。

    只列**出现过**的字段：把三条动作全给出来，会让"我只缺一个事实源"的那一屏也出现
    "先把模板入库"——而界面上一句多余的补救话术，看起来就是一件待办。
    """
    actions = {
        "template_ref": (
            "把缺的模板包入库（P05 入库向导），或改配置里的 template_ref（template_id@version）"
        ),
        "profile_id": "在档案库登记这些档案（P03 → 文档类型档案），或改配置里的 profile_id",
        "source_ids": "在该项目登记这些事实源（P06 事实源），或改配置里的 source_ids",
    }
    present = {item.get("field", "") for item in problems}
    return tuple(
        actions[field] for field in ("template_ref", "profile_id", "source_ids") if field in present
    )
