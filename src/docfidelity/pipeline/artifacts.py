"""产物用例：两份产出的稳定化比对（DD-10.4 / 需求 7.5.3；P11 的「版本对比」）。

为什么这条缝必须存在
--------------------
"两份产出差在哪"的判据只有一个实现——:func:`docfidelity.qa.compare.compare`，而
``server`` 不得 import ``qa``（``.importlinter`` 的 ``server-has-no-ooxml``，DD-13.1）。
把比对在服务端重写一遍，就等于让这个问题有两个答案；而两个答案迟早在归一化上分叉
（一份处理了 ``w:rsid*``、另一份没有），于是同一对产物在界面上报"有差异"、
在报告里报"一致"——这正是需求 10.9 要挡的形状。

于是本模块是 ``qa`` → ``pipeline`` 的唯一出口，且**只转发不重写**。

入参为什么是路径而不是包对象
----------------------------
服务端手里只有产物登记里的**相对路径**与哈希（``contracts.job.Artifact`` 不携带包）。
让调用方自己先开包再传进来，会把 ``DocxPackage.open`` 的生命周期管理摊到服务端，
而"包什么时候关"在那一边没有天然的所有者（HTTP 层不该持有文件句柄）。

``equal`` 也由这一层给
----------------------
界面需要的是"一样 / 不一样"，判据与 :func:`~docfidelity.qa.compare.compare` 同源：
三个差异集合全空。让界面按 ``added`` / ``removed`` / ``changed`` 自己推一遍，
就有了第二个判据（NFR-22 禁的正是这种"界面算业务结论"）。

三种分辨率与 ``qa`` 同源
------------------------
``structure`` / ``text`` / ``contract`` 直接取自 ``qa.compare.DiffMode``，
本模块不另立枚举——另立之后"界面能选的值"与"比对器认的值"会各自演化。
"""

from __future__ import annotations

import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Final, cast

from docfidelity.contracts.errors import EngineError, ErrorCode
from docfidelity.ooxml import DocxPackage
from docfidelity.qa import DiffMode, compare
from docfidelity.qa.compare import MAX_ITEMS

__all__ = [
    "COMPARE_MODES",
    "DiffMode",
    "DocumentDiff",
    "compare_documents",
]

#: 可比对的分辨率，顺序即界面下拉的顺序（粗 → 细）。
#:
#: 类型是 ``tuple[DiffMode, ...]`` 而不是 ``tuple[str, ...]``：这组取值**就是**
#: ``qa.compare.DiffMode`` 的三个字面量，写成 ``str`` 会让"服务端认哪些值"这件事
#: 在接口形状里丢掉，于是前端只能手写一份标签表——两份迟早分叉。
COMPARE_MODES: Final[tuple[DiffMode, ...]] = ("structure", "text", "contract")

#: 打开包时可能撞上的异常。``DocxPackage.open`` 是 zipfile + XML 的组合，两者各有各的
#: 坏法：截断的 zip 抛 ``BadZipFile``，缺部件抛 ``KeyError``，路径不对抛 ``OSError``。
#: 三种都要收成同一条"这份产物不可比对"，否则界面拿到的是一句 Python 异常文本。
_BROKEN_PACKAGE: Final = (OSError, KeyError, ValueError, zipfile.BadZipFile)


@dataclass(frozen=True)
class DocumentDiff:
    """两份产出之间的差异（**可序列化**：服务端直接搬进响应体）。"""

    mode: DiffMode
    baseline_ref: str = ""
    candidate_ref: str = ""
    #: 恒为 ``True``：三种模式都先经 ``ooxml.normalize``。留在载荷里是为了让界面能
    #: 明确标出"已剔除目录书签 ID 等假差异"（需求 7.5.3 对文本对比的硬要求）。
    normalized: bool = True
    added: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()
    changed: tuple[str, ...] = ()
    #: "关于结论本身"的说明（如"本模式只比结构、不比字节"）。与差异条目**分开**：
    #: 混进 ``changed`` 会让下游分不清"这一条是差异"与"这一条是说明"。
    notes: tuple[str, ...] = ()
    #: 差异条目数达到了 ``qa.compare.MAX_ITEMS``，即可能被截断。
    truncated: bool = False

    @property
    def equal(self) -> bool:
        """两份产出一致。**唯一判据**，界面照抄。"""
        return not (self.added or self.removed or self.changed)

    @property
    def counts(self) -> dict[str, int]:
        return {
            "added": len(self.added),
            "removed": len(self.removed),
            "changed": len(self.changed),
        }


def compare_documents(
    baseline: str | Path,
    candidate: str | Path,
    *,
    mode: str = "structure",
    baseline_ref: str = "",
    candidate_ref: str = "",
) -> DocumentDiff:
    """比对两份 docx 产物（``mode`` 决定分辨率）。

    Parameters
    ----------
    baseline / candidate:
        两份产出的**文件路径**（服务端从产物登记解析出来的绝对路径）。
    mode:
        :data:`COMPARE_MODES` 之一。取值本应先由服务端请求模型约束（``Literal``），
        这里的校验是兜底——绕过 HTTP 直接调本函数的调用方也该得到一句人话。
    baseline_ref / candidate_ref:
        界面上"哪两份在比"的标识（产物 id），原样带进结果。

    Raises
    ------
    EngineError
        模式不认识 → ``E_SCHEMA_INVALID``；路径打不开或不是一份完整的 docx →
        ``E_PACKAGE_INCOMPLETE``（均为 422）。报"包不完整"而不是 500：请求里点名的
        两份产物里有一份坏了，这是**可操作**的（重新产出那一份），而 500 只会说
        "服务器内部错误"，把一条能给出动作的失败降级成一句废话。
    """
    if mode not in COMPARE_MODES:
        raise EngineError(
            ErrorCode.SCHEMA_INVALID,
            f"不支持的比对模式：{mode}",
            where={"mode": mode, "supported": list(COMPARE_MODES)},
            remediation=["改用 structure（结构快照）/ text（文本）/ contract（契约字段）"],
        )

    try:
        with (
            DocxPackage.open(baseline, read_only=True) as base_pkg,
            DocxPackage.open(candidate, read_only=True) as cand_pkg,
        ):
            result = compare(
                base_pkg,
                cand_pkg,
                mode=cast(DiffMode, mode),
                baseline_ref=baseline_ref,
                candidate_ref=candidate_ref,
            )
    except _BROKEN_PACKAGE as exc:
        raise EngineError(
            ErrorCode.PACKAGE_INCOMPLETE,
            "两份产物中有一份不是可解析的 docx，无法比对",
            where={
                "baseline_ref": baseline_ref,
                "candidate_ref": candidate_ref,
                "reason": type(exc).__name__,
            },
            remediation=["确认这两份都是本系统产出的 docx", "或重新产出坏掉的那一份"],
        ) from exc

    snapshot = result.snapshot
    return DocumentDiff(
        mode=result.mode,
        baseline_ref=snapshot.baseline_ref,
        candidate_ref=snapshot.candidate_ref,
        normalized=snapshot.normalized,
        added=tuple(snapshot.added),
        removed=tuple(snapshot.removed),
        changed=tuple(snapshot.changed),
        notes=tuple(result.notes),
        truncated=any(
            len(items) >= MAX_ITEMS
            for items in (snapshot.added, snapshot.removed, snapshot.changed)
        ),
    )
