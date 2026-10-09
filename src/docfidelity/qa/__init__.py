"""门禁层：规则引擎、内置规则集、稳定化比对、渲染目检与类型无关检索器（DD-10）。

``qa`` 回答的问题是「这份产物**符不符合体例**」。它与 ``render`` 的写盘护栏不是
同一件事：护栏只查"产物本身已坏"（样式引用悬挂、占位残留），门禁查的是规则表那
十几条。两者都必要，且**结论必须能分开看**——一份"写得合规但样式回退了"的文档，
在护栏视角是坏的，在门禁视角可能是合规的。

``can_finalize`` 是唯一裁决出口
-------------------------------
:func:`can_finalize` 与 :attr:`Report.verdict` 是全项目对"能不能定稿"的唯一判断，
界面与调用方**不得**自行按"有没有 blocker"再推一遍（FR-M5-16 / DD-10.3）。
理由很实际：推导散落在多处之后，"门禁阻断"这条规则就有了多个实现，
而它们迟早会在某条豁免上给出不同结论。

导入本包即完成断言登记（见 :mod:`docfidelity.qa.rules`），故 :func:`resolve_check`
在任何正常入口下都能取到实现。
"""

from __future__ import annotations

# 导入即登记内置断言（这个副作用就是 qa.rules 存在的理由）。
from . import rules
from .compare import (
    CONTRACT_MODE_NOTE,
    STRUCTURE_MODE_NOTE,
    TEXT_MODE_NOTE,
    DiffMode,
    DiffResult,
    compare,
    metrics_of,
)
from .engine import (
    BLOCKING_RULE_ID,
    ENTRY_POINT_GROUP,
    REPORT_RULE_ID,
    RULE_FILE_FORMAT_VERSION,
    RULE_MODEL_RULE_ID,
    LoadedRules,
    QaContext,
    RuleFile,
    RuleSpec,
    apply_waivers,
    assign_finding_ids,
    builtin_rule_files,
    can_finalize,
    check_names,
    default_catalog,
    load_rule_file,
    parse_rule_file,
    register_check,
    resolve_check,
    rules_for,
    run,
    run_rule_set,
    summarize,
    verdict_of,
)
from .facade import audit_document, audit_rule_set, compare_documents
from .spotcheck import (
    SPOTCHECK_RULE_ID,
    SpotcheckResult,
    default_converter,
    default_extractor,
    spotcheck,
)
from .type_agnostic import TYPE_AGNOSTIC_RULE_ID, Hit, load_wordlist, scan, scan_ir, scan_sources

__all__ = [
    "BLOCKING_RULE_ID",
    "CONTRACT_MODE_NOTE",
    "ENTRY_POINT_GROUP",
    "REPORT_RULE_ID",
    "RULE_FILE_FORMAT_VERSION",
    "RULE_MODEL_RULE_ID",
    "SPOTCHECK_RULE_ID",
    "STRUCTURE_MODE_NOTE",
    "TEXT_MODE_NOTE",
    "TYPE_AGNOSTIC_RULE_ID",
    "DiffMode",
    "DiffResult",
    "Hit",
    "LoadedRules",
    "QaContext",
    "RuleFile",
    "RuleSpec",
    "SpotcheckResult",
    "apply_waivers",
    "assign_finding_ids",
    "audit_document",
    "audit_rule_set",
    "builtin_rule_files",
    "can_finalize",
    "check_names",
    "compare",
    "compare_documents",
    "default_catalog",
    "default_converter",
    "default_extractor",
    "load_rule_file",
    "load_wordlist",
    "metrics_of",
    "parse_rule_file",
    "register_check",
    "resolve_check",
    "rules",
    "rules_for",
    "run",
    "run_rule_set",
    "scan",
    "scan_ir",
    "scan_sources",
    "spotcheck",
    "summarize",
    "verdict_of",
]
