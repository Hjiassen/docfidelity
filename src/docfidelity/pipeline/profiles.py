"""文档类型档案用例（DD-7.5）——``server`` 与 ``model`` / ``compose`` 之间的接缝。

为什么是接缝而不是"服务端自己校验"
----------------------------------
档案登记（``POST /profiles``）要做三件服务端自己做不了的事（DD-7.5）：

1. 把 ``projections[].for_each`` 的引用解析成领域类型，比对已登记的集合；
2. 比对 ``bindings.template_packages`` 与模板库；
3. 按 ``genre.allowed_unit_types`` 逐槽位检查单元类型。

第 1、3 条需要 ``DomainModelRegistry``，第 2 条需要模板库，都住在内核里；而 DD-13.1
禁止 ``server`` 直接 import 内核实现模块。于是与 :mod:`.templates` / :mod:`.sources`
对称，本模块是 ``pipeline`` 里**唯一**处理档案的模块。

第 1、3 条**转发**，不重新实现
------------------------------
"``for_each`` → 基础类型"的解析与检查有唯一实现：
:func:`docfidelity.model.validation.require_registered_kind`（登记期与求值期共用）。
本模块只把它包一层，理由是复制的代价不是多一份代码，而是**两套口径**——
"取第一个点之前的一段"这类细节抄一遍就会漂移，而漂移的表现是"登记说没问题、
求值期整章消失"（FR-M2-23 正是为堵这个而设）。

包一层的第二个作用是**把抛出改成结果**：核心实现是抛 ``E_DOMAIN_KIND_UNKNOWN``
（登记期拒绝），而服务端要一次拿到"本次校验的全部结论 + 哪些检查没做"，再自己决定
报哪个码。这与 :func:`docfidelity.pipeline.templates.import_template` 的姿态一致。

外部数据一律以**纯参**传入，不在这里查库
----------------------------------------
``known_template_refs`` 是"模板库现有引用"，住在**服务端**仓储里——本模块不认识它，
也刻意不去够（签名收 ``Sequence[str] | None``）。两条理由都与本项目反复踩的坑有关：

* 一旦这里 import 了仓储协议，``pipeline`` 就从"内核编排"变成"认识服务端存储"，
  分层立刻破口（``pipeline`` 之下没有任何服务端概念）；
* 把"没提供"与"提供了但是空的"混为一谈，会让"缺能力跳过"伪装成"校验通过"
  （硬规则 11）。所以两者用 ``None`` 与 ``[]`` **显式区分**：``None`` = 做不了、
  记进 :attr:`ProfileCheck.skipped`；``[]`` = 做得了、结论是"一个都没有"。

领域模型是**库资产**，不是项目配置的一部分
------------------------------------------
第 1 条校验（``for_each`` 引用的领域类型是否已登记）按 FR-M2-23 要在**登记期**就
拒绝。而登记一份档案是**全局**动作——``doc_type_profiles`` 的主键是
``(profile_id, version)``，没有 ``project_id``。于是"拿哪个项目的领域模型去查"这个问题
在登记期**没有答案**；把领域模型放回 ``ProjectConfig`` 只会让这条校验永远做不了。

需求 4.9.2 把三层外部数据（领域模型 / 模板包 / 文档类型档案）并列为**正交的**库资产，
"一个领域模型可被多份档案复用"——故领域模型与模板包、档案同级：**进库**，
以 :func:`domain_registry` 装成注册表后交给本模块。

``registry`` 仍可缺省，但缺省现在只有一个含义：**调用方拿不到库**
（脚本、单测、将来的 SDK）。生产路径（``server.profiles.register_profile``）一律传入
注册表——包括"库里一个类型都没登记"的那种情况，此时传的是**空注册表**而不是
``None``，第 1 条会如实失败。**跳过不等于通过**：真缺能力时结论会带到登记回执的
``skipped`` 里，界面必须显示"领域类型未校验"，而不是装作已经查过（硬规则 11）。

可满足性**不在这里重新实现**
----------------------------
DD-7.5 第 4 条（档案 × 模板包）的唯一实现是 :func:`docfidelity.compose.precheck.satisfiable`，
本模块只转发。那边的口径是"以 ``SlotSpec.locator`` 为准、不以 ``slot_id`` 为准"，
抄过来必然漂移，而漂移的表现是"预检说可满足、渲染期查不到槽位"。

反向引用也在这里，而不是由服务端扫列表
--------------------------------------
"哪些档案引用了 ``template_id@version``"（P04 概要页签）与正向的 ``bindings.template_packages``
是同一份数据的两个方向，判据必须同源。服务端自己扫一遍列表，就等于有了第二处
"引用的书写形式长什么样"的实现（最后一个 ``@`` 之前是编号？没有 ``@`` 时算什么？），
而它的漂移表现很具体：界面说"没人引用这一版"、构建却被 ``E_PROFILE_TEMPLATE_MISSING``
拦住。故 :func:`referencing_profiles` 与 :func:`validate_profile` 同住一个模块。

档案的**版本**为什么必须跟着传进来：见 :class:`RegisteredProfile`。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Final

from docfidelity.compose import satisfiable as _satisfiable
from docfidelity.contracts.domain import DomainModel
from docfidelity.contracts.errors import EngineError, EngineErrorInfo, ErrorCode
from docfidelity.contracts.profile import DocTypeProfile, ProfileFingerprint
from docfidelity.contracts.qa import Finding
from docfidelity.contracts.template import TemplatePackage
from docfidelity.model import PROFILE_RULE_ID as PROFILE_RULE_ID
from docfidelity.model import DomainModelRegistry
from docfidelity.model import validate_profile as _core_validate_profile
from docfidelity.platform.hashing import combine_digests, digest_payload

__all__ = [
    "PROFILE_RULE_ID",
    "PROFILE_SECTIONS",
    "ProfileCheck",
    "ProfileReference",
    "RegisteredProfile",
    "domain_registry",
    "fingerprint_domain_model",
    "fingerprint_profile",
    "referencing_profiles",
    "satisfiable",
    "validate_profile",
]

#: 参与档案指纹的分区。**不含 ``applies``**：那是人写的"适用判据说明"（三条判据的
#: 文字陈述），改它一句措辞不影响任何一份产出，但进了指纹就会让"改档案后要重跑哪些
#: 文档"（FR-M2-26）凭空多出一批待重跑项——而使用者只是把说明写清楚了一点。
#: 反过来，``bindings`` 必须进：``terminology_scope`` 影响术语替换（产出内容），
#: ``rule_sets`` 影响门禁结论（产出判定）。
PROFILE_SECTIONS: Final[tuple[str, ...]] = ("genre", "projections", "bindings")


@dataclass(frozen=True)
class ProfileCheck:
    """一次档案登记校验的结果（DD-7.5）。

    不折成一个 ``ok: bool`` 加一句消息：三件事各有各的"没通过"的形状，服务端要据此
    给出**不同的错误码**（``E_PROFILE_TEMPLATE_MISSING`` / ``E_DOMAIN_KIND_UNKNOWN``），
    界面要把"补登记模板"和"改档案"分成两个动作。
    """

    #: ``bindings.template_packages`` 里在模板库中不存在的引用。
    missing_templates: list[str] = field(default_factory=list)
    #: 领域类型 / 单元类型的问题（转发自 ``model.validation``）。**原样保留错误码**：
    #: 它已经是 ``E_DOMAIN_KIND_UNKNOWN``，换个码会让"该去哪里补"这个答案变形。
    kind_error: EngineErrorInfo | None = None
    #: 领域类型没问题的那些结论（如槽位单元类型越界）。
    findings: list[Finding] = field(default_factory=list)
    #: 本次**做不了**的检查（调用方没提供对应外部数据）。必须回执给使用者，不得算作通过。
    skipped: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not (self.missing_templates or self.kind_error or self.findings)

    def problems(self) -> list[str]:
        """人类可读的问题清单，按"补模板 → 补领域类型 → 改骨架"的顺序排列。

        顺序即修复顺序：先补模板包（别人的资产），再补领域类型声明（可能是别人漏登），
        最后调单元类型约束（自己写的）。倒过来列，使用者会先改完骨架才发现模板也没登记。
        """
        problems = [f"模板包引用未登记：{ref}" for ref in self.missing_templates]
        if self.kind_error is not None:
            problems.append(self.kind_error.message)
        problems.extend(finding.message for finding in self.findings)
        return problems

    def error_infos(self) -> list[EngineErrorInfo]:
        """全部可上报的错误快照，供服务端挑一个当响应正文。"""
        infos: list[EngineErrorInfo] = []
        if self.missing_templates:
            infos.append(_missing_templates_info(self.missing_templates))
        if self.kind_error is not None:
            infos.append(self.kind_error)
        return infos


def validate_profile(
    profile: DocTypeProfile,
    *,
    registry: DomainModelRegistry | None = None,
    known_template_refs: Sequence[str] | None = None,
) -> ProfileCheck:
    """DD-7.5 的登记校验。**不抛异常**：结论以返回值给出，抛不抛由调用方决定。

    不抛的理由与 :func:`docfidelity.pipeline.templates.import_template` 一致——本层
    不做 HTTP 状态决策，而"缺模板引用"该报 422 还是该降级成告警，取决于调用它的
    是登记端点还是构建前预检。同一个校验函数被两处调用时，抛异常的那一版会逼着
    预检去 ``try`` 一次。

    Parameters
    ----------
    registry:
        领域模型注册表，由 :func:`domain_registry` 从库里的声明装出来。``None`` 表示
        调用方拿不到库——第 1、3 条进 :attr:`ProfileCheck.skipped`。
        **"库里一个类型都没登记"不是 ``None``**：那是空注册表，第 1 条会如实失败。
    known_template_refs:
        模板库现有引用（``"<template_id>@<version>"``）。``None`` 同上。
    """
    skipped: list[str] = []
    kind_error: EngineErrorInfo | None = None
    findings: list[Finding] = []

    if registry is None:
        skipped.append("projections[].for_each 引用的领域类型是否均已登记（未提供领域模型注册表）")
        skipped.append("槽位 unit_types 是否都在 genre.allowed_unit_types 内（同上，同一次校验）")
    else:
        try:
            findings = _core_validate_profile(profile, registry)
        except EngineError as error:
            # 核心实现是"未登记即拒绝"，在这里变成一条结论而不是一次中断：
            # 服务端要用它拼错误码与 ``where``，也要把它写进登记回执。
            kind_error = EngineErrorInfo.from_error(error)

    missing_templates: list[str] = []
    if known_template_refs is None:
        skipped.append("bindings.template_packages 是否均已登记（未提供模板库内容）")
    else:
        known_refs = set(known_template_refs)
        missing_templates = [
            ref for ref in profile.required_template_refs() if ref not in known_refs
        ]

    return ProfileCheck(
        missing_templates=missing_templates,
        kind_error=kind_error,
        findings=findings,
        skipped=skipped,
    )


def fingerprint_profile(profile: DocTypeProfile) -> ProfileFingerprint:
    """档案指纹：**分区**摘要，供"改档案后需重跑哪些文档"的漂移定位（FR-M2-26）。

    分区而不是整体摘要，是因为档案的修正往往是"只动了投影规则"这一种。整体摘要
    只能回答"变了没有"，分区摘要能回答"变的是哪一块"，而 FR-M2-26 要的是后者
    （受影响的历史产出与章节要列得出来）。

    口径与模板指纹一致（``combine_digests``）：整体摘要由分区摘要合成，
    与分区顺序无关，故新增一个分区不会让所有档案摘要全部变化。
    """
    payload = profile.model_dump(mode="json")
    sections = {name: digest_payload(payload.get(name)) for name in PROFILE_SECTIONS}
    return ProfileFingerprint(digest=combine_digests(sections), sections=sections)


def fingerprint_domain_model(model: DomainModel) -> str:
    """领域模型声明的稳定摘要——登记期"按内容判是否重复"的判据。

    与档案指纹（:func:`fingerprint_profile`）的差别在**要不要分区**：档案会以"只动了
    投影规则"这一种方式被修正，分区摘要才能回答"变的是哪一块"（FR-M2-26）；领域模型
    整份就是一条声明，动任何一处都等于**换了一份声明**——没有一处需要单独指认。

    稳定性来自 ``digest_payload`` 的 ``sort_keys=True``（``platform.hashing``）：
    字段的书写顺序不影响摘要，否则同一份声明换个字段顺序就会被判成"内容变了"，
    而表现是"同一份领域模型重复登记报冲突"。
    """
    return digest_payload(model.model_dump(mode="json"))


def domain_registry(models: Sequence[DomainModel]) -> DomainModelRegistry:
    """把一组已登记的领域模型声明装成注册表（DD-6.1）。

    为什么必须经由本模块：``server`` 不能 import ``model``（DD-13.1），而"领域类型
    是否已登记"这条判据只有 :class:`~docfidelity.model.DomainModelRegistry` 能回答。
    本模块是 ``pipeline`` 里唯一认识 ``model`` 的地方，故它在这里——不新开第五条缝
    （四条缝见 ``pipeline/__init__`` 的模块文档）。

    唯一实现仍是 ``DomainModelRegistry.register``：同 ``kind`` 出现两份不一致的声明
    会抛 ``E_CONFIG_INVALID``，此处**不重判**——重判就有了第二个"什么算不一致"的口径。

    Parameters
    ----------
    models:
        声明本身。调用方（``server``）从库里读好、滤掉废弃的，再以**纯参**传入：
        本层不认识仓储，也不去够它——与 ``known_template_refs`` 同一姿态。
        **空序列是有意义的结论**（"一个领域类型都没登记"），不是"没提供"。
    """
    return DomainModelRegistry(models)


def satisfiable(profile: DocTypeProfile, package: TemplatePackage) -> list[Finding]:
    """档案 × 模板包的可满足性（DD-7.5 第 4 条）。

    唯一实现在 :func:`docfidelity.compose.precheck.satisfiable`，此处只转发——
    服务端要能触发这条检查，又不能 import ``compose``。返回空列表表示可满足。
    """
    return _satisfiable(profile, package)


@dataclass(frozen=True)
class RegisteredProfile:
    """一份**已登记**的档案：契约本体 + 登记版本。

    版本为什么不在契约里：``DocTypeProfile`` 是人写的一份 JSON，**没有文档内版本号**
    可解析（模板包才可以从原件里读出版本）。版本由登记处（服务端仓储）给出，
    所以查询反向引用时它必须随契约一起传进来——不能指望从契约里取。
    """

    version: str
    profile: DocTypeProfile
    #: 该版本是否已被废弃标记。**只标不删**：废弃的档案仍在引用模板包，
    #: 故它必须出现在反向引用结果里（"这一版能不能废弃"要看的是引用，
    #: 不是被引用者自己是否已废弃）。
    deprecated: bool = False


@dataclass(frozen=True)
class ProfileReference:
    """一条"某档案版本引用了某模板包"的记录。"""

    profile_id: str
    version: str
    doc_type: str
    title: str
    deprecated: bool = False
    #: 引用出现在档案的哪个位置。目前只有一处，但保留成元组：``applies.layout_source``
    #: 是人写的文字陈述（"版式来源：TPL-X @ 1.0"），哪天要把声明式引用也纳入，
    #: 加一个取值即可，界面与载荷形状都不用改。
    declared_in: tuple[str, ...] = ("bindings.template_packages",)


def referencing_profiles(
    registered: Sequence[RegisteredProfile],
    *,
    template_id: str,
    version: str | None = None,
) -> list[ProfileReference]:
    """模板包 → 引用它的档案（P04 概要页签的「引用该包的作业与文档」）。

    Parameters
    ----------
    registered:
        全部已登记档案（含已废弃版本）。服务端从仓储读好后以**纯参**传入——
        与 :func:`validate_profile` 的 ``known_template_refs`` 同一姿态：
        本模块不认识仓储，也不去够它。
    template_id:
        模板包编号。
    version:
        具体版本；``None`` = 该编号的**任意版本**。两种粒度都需要：
        "谁引用了这一个版本"决定"这一版能不能废弃"，"谁引用了这个编号"决定
        "整个编号改名要通知谁"。

    匹配规则与 :attr:`DocTypeProfile.bindings.template_packages` 的书写形式一致：
    引用形如 ``"<template_id>@<version>"``，按最后一个 ``@`` 拆。**没有 ``@`` 的引用**
    视为"只有编号、没有版本"，故它只被 ``version=None`` 的查询命中，不会被
    "某个具体版本"的查询命中——把这种写法算成"匹配任意版本"会让废弃判据凭空放宽。

    结果按 ``(profile_id, version)`` 排序：反向引用的用途是给人看的清单，
    顺序不稳定会让同一份数据两次打开长得不一样。
    """
    hits: list[ProfileReference] = []
    for item in registered:
        if not _reference_matches(item.profile, template_id=template_id, version=version):
            continue
        hits.append(
            ProfileReference(
                profile_id=item.profile.profile_id,
                version=item.version,
                doc_type=item.profile.doc_type,
                title=item.profile.title,
                deprecated=item.deprecated,
            )
        )
    hits.sort(key=lambda hit: (hit.profile_id, hit.version))
    return hits


def _reference_matches(profile: DocTypeProfile, *, template_id: str, version: str | None) -> bool:
    for ref in profile.bindings.template_packages:
        ident, _, declared_version = ref.rpartition("@")
        if not ident:
            # 没有 ``@``：整串就是编号，版本为空。
            ident, declared_version = ref, ""
        if ident != template_id:
            continue
        if version is None or declared_version == version:
            return True
    return False


def _missing_templates_info(refs: Sequence[str]) -> EngineErrorInfo:
    return EngineErrorInfo(
        code=ErrorCode.PROFILE_TEMPLATE_MISSING,
        message=f"档案引用了未登记的模板包：{'、'.join(refs)}",
        where={"missing": list(refs)},
        remediation=[
            "先在模板库登记这些模板包（P05），再回到本页重新提交档案",
            "或修正档案 bindings.template_packages 里的引用（template_id@version）",
        ],
    )
