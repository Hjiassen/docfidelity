"""编排层：事实 → 内容 IR（``compose``，DD-8）。

职责单一：把**领域对象**按档案声明的投影规则装配成**内容 IR**。IR 是
``compose`` 与 ``render`` 之间唯一的接口（决策一：编排与渲染解耦）。

    FactSnapshot ──normalize──▶ DomainObject ──evaluate──▶ ContentUnit
                                                                │
        档案骨架 × 槽位 ──▶ 章节树 ──glossary──▶ captions ──▶ ContentIR
                                                                │
                                                           build_traces ──▶ TraceMatrix

模块分工
--------
===================  ==================================================================
``builder``          总装入口 ``compose()``：投影求值 → 槽位落位 → 图注 → 术语 → 自检
``glossary``         术语变量 + 连读重复检测（FR-M3-09），**全仓唯一实现**
``captions``         图注 / 表注编号 ``图 X-Y``（FR-M3-08）
``slots``            槽位校验：未填 / 类型越界 / id 重复 / 落位槽无人接收（FR-M3-15）
``precheck``         档案 × 模板包可满足性预检（DD-7.5 第 4 条，构建前防线）
``purity``           三条 IR 硬约束自检（NFR-28 / 验收 34）
``traces``           追溯矩阵 ``TraceEdge`` / orphans / unused（FR-M6-07）
===================  ==================================================================

边界
----
- **不得触碰 OOXML**：``.importlinter`` 的 ``compose-has-no-ooxml`` 机器强制。
  IR 不含样式信息，装配期也不需要模板知识——那是渲染层与预检的事。
- **不得依赖 render / qa**：装配只产 IR，不渲染、不判质量。
- **确定性**：集合遍历顺序显式排序，禁 ``random``；``unit_id`` 由投影路径确定性派生
  （NFR-02 / FR-M4-12）。
- **不做 I/O**：事实从哪来是 ``ingest`` 的问题；模板包从哪来是 ``registry`` 的问题。
"""

from __future__ import annotations

from .builder import ComposeResult, build_content_ir, compose
from .captions import CAPTION_RULE_ID, parse_numbered_caption
from .captions import assign as assign_captions
from .glossary import (
    GLOSSARY_RULE_ID,
    TERMINOLOGY_RULE_ID,
    apply_glossary_to_ir,
    fix_concat_duplication,
    glossary_apply,
    unresolved_variables,
)
from .precheck import PRECHECK_RULE_ID, satisfiable
from .purity import PURITY_RULE_ID, assert_ir_clean, check_ir_purity, ir_payload
from .slots import DEFAULT_SLOT, SLOT_RULE_ID, SlotPolicy, check_slots, orphan_slots
from .traces import TRACE_RULE_ID, build_traces

__all__ = [
    "CAPTION_RULE_ID",
    "DEFAULT_SLOT",
    "GLOSSARY_RULE_ID",
    "PRECHECK_RULE_ID",
    "PURITY_RULE_ID",
    "SLOT_RULE_ID",
    "TERMINOLOGY_RULE_ID",
    "TRACE_RULE_ID",
    "ComposeResult",
    "SlotPolicy",
    "apply_glossary_to_ir",
    "assert_ir_clean",
    "assign_captions",
    "build_content_ir",
    "build_traces",
    "check_ir_purity",
    "check_slots",
    "compose",
    "fix_concat_duplication",
    "glossary_apply",
    "ir_payload",
    "orphan_slots",
    "parse_numbered_caption",
    "satisfiable",
    "unresolved_variables",
]
