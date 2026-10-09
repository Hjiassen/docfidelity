"""模板库（DD-2.1 ``registry``）：入库解析、契约查询、模板包读写、指纹与版本比对。

职责边界
--------
本模块是**唯一**把模板原件变成契约的地方，也是生成期**唯一**的模板知识来源
（决策二）。它被允许依赖 ``contracts`` / ``ooxml`` / ``platform``，而
``compose`` / ``render`` / ``qa`` 不得反向依赖它——由 ``.importlinter`` 强制。

一处实现，一处真相
------------------
模板知识在本模块里各有**唯一**定义处，改动时不要绕开：

* 模板包的目录布局与"哪些文件参与 ``package_hash``" → :mod:`.package_io`
* 「哪个分区包含哪些契约内容」 → :func:`.fingerprint.section_payloads`
* 「易变标识怎么归一化」 → ``ooxml.normalize``（本模块只调用，不复制）

未实现：``profile_store``（文档类型档案的登记与双向校验，DD-7.5，属 P0-06 一侧）。
"""

from __future__ import annotations

from .contract import ContractQuery
from .fingerprint import (
    ALL_SECTIONS,
    FINGERPRINT_SECTIONS,
    SOURCE_SECTION,
    compute_fingerprint,
    section_payloads,
    source_digest,
    verify_fingerprint,
)
from .package_io import (
    BLOCK_DIR,
    BLOCKS_PATH,
    MANIFEST_PATH,
    OVERRIDES_PATH,
    PAGE_SETUP_PATH,
    PLACEHOLDERS_PATH,
    REQUIRED_PATHS,
    PackageWriteReport,
    build_contract_payloads,
    compute_package_hash,
    read_block_assets,
    read_overrides,
    read_package,
    verify_package,
    write_overrides,
    write_package,
)
from .pipeline import (
    STAGE_ORDER,
    IngestResult,
    StageOutcome,
    ingest_template,
    parse_template,
)
from .version_diff import (
    CATEGORY_PAGE_SETUP,
    CATEGORY_SOURCE,
    CATEGORY_STRUCTURE,
    CATEGORY_STYLE_LAYER,
    CATEGORY_TABLE_PROTOTYPE,
    SECTION_IMPACTS,
    FieldChange,
    SectionDelta,
    TemplateVersionDiff,
    diff_fingerprints,
    diff_packages,
    flatten,
)

__all__ = [
    "ALL_SECTIONS",
    "BLOCKS_PATH",
    "BLOCK_DIR",
    "CATEGORY_PAGE_SETUP",
    "CATEGORY_SOURCE",
    "CATEGORY_STRUCTURE",
    "CATEGORY_STYLE_LAYER",
    "CATEGORY_TABLE_PROTOTYPE",
    "FINGERPRINT_SECTIONS",
    "MANIFEST_PATH",
    "OVERRIDES_PATH",
    "PAGE_SETUP_PATH",
    "PLACEHOLDERS_PATH",
    "REQUIRED_PATHS",
    "SECTION_IMPACTS",
    "SOURCE_SECTION",
    "STAGE_ORDER",
    "ContractQuery",
    "FieldChange",
    "IngestResult",
    "PackageWriteReport",
    "SectionDelta",
    "StageOutcome",
    "TemplateVersionDiff",
    "build_contract_payloads",
    "compute_fingerprint",
    "compute_package_hash",
    "diff_fingerprints",
    "diff_packages",
    "flatten",
    "ingest_template",
    "parse_template",
    "read_block_assets",
    "read_overrides",
    "read_package",
    "section_payloads",
    "source_digest",
    "verify_fingerprint",
    "verify_package",
    "write_overrides",
    "write_package",
]
