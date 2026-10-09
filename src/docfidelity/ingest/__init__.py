"""事实源接入（``ingest``，DD-5）。

职责：把**外部来源**读成事实，再把事实归一、合并成一个稳定快照。
它不认识文档，也不认识章节——那是 ``compose`` / ``render`` 的事。

    SourceConfig ──▶ Connector ──▶ FactBundle ──▶ merge ──▶ FactSnapshot
                                                     │
                                       normalize ◀───┘        （快照 → 领域对象）

边界
----
- **不得触碰 OOXML**：``.importlinter`` 的 ``ingest-has-no-forbidden-deps`` 机器强制；
  接入层与文档概念无关。
- **不得依赖 registry / render / qa**：接入只产事实包，不产文档、不判质量。
- **从不执行被接入的代码**（FR-M1-05）：ast / orm 都是纯解析。
- **确定性**：事实顺序、摘要、合并结果都只由内容决定（NFR-02）。
"""

from __future__ import annotations

from .emitter import FactEmitter
from .loader import build_bundle, fact_digest, fact_sort_key, probe_connector, run_connector
from .merge import CONFLICT_RULE_ID, ConflictRules, PriorityTable, merge
from .normalize import normalize
from .protocol import FactSourceConnector, ProbeResult
from .registry import (
    ENTRY_POINT_GROUP,
    ConnectorRegistry,
    builtin_connectors,
    default_registry,
    discover_entry_points,
)
from .safety import (
    SourceGuard,
    UploadPolicy,
    check_file,
    check_tree,
    inspect_archive,
    is_sensitive_name,
)

__all__ = [
    "CONFLICT_RULE_ID",
    "ENTRY_POINT_GROUP",
    "ConflictRules",
    "ConnectorRegistry",
    "FactEmitter",
    "FactSourceConnector",
    "PriorityTable",
    "ProbeResult",
    "SourceGuard",
    "UploadPolicy",
    "build_bundle",
    "builtin_connectors",
    "check_file",
    "check_tree",
    "default_registry",
    "discover_entry_points",
    "fact_digest",
    "fact_sort_key",
    "inspect_archive",
    "is_sensitive_name",
    "merge",
    "normalize",
    "probe_connector",
    "run_connector",
]
