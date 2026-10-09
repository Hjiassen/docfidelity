"""门禁引擎：规则模型与加载、判定执行、豁免与报告（DD-10.1 / DD-10.3）。

门禁与渲染器的分工
------------------
``render`` 在写盘前拦一次（样式引用悬挂、占位残留），那组判定是**护栏**：它回答
"这份产物现在能不能交出去"。``qa`` 回答的是另一个问题——"这份产物符不符合体例"。
两者都必要，但**不是同一件事**：护栏只查"产物本身已坏"，门禁查的是规则表那 17 条。

因此本模块**独立复查产物**，而不是去读渲染器的自述。理由是可验证的：若门禁复用
渲染结论，那么"渲染器算错了"与"门禁通过了"会同时发生，而且不留痕迹——一次判定
被两处引用，却只执行了一次。上游（render）自己的判定可以经 ``QaContext.upstream``
并入报告，但走的是**去重合并**，且不改变本模块的独立结论。

``trace`` / ``origin`` 的白名单口径
-----------------------------------
IR 里 ``trace`` / ``origin`` 存的是 ``kind:key`` 引用句柄（DD-3.2（2）的已知边界）。
类型无关检索必须把它们排除在外，且**必须与 ``compose.purity.ir_payload`` 用同一口径**
——两处各写一份剔除规则，迟早出现"门禁说干净、CI 说命中"的分裂。故
``qa.type_agnostic`` 只调用 ``compose.purity``，不另写剔除逻辑。

规则执行失败不得中断门禁（DD-10.3）
-----------------------------------
一条规则的断言抛错，说明**这条规则**的判定不可信：它既不该让整次门禁失败（那会把
"某条规则写坏了"升级成"构建不可交付"），也不该被静默吞掉（那会让"规则没跑"
伪装成"规则通过"）。处置是记一条 ``minor`` 并写明失败码，其余规则照常跑完。

规则文件的 ``format_version`` 与契约家族版本是**两件事**
-------------------------------------------------------
DD-10.1 的规则文件写 ``format_version: "1"``（单段），而 ``contracts.base`` 的格式家族
版本是三段式（``"1.0.0"``）。它们校验的对象不同：前者校验"规则文件的写法"，后者校验
"契约数据的家族"。故这里用独立的 :data:`RULE_FILE_FORMAT_VERSION`，只比主版本。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any, Final, cast

import yaml

from docfidelity.contracts.config import PaginationPolicy, Terminology
from docfidelity.contracts.errors import (
    ConfigurationError,
    EngineError,
    ErrorCode,
    FormatVersionError,
)
from docfidelity.contracts.ir import ContentIR
from docfidelity.contracts.profile import DocTypeProfile
from docfidelity.contracts.qa import (
    Finding,
    Report,
    Rule,
    RuleCategory,
    RuleSet,
    RuleSeverity,
    TraceMatrix,
    Waiver,
    summarize,
    verdict_of,
)
from docfidelity.contracts.template import PageSetupContract, TemplatePackage
from docfidelity.ooxml import DOCUMENT_PART, DocxPackage

__all__ = [
    "BLOCKING_RULE_ID",
    "ENTRY_POINT_GROUP",
    "REPORT_RULE_ID",
    "RULE_FILE_FORMAT_VERSION",
    "RULE_MODEL_RULE_ID",
    "CheckFunction",
    "LoadedRules",
    "QaContext",
    "RuleFile",
    "RuleSpec",
    "apply_waivers",
    "assign_finding_ids",
    "builtin_rule_files",
    "can_finalize",
    "check_names",
    "default_catalog",
    "discover_entry_points",
    "load_rule_file",
    "parse_rule_file",
    "register_check",
    "resolve_check",
    "rules_for",
    "run",
    "run_rule_set",
    "summarize",
    "verdict_of",
]

# -- 规则编号（附录 C 的三条「元」规则） -------------------------------------
RULE_MODEL_RULE_ID: Final = "FR-M5-14"
"""FR-M5-14 严重级别与豁免。"""
REPORT_RULE_ID: Final = "FR-M5-15"
"""FR-M5-15 构建报告。"""
BLOCKING_RULE_ID: Final = "FR-M5-16"
"""FR-M5-16 门禁阻断。"""

#: 规则文件格式版本（DD-10.1）。主版本不同即拒绝加载：规则语义随格式版本变化，
#: 静默按旧语义跑新文件会让门禁给出无法归因的结论。
RULE_FILE_FORMAT_VERSION: Final = "1"

#: 第三方规则集的 entry point 组名（DD-10.1）。核心代码只认这个组名，
#: 不含任何插件名——「引擎不认识第三方」与「引擎不认识文档类型」是同一条纪律。
ENTRY_POINT_GROUP: Final = "docfidelity.qa_rules"

#: 内置规则集文件（随包分发，不需 entry point 即可用）。
BUILTIN_RULE_FILE: Final = Path(__file__).resolve().parent / "rules" / "builtin.yaml"

_SEVERITIES: Final[frozenset[str]] = frozenset({"blocker", "major", "minor"})
_CATEGORIES: Final[frozenset[str]] = frozenset(
    {"style", "layout", "terminology", "integrity", "trace", "spotcheck", "meta"}
)
_TOP_LEVEL_KEYS: Final[frozenset[str]] = frozenset({"format_version", "rule_set", "title", "rules"})
_RULE_KEYS: Final[frozenset[str]] = frozenset(
    {
        "id",
        "rule_id",
        "category",
        "severity",
        "title",
        "check",
        "scope",
        "params",
        "requires",
        "enabled",
        "description",
        "convention_ref",
    }
)

#: 结论的人读文案在 ``contracts.qa.VERDICT_TEXT``（结论与文案必须同源，
#: 见 :func:`~docfidelity.contracts.qa.verdict_of` 的说明）。


# ------------------------------------------------------------------ 规则模型
@dataclass(frozen=True)
class RuleSpec:
    """一条**已声明**的规则（规则文件的一行 + 断言函数名）。

    与契约 :class:`~docfidelity.contracts.qa.Rule` 只差两项：``check``（断言函数名）
    与 ``scope``（适用文档类型）是**声明期**概念，不进对外契约——契约描述"规则是什么"，
    这两项描述"由谁执行、对谁生效"。故 :meth:`to_contract` 会丢掉它们。
    """

    rule_id: str
    category: RuleCategory
    severity: RuleSeverity
    title: str
    check: str
    scope: tuple[str, ...] = ("*",)
    params: Mapping[str, Any] = field(default_factory=dict)
    requires: tuple[str, ...] = ()
    enabled: bool = True
    description: str | None = None
    convention_ref: str | None = None
    severity_override: RuleSeverity | None = None

    def applies_to(self, doc_type: str | None) -> bool:
        """本规则是否适用于该文档类型；``"*"`` 表示不限定。

        ``doc_type`` 为 ``None``（调用方没给类型）时**一律适用**：把"没给"当成
        "不匹配"会让整套规则静默不跑，而"门禁没跑"看起来和"门禁全过"一模一样。
        """
        if doc_type is None or not self.scope:
            return True
        return "*" in self.scope or doc_type in self.scope

    def to_contract(self) -> Rule:
        """转成对外契约模型（丢掉声明期字段，见类文档）。"""
        return Rule(
            rule_id=self.rule_id,
            category=self.category,
            severity=self.severity,
            title=self.title,
            description=self.description,
            enabled=self.enabled,
            params=dict(self.params),
            requires=list(self.requires),
            convention_ref=self.convention_ref,
        )


@dataclass(frozen=True)
class RuleFile:
    """一份规则文件（DD-10.1 的一个文档）。"""

    rule_set_id: str
    format_version: str
    rules: tuple[RuleSpec, ...]
    title: str | None = None

    def by_id(self) -> dict[str, RuleSpec]:
        return {rule.rule_id: rule for rule in self.rules}


@dataclass(frozen=True)
class LoadedRules:
    """已加载的规则全量：``rule_set_id -> RuleFile``。"""

    files: Mapping[str, RuleFile]
    warnings: tuple[str, ...] = ()

    def catalog(self) -> dict[str, RuleSpec]:
        """全部规则条目（跨规则集合并；同 ``rule_id`` 以 ``rule_set_id`` 升序后者为准）。"""
        merged: dict[str, RuleSpec] = {}
        for rule_set_id in sorted(self.files):
            merged.update(self.files[rule_set_id].by_id())
        return merged

    def rule_set(self, rule_set_id: str) -> RuleFile:
        file = self.files.get(rule_set_id)
        if file is None:
            raise EngineError(
                ErrorCode.PROFILE_RULESET_MISSING,
                f"规则集不存在：{rule_set_id}",
                where={"rule_set": rule_set_id, "available": sorted(self.files)},
                remediation=["在档案的 bindings.rule_sets 里改用已存在的规则集标识"],
            )
        return file


# ------------------------------------------------------------------ 加载
def _major(version: str) -> str:
    return str(version).split(".", 1)[0]


def _text(node: Mapping[str, Any], key: str, *, source: str) -> str:
    value = node.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ConfigurationError(
            f"规则文件缺少必填字段 {key!r}：{source}",
            where={"source": source, "key": key, "value": repr(value)},
        )
    return value


def _scope_of(node: Mapping[str, Any], rule_id: str, *, source: str) -> tuple[str, ...]:
    raw = node.get("scope", ["*"])
    if isinstance(raw, str):
        return (raw,)
    if isinstance(raw, Sequence) and not isinstance(raw, bytes):
        return tuple(str(item) for item in raw)
    raise ConfigurationError(
        f"规则 {rule_id} 的 scope 必须是字符串或字符串列表",
        where={"source": source, "rule_id": rule_id, "scope": repr(raw)},
    )


def _parse_rule(node: object, *, source: str, index: int) -> RuleSpec:
    where = {"source": source, "index": index}
    if not isinstance(node, Mapping):
        raise ConfigurationError(f"规则条目必须是映射：{source} #{index}", where=where)
    unknown = sorted(set(node) - _RULE_KEYS)
    if unknown:
        # 未知键必须报错而不是忽略：``scope`` 误写成 ``scopes`` 会被静默丢掉，
        # 规则于是"全类型生效"，现象是"某类文档上多了一批莫名其妙的告警"。
        raise ConfigurationError(
            f"规则条目含未知字段 {unknown}：{source} #{index}",
            where={**where, "unknown": unknown, "allowed": sorted(_RULE_KEYS)},
        )
    rule_id = _text(node, "id" if "id" in node else "rule_id", source=source)
    severity = _text(node, "severity", source=source)
    if severity not in _SEVERITIES:
        raise ConfigurationError(
            f"规则 {rule_id} 的 severity 取值非法：{severity!r}",
            where={**where, "severity": severity, "allowed": sorted(_SEVERITIES)},
        )
    category = _text(node, "category", source=source)
    if category not in _CATEGORIES:
        raise ConfigurationError(
            f"规则 {rule_id} 的 category 取值非法：{category!r}",
            where={**where, "category": category, "allowed": sorted(_CATEGORIES)},
        )
    requires_raw = node.get("requires", [])
    requires = (
        tuple(str(item) for item in requires_raw)
        if isinstance(requires_raw, Sequence) and not isinstance(requires_raw, str | bytes)
        else ()
    )
    params = node.get("params", {})
    if not isinstance(params, Mapping):
        raise ConfigurationError(
            f"规则 {rule_id} 的 params 必须是映射",
            where={**where, "rule_id": rule_id, "params": repr(params)},
        )
    description = node.get("description")
    convention_ref = node.get("convention_ref")
    return RuleSpec(
        rule_id=rule_id,
        category=cast(RuleCategory, category),
        severity=cast(RuleSeverity, severity),
        title=_text(node, "title", source=source),
        check=_text(node, "check", source=source),
        scope=_scope_of(node, rule_id, source=source),
        params=dict(params),
        requires=requires,
        enabled=bool(node.get("enabled", True)),
        description=description if isinstance(description, str) else None,
        convention_ref=convention_ref if isinstance(convention_ref, str) else None,
    )


def parse_rule_file(payload: object, *, source: str = "<inline>") -> RuleFile:
    """解析一份规则文档（DD-10.1 的 YAML 结构）。

    ``source`` 只用于错误定位：某个规则的 ``severity`` 写错时，报错必须指出**哪个文件**
    ——否则使用者面对一柜子模板包无从下手。
    """
    if not isinstance(payload, Mapping):
        raise ConfigurationError(f"规则文件顶层必须是映射：{source}", where={"source": source})
    unknown = sorted(set(payload) - _TOP_LEVEL_KEYS)
    if unknown:
        raise ConfigurationError(
            f"规则文件含未知顶层字段 {unknown}：{source}",
            where={"source": source, "unknown": unknown, "allowed": sorted(_TOP_LEVEL_KEYS)},
        )
    version = _text(payload, "format_version", source=source)
    if _major(version) != _major(RULE_FILE_FORMAT_VERSION):
        raise FormatVersionError(
            ErrorCode.FORMAT_VERSION,
            f"规则文件格式版本不兼容：{version}（本引擎支持 {RULE_FILE_FORMAT_VERSION}.x）",
            where={"source": source, "found": version, "supported": RULE_FILE_FORMAT_VERSION},
            remediation=["按当前格式版本重写规则文件，或升级引擎"],
        )
    rules_raw = payload.get("rules", [])
    if not isinstance(rules_raw, Sequence) or isinstance(rules_raw, str | bytes):
        raise ConfigurationError(
            f"rules 必须是列表：{source}", where={"source": source, "rules": repr(rules_raw)}
        )
    rules = tuple(
        _parse_rule(node, source=source, index=index) for index, node in enumerate(rules_raw)
    )
    seen: set[str] = set()
    for rule in rules:
        if rule.rule_id in seen:
            # 同文件内重复的 rule_id 会让"哪一条在跑"取决于加载顺序，表现为
            # "改了规则但没生效"，而文件看上去完全正常。
            raise ConfigurationError(
                f"规则文件内 rule_id 重复：{rule.rule_id}",
                where={"source": source, "rule_id": rule.rule_id},
            )
        seen.add(rule.rule_id)
    title = payload.get("title")
    return RuleFile(
        rule_set_id=_text(payload, "rule_set", source=source),
        format_version=version,
        rules=rules,
        title=title if isinstance(title, str) else None,
    )


def load_rule_file(path: str | Path) -> RuleFile:
    """从磁盘加载一份规则文件（YAML）。"""
    target = Path(path)
    if not target.is_file():
        raise ConfigurationError(f"规则文件不存在：{target}", where={"path": str(target)})
    try:
        payload = yaml.safe_load(target.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigurationError(
            f"规则文件不是合法 YAML：{target}",
            where={"path": str(target), "error": str(exc)},
        ) from exc
    return parse_rule_file(payload, source=str(target))


def builtin_rule_files() -> tuple[RuleFile, ...]:
    """内置规则集（随包分发）。"""
    if not BUILTIN_RULE_FILE.is_file():
        return ()
    return (load_rule_file(BUILTIN_RULE_FILE),)


def discover_entry_points() -> tuple[RuleFile, ...]:
    """把 entry point 提供的规则文件并入（DD-10.1）。

    entry point 的 ``value`` 指向**一个返回规则映射（或其列表）的可调用对象**
    （``module:factory``），于是第三方既可用 YAML 也可用代码生成规则。与 ``ingest``
    的接入器发现同构：导入内核**不等于**执行插件——本函数必须被显式调用。
    """
    files: list[RuleFile] = []
    for entry in entry_points(group=ENTRY_POINT_GROUP):
        try:
            produced = entry.load()()
        except Exception as exc:  # 单个插件坏掉不该拦住其余插件
            raise ConfigurationError(
                f"规则集插件加载失败：{entry.name}",
                where={"entry_point": entry.name, "value": entry.value},
            ) from exc
        candidates = produced if isinstance(produced, list | tuple) else [produced]
        for index, node in enumerate(candidates):
            files.append(parse_rule_file(node, source=f"{entry.name}#{index}"))
    return tuple(files)


def default_catalog(*, include_entry_points: bool = False) -> LoadedRules:
    """默认规则目录：内置 + 可选的第三方。

    ``include_entry_points=False`` 是刻意的默认——源码树直接可用且**无副作用**，
    与 ``ingest.default_registry`` 同一条纪律。
    """
    files: list[RuleFile] = list(builtin_rule_files())
    if include_entry_points:
        files.extend(discover_entry_points())
    merged: dict[str, RuleFile] = {}
    warnings: list[str] = []
    for file in files:
        if file.rule_set_id in merged:
            warnings.append(f"规则集标识重复，以加载顺序后者为准：{file.rule_set_id}")
        merged[file.rule_set_id] = file
    return LoadedRules(files=merged, warnings=tuple(warnings))


def rules_for(rule_set: RuleSet, catalog: Mapping[str, RuleSpec]) -> list[RuleSpec]:
    """按规则集声明选出规则（含启用/禁用与严重级别、参数覆盖）。

    覆盖顺序固定为「先选再改」：``included`` / ``excluded`` 决定**有哪些**，
    ``severity_overrides`` / ``param_overrides`` 决定**长什么样**。反过来做的话，
    一条被 ``excluded`` 的规则仍会被 ``severity_overrides`` 命中，现象是"排除了但还在报"。
    """
    selected: list[RuleSpec] = []
    for rule_id in sorted(catalog):
        if rule_set.included_rules and rule_id not in rule_set.included_rules:
            continue
        if rule_id in rule_set.excluded_rules:
            continue
        selected.append(_apply_overrides(catalog[rule_id], rule_set))
    missing = sorted(set(rule_set.included_rules) - set(catalog))
    if missing:
        raise EngineError(
            ErrorCode.PROFILE_RULESET_MISSING,
            f"规则集 {rule_set.rule_set_id} 引用了未登记的规则：{missing}",
            where={"rule_set": rule_set.rule_set_id, "missing": missing},
            remediation=["在规则文件里补上这些 rule_id，或从档案的 included_rules 移除"],
        )
    return selected


def _apply_overrides(spec: RuleSpec, rule_set: RuleSet) -> RuleSpec:
    severity = rule_set.severity_overrides.get(spec.rule_id)
    params = rule_set.param_overrides.get(spec.rule_id)
    updates: dict[str, Any] = {}
    if severity is not None:
        if severity not in _SEVERITIES:
            raise ConfigurationError(
                f"规则 {spec.rule_id} 的 severity 覆盖取值非法：{severity!r}",
                where={"rule_id": spec.rule_id, "severity": severity},
            )
        updates["severity"] = severity
        updates["severity_override"] = severity
    if params is not None:
        updates["params"] = {**spec.params, **params}
    return replace(spec, **updates) if updates else spec


# ------------------------------------------------------------------ 断言注册表
#: 断言函数签名：``(规则, 上下文) -> 命中列表``。
CheckFunction = Callable[["RuleSpec", "QaContext"], list[Finding]]

_CHECKS: dict[str, CheckFunction] = {}


def register_check(name: str, function: CheckFunction) -> None:
    """登记一个断言实现。重复登记同名断言即报错。

    重复登记是**缺陷**而非覆盖：两个模块各自 ``register_check("x", ...)`` 时，实际跑的
    是最后导入的那一个，而"哪个后导入"取决于 import 顺序——同一份代码在不同入口下
    会得出不同结论。
    """
    existing = _CHECKS.get(name)
    if existing is not None and existing is not function:
        raise ConfigurationError(
            f"断言名重复登记：{name}",
            where={"check": name, "existing": repr(existing), "incoming": repr(function)},
        )
    _CHECKS[name] = function


def resolve_check(name: str) -> CheckFunction:
    """按名取断言实现；未登记即抛错（由 :func:`run` 折成 minor）。"""
    function = _CHECKS.get(name)
    if function is None:
        raise EngineError(
            ErrorCode.INTERNAL,
            f"规则声明的断言未登记：{name}",
            where={"check": name, "registered": check_names()},
            remediation=["确认 qa.rules 已被导入（登记发生在导入期）"],
        )
    return function


def check_names() -> list[str]:
    """已登记的断言名（升序）。"""
    return sorted(_CHECKS)


# ------------------------------------------------------------------ 上下文
@dataclass
class QaContext:
    """门禁判定的输入：产物包 + 契约 + IR + 可选的外部证据。

    只放**判定需要的东西**。门禁读产物（``pkg``）、契约（``template`` / ``profile``）
    与 IR（结构、槽位、追溯），再加可选的对照组：``baseline``（回归基线）、
    ``traces``（追溯完整）、``terminology``（术语一致）、``spotcheck``（目检结论）。
    """

    pkg: DocxPackage
    template: TemplatePackage
    ir: ContentIR
    profile: DocTypeProfile | None = None
    traces: TraceMatrix | None = None
    terminology: Terminology | None = None
    baseline: DocxPackage | None = None
    baseline_ref: str = ""
    spotcheck: Any | None = None
    document_part: str = DOCUMENT_PART
    #: 本次构建**实际使用**的分页策略。
    #:
    #: 必须由调用方冻进来，而不是让分页规则就地取默认值：``FR-M5-04`` 的白名单是
    #: 「策略允许哪些分页点」推出来的，拿默认策略去判一份按 ``page_break_before_chapter``
    #: 渲染的产物，会把每一处**合规的**章节分页都报成 blocker——而报告上看起来
    #: 只是"文档分了页"，没人会怀疑是判据拿错了策略。
    pagination: PaginationPolicy = field(default_factory=PaginationPolicy)
    #: 上游（如 render）自己的判定；经 :func:`run` 去重并入报告。
    upstream: tuple[Finding, ...] = ()

    @property
    def page_setup(self) -> PageSetupContract:
        return self.template.page_setup

    @property
    def doc_type(self) -> str | None:
        return None if self.profile is None else self.profile.doc_type

    def capabilities(self) -> frozenset[str]:
        """本上下文**实际具备**的判定能力（``RuleSpec.requires`` 据此跳过规则）。

        由"有没有给对应输入"推出，而不是由调用方声明：调用方声明漏了一项，规则会在
        一半构建里跑、另一半里静默跳过，而两种情况下报告都写着"通过"。
        """
        caps = {
            "ooxml.styles",
            "ooxml.geometry",
            "ooxml.pictures",
            "ooxml.normalize",
            "contract",
            "ir",
        }
        if self.profile is not None:
            # 渲染视图要档案才能构造（内容区判据需要骨架层级 → 样式映射）。
            caps.add("render_view")
        if self.terminology is not None:
            caps.add("terminology")
        if self.traces is not None:
            caps.add("trace")
        if self.baseline is not None:
            caps.add("baseline")
        if self.spotcheck is not None:
            caps.add("spotcheck")
        return frozenset(caps)


# ------------------------------------------------------------------ 执行
def run(
    *,
    context: QaContext,
    rules: Sequence[RuleSpec],
    waivers: Sequence[Waiver] = (),
    now: datetime | None = None,
    report_id: str | None = None,
    job_id: str = "",
) -> Report:
    """执行规则集，产出报告（DD-10.3）。

    ``can_finalize`` 的唯一出口是 :func:`can_finalize`（= ``verdict != "blocked"``），
    界面不得自行推导（FR-M5-16 / DD-10.3）。
    """
    moment = now or datetime.now(tz=timezone.utc)
    capabilities = context.capabilities()
    findings: list[Finding] = []
    skipped: list[str] = []
    overrides: dict[str, RuleSeverity] = {}

    for spec in rules:
        if not spec.enabled or not spec.applies_to(context.doc_type):
            continue
        missing = sorted(set(spec.requires) - capabilities)
        if missing:
            skipped.append(f"{spec.rule_id}(缺 {', '.join(missing)})")
            continue
        if spec.severity_override is not None:
            overrides[spec.rule_id] = spec.severity_override
        findings.extend(_dispatch(spec, context))

    upstream = [
        item.model_copy(update={"severity": overrides[item.rule_id]})
        if item.rule_id in overrides
        else item
        for item in context.upstream
    ]
    findings = _merge(findings, upstream)
    waived = apply_waivers(findings, waivers, now=moment)
    verdict = verdict_of(findings)
    resolved_report_id = report_id or _report_id(findings, moment)
    assign_finding_ids(findings, resolved_report_id)
    return Report(
        report_id=resolved_report_id,
        job_id=job_id,
        generated_at=moment,
        findings=findings,
        waivers=waived,
        verdict=verdict,
        summary=summarize(findings, verdict=verdict, skipped=skipped),
        # 跳过项同时**落成数据**：摘要那句散文在人工裁决重算之后就提取不出来了
        # （见 ``Report.skipped`` 的说明）。
        skipped=list(skipped),
    )


def run_rule_set(
    *,
    context: QaContext,
    rule_set: RuleSet,
    loaded: LoadedRules | None = None,
    waivers: Sequence[Waiver] = (),
    now: datetime | None = None,
    report_id: str | None = None,
    job_id: str = "",
) -> Report:
    """按档案声明的规则集跑一遍（``rule_set_id`` -> 目录 -> 执行）。"""
    source = loaded or default_catalog()
    file = source.rule_set(rule_set.rule_set_id)
    return run(
        context=context,
        rules=rules_for(rule_set, file.by_id()),
        waivers=waivers,
        now=now,
        report_id=report_id,
        job_id=job_id,
    )


def _dispatch(spec: RuleSpec, context: QaContext) -> list[Finding]:
    """执行一条规则；**任何**异常都折成一条 ``minor``，绝不外抛（DD-10.3）。"""
    try:
        findings = list(resolve_check(spec.check)(spec, context))
        if spec.severity_override is not None:
            findings = [
                item.model_copy(update={"severity": spec.severity_override}) for item in findings
            ]
        return findings
    except EngineError as exc:
        return [_failure(spec, exc.code, str(exc))]
    except Exception as exc:  # 断言写坏 / 第三方库异常：同样是"这条规则不可信"
        return [_failure(spec, type(exc).__name__, str(exc))]


def _failure(spec: RuleSpec, code: str, detail: str) -> Finding:
    return Finding(
        rule_id=spec.rule_id,
        severity="minor",
        message=f"规则执行失败（{code}）：本条判定不可信，其余规则不受影响",
        evidence=detail[:500],
        suggestion="修正该规则的断言实现，或补齐它 requires 声明的能力后重跑",
    )


def _merge(primary: Sequence[Finding], upstream: Sequence[Finding]) -> list[Finding]:
    """把上游判定并入主判定（按同一性去重，保序）。

    同一结论只保留一条——``render`` 与 ``qa`` 都会判 FR-M5-04，两处都报会让
    "分页符有 1 处"看起来像 2 处。先来者（门禁自己）优先，因为它判的是**最终产物**。
    """
    merged: list[Finding] = []
    seen: set[tuple[Any, ...]] = set()
    for finding in (*primary, *upstream):
        key = _identity(finding)
        if key in seen:
            continue
        seen.add(key)
        merged.append(finding)
    return merged


def _identity(finding: Finding) -> tuple[Any, ...]:
    location = finding.location
    return (
        finding.rule_id,
        finding.severity,
        finding.message,
        location.unit_id,
        tuple(location.chapter_path),
        location.part,
        location.xpath,
        location.page_hint,
        finding.evidence,
    )


def apply_waivers(
    findings: Sequence[Finding], waivers: Sequence[Waiver], *, now: datetime
) -> list[Waiver]:
    """把生效的豁免落到判定上（改判定状态，返回**生效**的豁免）。

    过期的豁免不算数：``expires_at`` 一旦过去即自动失效（契约 :meth:`Waiver.is_active`）。
    "过期豁免仍然生效"是最危险的一种配置——它让一条 blocker 永远不阻断，而看板上
    这条豁免还写着"已批准"。
    """
    active = [waiver for waiver in waivers if waiver.is_active(now=now)]
    by_rule: dict[str, Waiver] = {}
    for waiver in active:
        previous = by_rule.get(waiver.rule_id)
        if previous is None or _expiry_key(waiver) < _expiry_key(previous):
            by_rule[waiver.rule_id] = waiver
    for finding in findings:
        matched = by_rule.get(finding.rule_id)
        if matched is None:
            continue
        finding.status = "waived"
        finding.waiver_id = matched.waiver_id
    return active


def _expiry_key(waiver: Waiver) -> tuple[int, float]:
    """排序键：有过期时间者优先取**最早**到期的那条（豁免收窄优先）。"""
    if waiver.expires_at is None:
        return (1, 0.0)
    return (0, waiver.expires_at.timestamp())


def can_finalize(report: Report) -> bool:
    """``can_finalize`` 的唯一裁决出口（FR-M5-16）。"""
    return report.verdict != "blocked"


def assign_finding_ids(findings: Sequence[Finding], report_id: str) -> None:
    """给每条判定一个**可寻址**标识（``<报告标识>@<序号>``）。

    判定的身份不能留给"报告里第几条"：那个坐标得由调用方自己算，正是"前端推断"
    （DD-14.3 禁止），而且一旦有人重排 ``findings``，同一个坐标就指向了另一条规则。
    报告一经产出即不可变，故序号在此之后是稳定的；服务端拿这个标识反查时**校验**
    回来的是不是同一条，不一致就报找不到，而不是静默改指。

    标识里不含 ``job_id``：``qa`` 可以在没有作业上下文的场合被调用（``job_id`` 默认空串），
    而报告标识在任何场合都有值。

    **公开**而不是私有：产出一份"能被人指认"的报告不只是 :func:`run` 的事——
    凡是构造 :class:`~docfidelity.contracts.qa.Report` 的地方（含测试夹具）都必须用
    同一套规则赋标识，否则会出现"有的报告能提交裁决、有的不能"，而原因看上去
    是随机的。
    """
    for ordinal, finding in enumerate(findings, start=1):
        finding.finding_id = f"{report_id}@{ordinal}"


def _report_id(findings: Sequence[Finding], moment: datetime) -> str:
    """报告标识：由判定内容与时刻派生，使"同一份报告"可被反复引用。

    刻意**不**用随机数：内核禁止 ``random`` 参与逻辑（DD-2.4），而且报告标识一旦随机，
    "同一份报告"就没法在日志与界面之间对齐。
    """
    payload = json.dumps([_identity(item) for item in findings], ensure_ascii=False, sort_keys=True)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
    return f"RPT-{moment.strftime('%Y%m%d%H%M%S')}-{digest}"
