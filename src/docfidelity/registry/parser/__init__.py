"""模板入库解析（DD-2.1 ``registry/parser``）：把模板原件解析成契约。

阶段划分沿用 DD-7.1 的流水线::

    unpack → styles → page → numbering → outline → blocks → slots → placeholders
             → fingerprint → package_io → register

本包只负责"原件 → 契约模型"这一段，不含指纹、落盘与登记——那三项分别在
``registry.fingerprint``（P0-05）、``registry.package_io`` 与 ``registry.profile_store``。

样式与编号两个阶段直接复用 :mod:`docfidelity.ooxml` 的实现，不在这里再写一遍：
它们在入库期与渲染期都要用，只能有一处实现，否则"入库时读到的样式"和
"渲染时读到的样式"会漂移。
"""

from __future__ import annotations

from .blocks import BLOCK_RULE_ID, DEFAULT_REVISION_KEYWORDS, describe_block, parse_blocks
from .common import (
    SCANNABLE_PART_PREFIXES,
    core_property,
    header_footer_parts,
    localname_of_part,
    paragraph_texts,
    table_texts,
)
from .outline import OUTLINE_RULE_ID, heading_levels, parse_outline
from .page import parse_page_setup
from .placeholders import (
    PLACEHOLDER_KEYWORDS,
    PLACEHOLDER_RULE_ID,
    count_by_part,
    matched_placeholder,
    parse_placeholders,
)
from .unpack import (
    DEFAULT_VERSION,
    IDENTITY_RULE_ID,
    SourceSnapshot,
    TemplateIdentity,
    detect_identity,
    open_template,
    snapshot_source,
)

__all__ = [
    "BLOCK_RULE_ID",
    "DEFAULT_REVISION_KEYWORDS",
    "DEFAULT_VERSION",
    "IDENTITY_RULE_ID",
    "OUTLINE_RULE_ID",
    "PLACEHOLDER_KEYWORDS",
    "PLACEHOLDER_RULE_ID",
    "SCANNABLE_PART_PREFIXES",
    "SourceSnapshot",
    "TemplateIdentity",
    "core_property",
    "count_by_part",
    "describe_block",
    "detect_identity",
    "header_footer_parts",
    "heading_levels",
    "localname_of_part",
    "matched_placeholder",
    "open_template",
    "paragraph_texts",
    "parse_blocks",
    "parse_outline",
    "parse_page_setup",
    "parse_placeholders",
    "snapshot_source",
    "table_texts",
]
