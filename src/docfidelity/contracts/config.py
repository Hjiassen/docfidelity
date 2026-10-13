"""项目配置契约（DD-16）。

配置分层：``system``（部署级）< ``global``（应用级）< ``default``（内置）< 项目级。
合并规则见 DD-16.3：项目级覆盖全局，全局覆盖内置；列表型按标识合并而非整体替换。

两条界面纪律（NFR-22 / 需求 7.1.1）在契约上的体现：
1. **界面上每一个"能改的东西"都必须对应本文件里的一个字段**；
2. 界面不做业务判断——所有"该不该阻断""用哪个值"的结论都由服务端按本配置算出。
"""

from __future__ import annotations

from typing import Any, ClassVar, Literal

from pydantic import Field, model_validator

from .base import FormatModel

TerminologyScope = Literal["project", "global", "none"]

DecisionLevel = Literal["auto", "confirm", "manual"]
"""决策点分级（DD-5.2.3）：``auto`` 自动采用；``confirm`` 需人工确认；``manual`` 必须人工。"""

DecisionStrategy = Literal["rule_only", "rule_then_model", "model_then_rule"]

AdmissionPolicy = Literal["require_confidence", "independent_validation"]

FallbackAction = Literal["rule_default", "skip", "queue"]

DecisionRoute = Literal["rule", "small_model", "optional_large_model", "human"]
"""决策点归属（需求 5.2.3 的分级表 / 验收 41）。

取值只可能是这四档之一：``rule`` 规则、``small_model`` 小模型、``optional_large_model``
可选大模型、``human`` 人工。**归属必须是显式声明的**——"顺手让模型判断一下"是本项目
认定的最大不稳定源（需求第 13 章风险项），故它必须在配置里可被逐点查到。
"""


class TerminologyEntry(FormatModel):
    """术语条目。

    ``forbidden`` 是**禁用写法**列表；命中文档中出现禁用写法即触发术语一致规则
    （FR-M5-08）。空 ``forbidden`` 表示只做统一替换、不做禁用校验。
    """

    term: str = Field(description="术语标识")
    canonical: str | None = Field(default=None, description="规范写法；为空表示以 term 为规范")
    forbidden: list[str] = Field(default_factory=list, description="禁用写法")
    note: str | None = None

    @property
    def canonical_form(self) -> str:
        return self.canonical or self.term


class Terminology(FormatModel):
    """术语表。作用域由档案的 ``bindings.terminology_scope`` 决定。"""

    scope: TerminologyScope = "project"
    entries: list[TerminologyEntry] = Field(default_factory=list)

    def lookup(self, text: str) -> list[TerminologyEntry]:
        """返回在给定文本中命中的术语条目。"""
        hits: list[TerminologyEntry] = []
        for entry in self.entries:
            forms = [entry.canonical_form, *entry.forbidden]
            if any(form and form in text for form in forms):
                hits.append(entry)
        return hits


class PaginationPolicy(FormatModel):
    """分页策略。

    只声明**语义**（哪些内容必须成组），具体 OOXML 属性由渲染层设置
    （``keepNext`` / ``keepLines`` / ``cantSplit`` / 分页符）。
    """

    keep_heading_with_next: bool = Field(default=True, description="标题与后继段落同页")
    min_body_lines_after_heading: int = Field(
        default=2, ge=0, description="标题后至少保留的正文行数"
    )
    keep_table_header_row: bool = Field(default=True, description="跨页时表头行重复")
    avoid_row_split: bool = Field(default=True, description="单行不跨页断开")
    avoid_figure_orphan: bool = Field(default=True, description="图与图注不分离")
    page_break_before_chapter: bool = Field(default=False, description="每章另起一页")


class RuleSetRef(FormatModel):
    """规则集引用与项目级覆盖。"""

    rule_set_id: str
    included_rules: list[str] = Field(default_factory=list)
    excluded_rules: list[str] = Field(default_factory=list)
    severity_overrides: dict[str, str] = Field(default_factory=dict)
    param_overrides: dict[str, dict[str, Any]] = Field(default_factory=dict)


