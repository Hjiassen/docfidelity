"""领域模型与投影（``model``，DD-6）。

内核第三层：把**领域对象**按档案声明装配成**内容单元**。它只知道两样东西——
领域模型声明的 ``kind`` / ``key`` / ``fields``，以及档案声明的投影规则；
文档类型、领域概念一概不进这一层（决策六，验收 42）。

边界
----
- **不得触碰 OOXML**：``.importlinter`` 的 ``model-has-no-ooxml`` 契约机器强制。
  投影求值不需要看文档——那是 ``render`` 的事。
- **不做 I/O**：本层是纯函数 + 数据结构。事实从哪里来是 ``ingest`` 的问题。
- **确定性**：内核禁用 ``random``；本层的排序、分组、``unit_id`` 派生都必须可复现
  （NFR-02 / FR-M4-12）。
"""

from __future__ import annotations

from .projection import (
    CONVENTION_RULE_ID,
    PROJECTION_RULE_ID,
    EvalContext,
    apply_filter,
    compose_sort,
    content_type_of,
    evaluate,
    is_per_item,
    resolve_mapping,
)
from .registry import DomainModelRegistry
from .validation import (
    DOMAIN_RULE_ID,
    PROFILE_RULE_ID,
    collect_findings,
    is_blank,
    validate_object,
    validate_objects,
    validate_profile,
)
from .values import read_field, scalar_text

__all__ = [
    "CONVENTION_RULE_ID",
    "DOMAIN_RULE_ID",
    "PROFILE_RULE_ID",
    "PROJECTION_RULE_ID",
    "DomainModelRegistry",
    "EvalContext",
    "apply_filter",
    "collect_findings",
    "compose_sort",
    "content_type_of",
    "evaluate",
    "is_blank",
    "is_per_item",
    "read_field",
    "resolve_mapping",
    "scalar_text",
    "validate_object",
    "validate_objects",
    "validate_profile",
]
