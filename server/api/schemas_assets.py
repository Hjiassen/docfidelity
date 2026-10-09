"""资产类端点的请求 / 响应模型：模板库详情、文档类型档案、事实源。

为什么另起一个模块而不是往 ``schemas`` 里加
------------------------------------------
``server/api/schemas.py`` 装的是**主流程**（项目 / 配置 / 上传 / 作业 / 裁决 / 模板入库
流水线）的形状；本模块装的是**库与资产的治理**（看详情、比版本、废弃、导出、来源声明）。
两者的读者不同——前者是"发起构建的人"，后者是"维护资产的人"——而合在一起已经越过
NFR-09 的 800 行上限。分界线按职责取，不是按行数切。

``_Body`` 从 ``schemas`` 借来用
------------------------------
"请求体未知字段一律拒绝"这条纪律只该有一份实现（``schemas._Body``）。复制一份
``model_config`` 的话，某天给主流程加上"允许额外字段"就会与本模块分叉，而
"多传了字段却没人报错"正是版本不匹配最难查的那种症状。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from docfidelity.contracts.domain import DomainModel
from docfidelity.contracts.errors import EngineErrorInfo
from docfidelity.contracts.fact import AssetRef, Fact, KindBinding, SourceConfig
from docfidelity.contracts.profile import DocTypeProfile
from docfidelity.contracts.template import TemplatePackage
from docfidelity.pipeline import SectionChange, SourceItems, VersionDiff

from ..domain_models import DomainModelRegistration
from ..profiles import BundledTemplate, ProfilePackageImport, ProfileRegistration
from ..sources import SourceProbeResult
from ..storage import FactSourceRecord, RegisteredDomainModel, RegisteredProfile
from .schemas import ContractSummaryOut, RegisteredTemplateOut, _Body

__all__ = [
    "BundledTemplateOut",
    "DomainModelDeprecateRequest",
    "DomainModelLibraryResponse",
    "DomainModelRegisterRequest",
    "DomainModelRegistrationOut",
    "ProfileDeprecateRequest",
    "ProfileDetailOut",
    "ProfileImportOut",
    "ProfileLibraryResponse",
    "ProfileRegisterRequest",
    "ProfileRegistrationOut",
    "RegisteredDomainModelOut",
    "RegisteredProfileOut",
    "SourceCreateRequest",
    "SourceCreateResponse",
    "SourceItemsOut",
    "SourceListResponse",
    "SourceOut",
    "SourceProbeOut",
    "TemplateDeprecateRequest",
    "TemplateDetailOut",
    "TemplateDiffOut",
]


# ===================================================================== 档案
class RegisteredProfileOut(BaseModel):
    """档案库里的一条登记（``GET /profiles`` 的一行）。

    **不含 ``profile_dir``**：落点不外泄（同 ``RegisteredTemplateOut`` 对
    ``package_dir`` 的处理）。``ref`` = ``profile_id@version``，正是配置里
    ``DocumentEntry.profile_id`` 的引用形式。
    """

    profile_id: str
    version: str
    ref: str
    doc_type: str
    title: str
    fingerprint: str
    registered_at: datetime
    registered_by: str | None = None
    deprecated: bool = False
    deprecated_reason: str | None = None

    @classmethod
    def of(cls, item: RegisteredProfile) -> RegisteredProfileOut:
        return cls(
            profile_id=item.profile_id,
            version=item.version,
            ref=item.ref,
            doc_type=item.doc_type,
            title=item.title,
            fingerprint=item.fingerprint,
            registered_at=item.registered_at,
            registered_by=item.registered_by,
            deprecated=item.deprecated,
            deprecated_reason=item.deprecated_reason,
        )


class ProfileLibraryResponse(BaseModel):
    """档案库列表。``count`` 由服务端给出（NFR-22）。"""

    items: list[RegisteredProfileOut] = Field(default_factory=list)
    count: int = 0


class ProfileRegisterRequest(_Body):
    """登记一个档案版本。

    ``profile`` 直接是 :class:`~docfidelity.contracts.profile.DocTypeProfile`——
    不另造一份同构的输入模型：档案就是那份契约，多一层映射只会让"档案里有哪些字段"
    有两个答案。``version`` 不在档案里（档案没有文档内版本号可解析，见
    ``server.profiles`` 的模块文档），故单独给。
    """

    profile: DocTypeProfile
    version: str = Field(default="1.0", max_length=64, description="档案版本号")
    author: str | None = Field(default=None, max_length=200, description="登记人；只落留痕")
    on_conflict: str = Field(
        default="abort", description="同 id@version 已存在且内容不同时：abort / overwrite"
    )


class ProfileRegistrationOut(BaseModel):
    """登记回执。

    ``skipped`` **必须显示**：它列出本次**做不了**的校验。生产路径下（领域模型库与
    模板库都给全）它应当是**空**的——空列表才是"全部查过了"；非空只意味着一件事，
    调用方拿不到库。

    ``findings`` 与它**不是一回事**：那是"查了、结论是小毛病"（如槽位声明的单元类型
    越界）。界面要把两句话分开说，合成一句"校验有问题"会让使用者不知道下一步是**补库**
    还是**改档案**（硬规则 24 的同一口径）。
    """

    profile_id: str
    version: str
    ref: str
    doc_type: str
    title: str
    fingerprint: str
    registered_at: datetime
    registered_by: str | None = None
    replaced: bool = False
    skipped: list[str] = Field(default_factory=list)
    findings: list[str] = Field(default_factory=list)

    @classmethod
    def of(cls, item: ProfileRegistration) -> ProfileRegistrationOut:
        return cls(
            profile_id=item.profile_id,
            version=item.version,
            ref=item.ref,
            doc_type=item.doc_type,
            title=item.title,
            fingerprint=item.fingerprint,
            registered_at=item.registered_at,
            registered_by=item.registered_by,
            replaced=item.replaced,
            skipped=list(item.skipped),
            findings=list(item.findings),
        )


class BundledTemplateOut(BaseModel):
    """档案包里**随包带来并被登记**的一个模板包（验收 45 的回执的一行）。"""

    ref: str
    package_hash: str
    #: 库里本来就有这个版本（``True``）还是这次新登记进来的（``False``）。
    #: 界面据此说"覆盖了一个已有的模板包"，而不是把覆盖说成新增。
    replaced: bool = False

    @classmethod
    def of(cls, item: BundledTemplate) -> BundledTemplateOut:
        return cls(ref=item.ref, package_hash=item.package_hash, replaced=item.replaced)


class ProfileImportOut(BaseModel):
    """导入档案包的回执（``POST /profiles/import``）。

    两件事分开报，因为它们**可以一件成一件不成**——模板登记成功而档案本体因版本冲突
    被拒时，库里确实多了模板包。合成一句"导入成功"会让那次冲突消失得无影无踪。
    """

    profile: ProfileRegistrationOut
    templates: list[BundledTemplateOut] = Field(default_factory=list)
    #: 包里带了、但这份档案**并没有引用**的模板包。如实列出而不是静默忽略：
    #: 多出来的东西要么是发件方包打错了，要么是两边版本对不上，两种都要人看一眼。
    unused_templates: list[str] = Field(default_factory=list)

    @classmethod
    def of(cls, item: ProfilePackageImport) -> ProfileImportOut:
        return cls(
            profile=ProfileRegistrationOut.of(item.registration),
            templates=[BundledTemplateOut.of(entry) for entry in item.templates],
            unused_templates=list(item.unused_templates),
        )


class ProfileDetailOut(BaseModel):
    """一个档案版本的详情（P04 式的"打开这份档案"）。

    ``document`` 是磁盘上那份原件读回来的**完整契约**（含已算好的 ``fingerprint``），
    前端不再自己拼字段——硬规则 17 的另一半：不显示契约里没有的东西，
    反过来也不该漏掉契约里有的东西。
    """

    entry: RegisteredProfileOut
    document: DocTypeProfile
    versions: list[RegisteredProfileOut] = Field(default_factory=list)


class ProfileDeprecateRequest(_Body):
    reason: str = Field(min_length=1, max_length=2000, description="废弃理由（必填）")
    operator: str | None = Field(default=None, max_length=200, description="办理人；只落留痕")


# ============================================================= 模板库（后半）
class TemplateDetailOut(BaseModel):
    """一个模板版本的详情（P04 的八个页签全部取自这里）。

    ``package`` 为 ``None`` 表示这次登记**只有原件**（解析没成功）。此时 ``integrity``
    与 ``fingerprint_problems`` 都是空列表——界面必须按"未解析"渲染，而**不是**把空
    列表画成一张全绿的检查表：那正是"缺能力跳过伪装成校验通过"的形状。

    ``summary`` 与 ``package`` 同进同出：它是从契约里数出来的摘要卡（P04 的"概要"页签），
    没有包就没有可数的东西。
    """

    entry: RegisteredTemplateOut
    parsed: bool = False
    package: TemplatePackage | None = None
    summary: ContractSummaryOut | None = None
    versions: list[RegisteredTemplateOut] = Field(default_factory=list)
    #: ``verify_package`` 的问题清单（包内容与清单是否一致）。
    integrity: list[str] = Field(default_factory=list)
    #: ``fingerprint_problems`` 的问题清单（指纹与包内容是否一致）。
    fingerprint_problems: list[str] = Field(default_factory=list)


class TemplateDeprecateRequest(_Body):
    version: str = Field(min_length=1, max_length=64, description="要废弃的版本号")
    reason: str = Field(min_length=1, max_length=2000, description="废弃理由（必填）")
    operator: str | None = Field(default=None, max_length=200, description="办理人；只落留痕")


class FieldDeltaOut(BaseModel):
    """一条字段级变更。``description`` 由内核生成，界面直接显示。"""

    path: str
    section: str
    category: str
    kind: str
    before: str | None = None
    after: str | None = None
    description: str = ""


class SectionChangeOut(BaseModel):
    section: str
    impact: str
    blocking: bool
    changed: bool
    changes: list[FieldDeltaOut] = Field(default_factory=list)

    @classmethod
    def of(cls, item: SectionChange) -> SectionChangeOut:
        return cls(
            section=item.section,
            impact=item.impact,
            blocking=item.blocking,
            changed=item.changed,
            changes=[
                FieldDeltaOut(
                    path=change.path,
                    section=change.section,
                    category=change.category,
                    kind=change.kind,
                    before=change.before,
                    after=change.after,
                    description=change.description,
                )
                for change in item.changes
            ],
        )


class TemplateDiffOut(BaseModel):
    """两个模板版本之间的差异（P04 的"版本对比"）。

    ``blocking_sections`` 与 ``changed_sections`` 由服务端算好，界面不做筛选
    （NFR-22：判定与统计都在服务端，"界面不做业务判断"）。
    """

    before_ref: str
    after_ref: str
    identical: bool
    ok: bool
    summary: str
    changelog: list[str] = Field(default_factory=list)
    sections: list[SectionChangeOut] = Field(default_factory=list)
    blocking_sections: list[str] = Field(default_factory=list)
    changed_sections: list[str] = Field(default_factory=list)

    @classmethod
    def of(cls, item: VersionDiff) -> TemplateDiffOut:
        return cls(
            before_ref=item.before_ref,
            after_ref=item.after_ref,
            identical=item.identical,
            ok=item.ok,
            summary=item.summary,
            changelog=list(item.changelog),
            sections=[SectionChangeOut.of(section) for section in item.sections],
            blocking_sections=item.blocking_sections,
            changed_sections=item.changed_sections,
        )


# ================================================================= 事实源
class SourceOut(BaseModel):
    """一条来源声明（``GET /projects/{pid}/sources`` 的一行）。

    ``last_probe_ok`` 的 ``None`` 与 ``False`` 是**两件事**：前者"从没探过"、
    后者"探了、没通"，界面据此给不同的下一步动作（去探测 / 去看为什么不通）。
    故这里保留 ``bool | None`` 而不是折成 ``False``。
    """

    project_id: str
    source_id: str
    source_type: str
    title: str | None = None
    root: str | None = None
    enabled: bool = True
    priority: int = 0
    bindings: list[KindBinding] = Field(default_factory=list)
    options: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    created_by: str | None = None
    last_probed_at: datetime | None = None
    last_probe_ok: bool | None = None
    last_probe_detail: str | None = None
    item_count: int | None = None

    @classmethod
    def of(cls, record: FactSourceRecord) -> SourceOut:
        config = record.config
        return cls(
            project_id=record.project_id,
            source_id=record.source_id,
            source_type=record.source_type,
            title=config.title,
            root=config.root,
            enabled=config.enabled,
            priority=config.priority,
            bindings=list(config.bindings),
            options=dict(config.options),
            created_at=record.created_at,
            created_by=record.created_by,
            last_probed_at=record.last_probed_at,
            last_probe_ok=record.last_probe_ok,
            last_probe_detail=record.last_probe_detail,
            item_count=record.item_count,
        )


class SourceListResponse(BaseModel):
    """来源列表。``known_types`` 一并给出：界面据此渲染"可选接入器"下拉，
    不必再发一次请求、也不必自己维护一份枚举（注册表可由 entry point 扩充）。"""

    items: list[SourceOut] = Field(default_factory=list)
    count: int = 0
    known_types: list[str] = Field(default_factory=list)


class SourceCreateRequest(_Body):
    """新增一条来源声明。``config`` 就是那份 :class:`SourceConfig`（见档案侧的同类说明）。"""

    config: SourceConfig
    author: str | None = Field(default=None, max_length=200, description="新增人；只落留痕")


class SourceCreateResponse(BaseModel):
    """新增结果。

    ``created=False`` 表示**同 id 同配置已存在**（幂等，什么也没改）。界面据此显示
    "已存在，未改动"而不是"新增成功"——两者对使用者的下一步动作不一样。
    """

    source: SourceOut
    created: bool = False


class SourceProbeOut(BaseModel):
    """一次试接入的结果。``ok=False`` 也是**正常回答**（探针不抛错，见 ``server.sources``）。"""

    source_id: str
    ok: bool
    detail: str
    item_count: int | None = None
    probed_at: datetime
    warnings: list[str] = Field(default_factory=list)

    @classmethod
    def of(cls, item: SourceProbeResult) -> SourceProbeOut:
        return cls(
            source_id=item.source_id,
            ok=item.ok,
            detail=item.detail,
            item_count=item.item_count,
            probed_at=item.probed_at,
            warnings=list(item.warnings),
        )


class SourceItemsOut(BaseModel):
    """条目浏览器的一页。

    ``kinds`` 是按领域类型计的个数（服务端算好）；``truncated`` 如实标出"还有更多"——
    悄悄少给会让使用者以为来源里就这么多对象。
    """

    source_id: str
    source_type: str
    digest: str = ""
    total: int = 0
    truncated: bool = False
    kinds: dict[str, int] = Field(default_factory=dict)
    facts: list[Fact] = Field(default_factory=list)
    assets: list[AssetRef] = Field(default_factory=list)
    warnings: list[EngineErrorInfo] = Field(default_factory=list)

    @classmethod
    def of(cls, item: SourceItems) -> SourceItemsOut:
        return cls(
            source_id=item.source_id,
            source_type=item.source_type,
            digest=item.digest,
            total=item.total,
            truncated=item.truncated,
            kinds=item.kinds(),
            facts=list(item.facts),
            assets=list(item.assets),
            warnings=list(item.warnings),
        )


# =========================================================== 领域模型库
class RegisteredDomainModelOut(BaseModel):
    """领域模型库里的一条登记（``GET /domain-models`` 的一行）。

    ``model`` 是**整份声明**透出，而不是只给 ``kind`` / ``title``：界面要回答
    "这个类型有哪些字段、哪些参与比对、哪些构成追溯边"，那些结论只能从声明里读
    （硬规则 17 的另一半：契约里有的不该漏，契约里没有的不该编）。

    行形状与模板包 / 档案**刻意不同**：那两者是 ``id@version``，领域模型没有版本
    （见 ``storage.RegisteredDomainModel`` 的类文档），故这里没有 ``ref`` / ``version``。
    """

    kind: str
    title: str
    fingerprint: str
    registered_at: datetime
    registered_by: str | None = None
    deprecated: bool = False
    deprecated_reason: str | None = None
    #: 字段个数。由服务端数（NFR-22 的"界面不做判断"）：界面只负责显示。
    field_count: int = 0
    model: DomainModel

    @classmethod
    def of(cls, item: RegisteredDomainModel) -> RegisteredDomainModelOut:
        return cls(
            kind=item.kind,
            title=item.title,
            fingerprint=item.fingerprint,
            registered_at=item.registered_at,
            registered_by=item.registered_by,
            deprecated=item.deprecated,
            deprecated_reason=item.deprecated_reason,
            field_count=len(item.model.fields),
            model=item.model,
        )


class DomainModelLibraryResponse(BaseModel):
    """领域模型库列表。

    ``active_count`` 一并给出而不是让界面按 ``deprecated`` 自己数：它要回答的是
    "**有几个类型参与档案登记校验**"，而"哪些算数"是服务端判据
    （``Services.domain_declarations``）。界面自己数就有了第二处口径，
    而它漂移的表现很具体——登记档案被拒，界面却显示"库里有类型"。
    """

    items: list[RegisteredDomainModelOut] = Field(default_factory=list)
    count: int = 0
    active_count: int = 0


class DomainModelRegisterRequest(_Body):
    """登记一份领域模型声明。

    ``model`` 直接是 :class:`~docfidelity.contracts.domain.DomainModel`——不另造一份
    同构的输入模型（同 ``ProfileRegisterRequest.profile`` 的理由）。

    **没有 ``version`` 字段**，不是漏了：领域模型的身份就是 ``kind``
    （见 ``storage.RegisteredDomainModel``）。
    """

    model: DomainModel
    author: str | None = Field(default=None, max_length=200, description="登记人；只落留痕")
    on_conflict: str = Field(
        default="abort", description="同 kind 已存在且内容不同时：abort / overwrite"
    )


class DomainModelRegistrationOut(BaseModel):
    """登记回执。``replaced=True`` 表示这次**覆盖**了库里那份声明（内容不同）。"""

    kind: str
    title: str
    fingerprint: str
    registered_at: datetime
    registered_by: str | None = None
    replaced: bool = False

    @classmethod
    def of(cls, item: DomainModelRegistration) -> DomainModelRegistrationOut:
        return cls(
            kind=item.kind,
            title=item.title,
            fingerprint=item.fingerprint,
            registered_at=item.registered_at,
            registered_by=item.registered_by,
            replaced=item.replaced,
        )


class DomainModelDeprecateRequest(_Body):
    reason: str = Field(min_length=1, max_length=2000, description="废弃理由（必填）")
    operator: str | None = Field(default=None, max_length=200, description="办理人；只落留痕")