class DecisionPointRoute(FormatModel):
    """单个决策点的路由配置。

    ``route`` 为 ``None`` 表示"由引擎内置的默认路由表决定"——默认表逐点对应需求 5.2.3
    的分级表，故"未显式声明"不等于"没有归属"，但配置一旦写了就以配置为准。

    ``level`` / ``strategy`` / ``confidence_threshold`` 三个字段**同样以 ``None`` 表示
    "由归属决定"**。它们不能带非空默认值：配置项里最常写的只有 ``model``（换一个模型），
    若其余字段带默认值，那么"只换了模型标识"会顺带把 ``level`` 从 ``confirm`` 压回
    ``auto``、把 ``strategy`` 从 ``rule_only`` 换成 ``rule_then_model``——
    而这两处行为改变在配置文本里**完全看不出来**。
    """

    route: DecisionRoute | None = Field(
        default=None, description="归属：规则 / 小模型 / 可选大模型 / 人工；为空则由内置默认表决定"
    )
    level: DecisionLevel | None = Field(
        default=None,
        description="自动 / 需确认 / 需人工；为空则由归属决定（规则与小模型=自动，可选大模型=需确认，人工=需人工）",
    )
    strategy: DecisionStrategy | None = Field(
        default=None,
        description="采用策略；为空则由归属决定（规则与人工=纯规则，模型两档=先规则后模型）",
    )
    model: str | None = Field(default=None, description="模型标识；为空表示用服务端默认模型")
    confidence_threshold: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="低于此阈值不得自动采用，必须进裁决队列；为空用引擎默认 0.7",
    )
    admission_policy: AdmissionPolicy = Field(
        default="require_confidence",
        description="模型准入依据：真实置信度 / 独立约束校验；独立校验仅支持章节放置，仍需同策略基线达标",
    )
    fallback: FallbackAction = Field(
        default="rule_default", description="模型不可用时的动作；rule_default 保证构建不失败"
    )


class RoutingConfig(FormatModel):
    """决策点路由表：决策点标识 -> 路由配置。"""

    model_enabled: bool = Field(
        default=True, description="总开关；False 时全链路走规则，产出仍完整（验收 39）"
    )
    decision_points: dict[str, DecisionPointRoute] = Field(default_factory=dict)

    def route_for(self, decision_point: str) -> DecisionPointRoute:
        """取某决策点的路由配置；未配置时返回保守默认（自动 + 规则优先）。"""
        return self.decision_points.get(decision_point, DecisionPointRoute())


class DocumentEntry(FormatModel):
    """项目内的一份待生成文档。"""

    doc_key: str = Field(description="项目内唯一键，如 'overview' / 'detail'")
    title: str = Field(description="显示名")
    profile_id: str = Field(description="所用的文档类型档案")
    template_ref: str = Field(description="模板包引用 template_id@version")
    source_ids: list[str] = Field(default_factory=list, description="使用的事实源标识")
    overrides_ref: str | None = Field(
        default=None,
        description="覆盖配置路径。**只允许修正定位与映射**，不得改样式与版心（DD-7.3）",
    )
    enabled: bool = True


class RetentionPolicy(FormatModel):
    """保留策略（部署级 settings，可按项目覆盖）。"""

    uploads_days: int = Field(default=30, ge=1)
    workspace_days: int = Field(default=30, ge=1)
    artifacts_days: int = Field(default=180, ge=1)


class DataBoundary(FormatModel):
    """数据边界只读视图（DD-13.7）。

    服务端 ``GET /settings`` 返回本结构，使"数据不出内网"在界面上**可见**。
    """

    deployment: Literal["local", "intranet", "public"] = "local"
    model_enabled: bool = False
    model_base_url: str | None = None
    external_calls: bool = Field(default=False, description="是否存在任何外部网络调用")
    storage_path: str | None = None


class ProjectConfig(FormatModel):
    """项目级配置。写入不覆盖历史：每次写入生成新版本（DD-13.6）。"""

    format_family: ClassVar[str] = "config"

    project_id: str
    version: int = Field(default=1, ge=1, description="自增版本；回滚 = 以历史版本为内容再写一版")
    author: str | None = None
    documents: list[DocumentEntry] = Field(default_factory=list)
    pagination: PaginationPolicy = Field(default_factory=PaginationPolicy)
    terminology: Terminology = Field(default_factory=Terminology)
    rule_sets: list[RuleSetRef] = Field(default_factory=list)
    routing: RoutingConfig = Field(default_factory=RoutingConfig)
    retention: RetentionPolicy = Field(default_factory=RetentionPolicy)

    @model_validator(mode="after")
    def _check_doc_keys_unique(self) -> ProjectConfig:
        keys = [doc.doc_key for doc in self.documents]
        dupes = {key for key in keys if keys.count(key) > 1}
        if dupes:
            raise ValueError(f"documents 中存在重复 doc_key：{sorted(dupes)}")
        return self

    def enabled_documents(self) -> list[DocumentEntry]:
        return [doc for doc in self.documents if doc.enabled]

    def document(self, doc_key: str) -> DocumentEntry | None:
        for doc in self.documents:
            if doc.doc_key == doc_key:
                return doc
        return None


__all__ = [
    "AdmissionPolicy",
    "DataBoundary",
    "DecisionLevel",
    "DecisionPointRoute",
    "DecisionRoute",
    "DecisionStrategy",
    "DocumentEntry",
    "FallbackAction",
    "PaginationPolicy",
    "ProjectConfig",
    "RetentionPolicy",
    "RoutingConfig",
    "RuleSetRef",
    "Terminology",
    "TerminologyEntry",
    "TerminologyScope",
]
