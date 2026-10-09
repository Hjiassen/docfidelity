"""接入安全校验（DD-5.4 / NFR-08 / 10.11）。

四条纪律
--------
1. **白名单优先**。"不在黑名单里"不等于"可以读"：扩展名必须显式在白名单内。
   可执行内容则与白名单无关，任何情况下都拒——白名单配错不该变成命令执行的口子。
2. **压缩包在解压前检查**。条目路径里的 `..` 与绝对路径、条目数、解压后总大小
   （zip 炸弹）、可执行成员、文档内宏（`vbaProject.bin`）都在**打开之前**判掉。
   先解压再检查等于已经把炸弹放进了磁盘。
3. **密钥类文件永不读取**。`*.pem` / `*.key` / `.env` 这类文件在目录扫描里被排除，
   在显式指定时直接拒绝。排除动作会留下一条告警——"静默跳过"与"本来没有"在产物上
   分不出来，而前者是安全事故的常见形态。
4. **从不执行被接入的代码**。本模块只读元数据（文件名、大小、条目清单），不导入、
   不解析、不运行（FR-M1-05 / FR-M1-12）。
"""

from __future__ import annotations

import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Final

from docfidelity.contracts.errors import EngineError, ErrorCode
from docfidelity.contracts.fact import SourceConfig
from docfidelity.platform.fs import iter_files, safe_join

#: 默认白名单扩展名。项目可在配置里覆盖（P13 区域 3）。
DEFAULT_ALLOWED_EXTENSIONS: Final[frozenset[str]] = frozenset(
    {
        ".json",
        ".yaml",
        ".yml",
        ".toml",
        ".py",
        ".sql",
        ".proto",
        ".md",
        ".csv",
        ".xlsx",
        ".docx",
        ".txt",
        ".png",
        ".jpg",
        ".jpeg",
        ".svg",
    }
)

#: 可执行 / 脚本内容：与白名单无关，一律拒。
EXECUTABLE_SUFFIXES: Final[frozenset[str]] = frozenset(
    {".exe", ".dll", ".so", ".dylib", ".bat", ".cmd", ".ps1", ".sh", ".com", ".scr", ".msi", ".js"}
)

#: 宏文档：格式本身即声明宏（打开即可能触发），直接拒绝。
MACRO_DOCUMENT_SUFFIXES: Final[frozenset[str]] = frozenset({".docm", ".xlsm", ".pptm", ".dotm"})

#: 压缩包容器：需要逐条目检查。
ARCHIVE_SUFFIXES: Final[frozenset[str]] = frozenset(
    {".zip", ".docx", ".xlsx", ".pptx", ".docm", ".xlsm", ".pptm"}
)

#: 宏载荷在压缩包内的固定位置。
MACRO_ENTRY_SUFFIX: Final = "vbaproject.bin"

#: 密钥类文件：**永不读取**。
SENSITIVE_NAMES: Final[frozenset[str]] = frozenset(
    {".env", ".netrc", ".npmrc", ".pypirc", "credentials", "id_rsa", "id_ed25519", "id_ecdsa"}
)

SENSITIVE_SUFFIXES: Final[frozenset[str]] = frozenset(
    {".pem", ".key", ".pfx", ".p12", ".keystore", ".jks"}
)

_ABSOLUTE_RE: Final = re.compile(r"^(?:/|[A-Za-z]:)")

_MIB: Final = 1024 * 1024


@dataclass(frozen=True)
class UploadPolicy:
    """上传与接入的容量与类型策略（部署级可配）。"""

    allowed_extensions: frozenset[str] = field(default_factory=lambda: DEFAULT_ALLOWED_EXTENSIONS)
    max_file_bytes: int = 32 * _MIB
    max_total_bytes: int = 256 * _MIB
    max_archive_entries: int = 4096
    max_uncompressed_bytes: int = 256 * _MIB


def _reject(code: str, message: str, **where: object) -> EngineError:
    return EngineError(
        code,
        message,
        where=dict(where),
        remediation=["改为白名单内的普通数据文件，或调整部署级上传策略"],
    )


def is_sensitive_name(name: str) -> bool:
    """文件名是否属于密钥 / 凭据类（按名判断，不需要读取内容）。"""
    lowered = name.lower()
    return lowered in SENSITIVE_NAMES or Path(lowered).suffix in SENSITIVE_SUFFIXES


def check_file(path: Path | str, policy: UploadPolicy | None = None) -> Path:
    """校验单个文件：敏感名 / 可执行 / 白名单 / 大小 / 压缩包条目。

    返回解析后的路径。任一不通过即抛 :class:`EngineError`。
    """
    target = Path(path)
    limits = policy or UploadPolicy()
    name = target.name

    if is_sensitive_name(name):
        raise _reject(
            ErrorCode.UPLOAD_TYPE,
            f"拒绝读取密钥 / 凭据类文件：{name}",
            file=name,
        )
    suffix = target.suffix.lower()
    if suffix in EXECUTABLE_SUFFIXES:
        raise _reject(ErrorCode.UPLOAD_TYPE, f"拒绝可执行内容：{name}", file=name)
    if suffix in MACRO_DOCUMENT_SUFFIXES:
        raise _reject(ErrorCode.UPLOAD_TYPE, f"拒绝宏文档：{name}", file=name)
    if suffix not in limits.allowed_extensions:
        raise _reject(
            ErrorCode.UPLOAD_TYPE,
            f"扩展名不在白名单内：{suffix or '(无扩展名)'}",
            file=name,
        )
    if not target.is_file():
        raise _reject(ErrorCode.UPLOAD_TYPE, f"不是可读文件：{name}", file=name)

    size = target.stat().st_size
    if size > limits.max_file_bytes:
        raise EngineError(
            ErrorCode.UPLOAD_SIZE,
            f"单文件超出上限：{size} > {limits.max_file_bytes} 字节",
            where={"file": name, "size": size},
            remediation=["压缩或拆分该文件后重试"],
        )
    if suffix in ARCHIVE_SUFFIXES:
        inspect_archive(target, limits)
    return target


