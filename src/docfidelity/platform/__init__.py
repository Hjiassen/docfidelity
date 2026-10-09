"""纯基础设施：路径、哈希、日志（DD-2.1）。

本层只依赖标准库与 ``contracts``，**不得**依赖任何内核业务模块。
它是"内核零 Web 依赖"（决策四）之外的另一条基线：内核里的每个模块都
可以引入本层，而本层不会反向把 Web / DB / 业务概念带进来。
"""

from __future__ import annotations

from .fs import (
    atomic_write_bytes,
    atomic_write_text,
    ensure_dir,
    ensure_within,
    iter_files,
    read_bytes,
    read_text,
    relative_posix,
    safe_join,
)
from .hashing import (
    canonical_json,
    combine_digests,
    digest_entries,
    digest_payload,
    sha256_file,
    sha256_hex,
)

__all__ = [
    "atomic_write_bytes",
    "atomic_write_text",
    "canonical_json",
    "combine_digests",
    "digest_entries",
    "digest_payload",
    "ensure_dir",
    "ensure_within",
    "iter_files",
    "read_bytes",
    "read_text",
    "relative_posix",
    "safe_join",
    "sha256_file",
    "sha256_hex",
]
