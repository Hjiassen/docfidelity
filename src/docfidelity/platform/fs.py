"""受限路径访问与原子落盘（DD-2.1 ``platform``；NFR-08 / 10.11）。

两条纪律
--------
1. **越界必须报错，而不是"尽力而为"**。所有拼接外部输入的路径都要走
   :func:`safe_join`，越界抛 ``E_PATH_ESCAPE``。字符串前缀比对（``str(p).startswith(root)``）
   是不够的：``root-evil/x`` 会被前缀检查放过，``root/../secret`` 也一样。
   本模块用 ``resolve()`` 之后的**路径包含关系**判定，并顺带解掉符号链接。
2. **落盘必须原子**。写一半进程被杀会留下半截文件，而模板包与产物的读取方
   都假设"文件存在即完整"。临时文件必须与目标**同目录**——跨卷时 ``os.replace``
   会退化成拷贝，原子性随之消失，所以不能图省事写到系统临时目录。
"""

from __future__ import annotations

import io
import os
import tempfile
import zipfile
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path, PurePath, PureWindowsPath
from typing import Final

from docfidelity.contracts.errors import EngineError, ErrorCode

_TEMP_SUFFIX: Final = ".dftmp"
"""临时文件后缀。测试据此断言"保存后不残留临时文件"。"""


def _escape(candidate: Path, root: Path) -> EngineError:
    return EngineError(
        ErrorCode.PATH_ESCAPE,
        f"路径越出允许范围：{candidate}",
        where={"root": str(root), "candidate": str(candidate)},
        remediation=["改为位于允许根目录之内的相对路径"],
    )


def _comparison_path(path: Path) -> PurePath:
    if os.name != "nt":
        return path
    raw = str(path)
    # Windows resolve 在并发建目录时可能保留扩展前缀，比较时统一等价写法。
    if raw[:8].upper() == "\\\\?\\UNC\\":
        raw = "\\\\" + raw[8:]
    elif raw.startswith("\\\\?\\") and len(raw) >= 7 and raw[5:7] == ":\\":
        raw = raw[4:]
    return PureWindowsPath(raw)


def ensure_within(root: Path | str, candidate: Path | str) -> Path:
    """校验 ``candidate`` 落在 ``root`` 之内，返回解析后的绝对路径。

    ``root`` 自身也算在内（调用方可能确实要指向根目录）。
    """
    base = Path(root).resolve()
    target = Path(candidate).resolve()
    comparable_base = _comparison_path(base)
    comparable_target = _comparison_path(target)
    if comparable_target != comparable_base and comparable_base not in comparable_target.parents:
        raise _escape(target, base)
    return target


def safe_join(root: Path | str, *parts: str) -> Path:
    """在 ``root`` 之下按片段拼接路径，越界即抛 ``E_PATH_ESCAPE``。

    ``parts`` 里的 ``..`` 不会被拦截后放行，而是参与解析后由包含关系判定——
    因此 ``safe_join(root, "a/../b")`` 合法，``safe_join(root, "../b")`` 报错。
    """
    base = Path(root)
    if not parts:
        return base.resolve()
    return ensure_within(base, base.joinpath(*parts))


def ensure_dir(path: Path | str) -> Path:
    """确保目录存在（含父级），返回该目录。"""
    target = Path(path)
    target.mkdir(parents=True, exist_ok=True)
    return target


def atomic_write_bytes(path: Path | str, payload: bytes) -> Path:
    """原子写字节：同目录临时文件 → ``fsync`` → ``os.replace``。

    失败路径会清掉临时文件，因此调用方不会在目录里看到半成品。
    """
    target = Path(path)
    ensure_dir(target.parent)
    handle, temp_name = tempfile.mkstemp(
        dir=target.parent, prefix=f".{target.name}.", suffix=_TEMP_SUFFIX
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, target)
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise
    return target


def atomic_write_text(path: Path | str, text: str, *, encoding: str = "utf-8") -> Path:
    """原子写文本。换行不做平台转换——产物必须逐字节可复现。"""
    return atomic_write_bytes(path, text.encode(encoding))


def read_bytes(path: Path | str) -> bytes:
    """读取整个文件的字节。"""
    return Path(path).read_bytes()


def read_text(path: Path | str, *, encoding: str = "utf-8") -> str:
    """读取整个文件的文本。"""
    return Path(path).read_text(encoding=encoding)


def iter_files(root: Path | str, *, suffixes: Sequence[str] | None = None) -> Iterator[Path]:
    """按相对路径升序遍历目录下的**文件**。

    排序是刻意的：摘要计算依赖遍历顺序稳定，而文件系统的列举顺序不保证稳定。
    """
    base = Path(root)
    if not base.is_dir():
        return
    wanted = tuple(suffixes) if suffixes else None
    for path in sorted(item for item in base.rglob("*") if item.is_file()):
        if wanted is None or path.suffix.lower() in wanted:
            yield path


def relative_posix(path: Path | str, root: Path | str) -> str:
    """相对路径的 POSIX 形式，用作包内条目名。"""
    return Path(path).relative_to(Path(root)).as_posix()


ZIP_EPOCH: Final[tuple[int, int, int, int, int, int]] = (1980, 1, 1, 0, 0, 0)
"""zip 条目的固定时刻（zip 格式能表示的最小值）。

**同一份内容导出两次，字节必须完全相同**——导出件是"交给第三方复现"的凭据，
若每次导出的字节都不同，收件方就无法用摘要核对"你发我的就是你库里的那一份"。
而 zip 头部默认写"现在"，于是确定性只能靠钉死时刻来换。
"""


def zip_directory(root: Path | str, *, extra: Mapping[str, bytes] | None = None) -> bytes:
    """把目录下的文件打成 zip 字节；``extra`` 是额外条目（相对路径 → 内容）。

    条目按相对路径**排序**写入（``zipfile`` 按写入顺序排列，这是唯一能保证两次导出
    次序一致的做法），时刻统一取 :data:`ZIP_EPOCH`，压缩方式固定 deflate。
    这些"多此一举"的约束合起来只有一个目的：**内容相同 ⇒ 字节相同**。

    ``extra`` 的存在是因为有些导出件要附带一份**生成出来**的说明（如同一份档案对
    第三方复现者要说明的三条适用判据），它不是目录里的文件，但属于同一份凭据。
    名字与目录内文件重名时以 ``extra`` 为准（调用方自己保证不重名即可）。
    """
    base = Path(root)
    payloads: dict[str, bytes] = {}
    if base.is_dir():
        payloads.update(
            (relative_posix(path, base), path.read_bytes()) for path in iter_files(base)
        )
    payloads.update(extra or {})

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in sorted(payloads):
            info = zipfile.ZipInfo(filename=name, date_time=ZIP_EPOCH)
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, payloads[name])
    return buffer.getvalue()


__all__ = [
    "ZIP_EPOCH",
    "atomic_write_bytes",
    "atomic_write_text",
    "ensure_dir",
    "ensure_within",
    "iter_files",
    "read_bytes",
    "read_text",
    "relative_posix",
    "safe_join",
    "zip_directory",
]