def inspect_archive(path: Path | str, policy: UploadPolicy | None = None) -> list[str]:
    """在**解压之前**检查压缩包条目，返回条目名清单。

    Raises
    ------
    EngineError
        ``E_UPLOAD_TYPE``：不是有效压缩包、含越界条目、含可执行内容或宏载荷。
        ``E_UPLOAD_SIZE``：条目数或解压后总大小超限。
    """
    limits = policy or UploadPolicy()
    target = Path(path)
    try:
        with zipfile.ZipFile(target) as archive:
            infos = archive.infolist()
    except zipfile.BadZipFile as exc:
        raise _reject(
            ErrorCode.UPLOAD_TYPE, f"不是有效压缩包：{target.name}", file=target.name
        ) from exc

    if len(infos) > limits.max_archive_entries:
        raise EngineError(
            ErrorCode.UPLOAD_SIZE,
            f"压缩包条目数超限：{len(infos)} > {limits.max_archive_entries}",
            where={"file": target.name, "entries": len(infos)},
            remediation=["拆分压缩包后重试"],
        )

    total = 0
    names: list[str] = []
    for info in infos:
        name = info.filename
        names.append(name)
        posix = PurePosixPath(name.replace("\\", "/"))
        if _ABSOLUTE_RE.match(name) or ".." in posix.parts:
            raise _reject(ErrorCode.PATH_ESCAPE, f"压缩包含越界条目：{name}", file=target.name)
        if posix.suffix.lower() in EXECUTABLE_SUFFIXES:
            raise _reject(ErrorCode.UPLOAD_TYPE, f"压缩包含可执行内容：{name}", file=target.name)
        if name.lower().endswith(MACRO_ENTRY_SUFFIX):
            raise _reject(ErrorCode.UPLOAD_TYPE, f"压缩包含宏载荷：{name}", file=target.name)
        total += info.file_size

    if total > limits.max_uncompressed_bytes:
        raise EngineError(
            ErrorCode.UPLOAD_SIZE,
            f"压缩包解压后超出上限：{total} > {limits.max_uncompressed_bytes} 字节",
            where={"file": target.name, "uncompressed": total},
            remediation=["拆分压缩包后重试"],
        )
    return names


def check_tree(
    root: Path | str, policy: UploadPolicy | None = None
) -> tuple[list[Path], list[str]]:
    """校验目录树，返回（可用文件，告警）。

    密钥类文件在这里被**排除并记告警**（不是拒绝整个来源，也不是静默跳过）；
    可执行内容与超限则拒绝。只读元数据，不打开任何文件内容。
    """
    limits = policy or UploadPolicy()
    base = Path(root)
    if not base.is_dir():
        return [], []

    kept: list[Path] = []
    warnings: list[str] = []
    total = 0
    for path in iter_files(base):
        if is_sensitive_name(path.name):
            warnings.append(f"已跳过密钥 / 凭据类文件：{path.name}")
            continue
        if path.suffix.lower() in EXECUTABLE_SUFFIXES:
            raise _reject(ErrorCode.UPLOAD_TYPE, f"拒绝可执行内容：{path.name}", file=path.name)
        kept.append(path)
        total += path.stat().st_size
    if total > limits.max_total_bytes:
        raise EngineError(
            ErrorCode.UPLOAD_SIZE,
            f"来源总量超出上限：{total} > {limits.max_total_bytes} 字节",
            where={"root": str(base), "total": total},
            remediation=["缩减来源范围或调整部署级上传策略"],
        )
    return kept, warnings


@dataclass(frozen=True)
class SourceGuard:
    """把「工作区根 + 容量策略」绑在一起，供接入执行前统一把关。

    所有外部路径都经 :func:`~docfidelity.platform.fs.safe_join` 解析，
    越界抛 ``E_PATH_ESCAPE``——接入器自己拼路径是越界上报不出来的原因之一。
    """

    workspace_root: Path
    policy: UploadPolicy = field(default_factory=UploadPolicy)

    def resolve(self, relative: str) -> Path:
        """把配置里的相对路径解析到工作区内。"""
        return safe_join(self.workspace_root, relative)

    def check(self, cfg: SourceConfig) -> tuple[Path | None, list[str]]:
        """按来源配置把关，返回（解析后的目标，告警）。

        ``root`` 为空（如手工输入）时不做文件校验。目录来源的告警（例如跳过了
        密钥类文件）会被调用方带进事实包——**丢弃告警等于把安全事故伪装成正常**。
        """
        if cfg.root is None:
            return None, []
        target = self.resolve(cfg.root)
        if target.is_dir():
            _, warnings = check_tree(target, self.policy)
            return target, warnings
        return check_file(target, self.policy), []


__all__ = [
    "ARCHIVE_SUFFIXES",
    "DEFAULT_ALLOWED_EXTENSIONS",
    "EXECUTABLE_SUFFIXES",
    "MACRO_DOCUMENT_SUFFIXES",
    "SENSITIVE_NAMES",
    "SENSITIVE_SUFFIXES",
    "SourceGuard",
    "UploadPolicy",
    "check_file",
    "check_tree",
    "inspect_archive",
    "is_sensitive_name",
]
