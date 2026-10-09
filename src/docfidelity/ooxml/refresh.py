"""域结果回读与 Word（COM）刷新（DD-4.6）。

为什么刷新必须**非破坏性**
--------------------------
``Word.Document.Save()`` 不是"把算好的页码写下来"，而是**整包重写**：
``word/styles.xml`` 会被换成 Word 自己的紧凑 styleId 方案（``a`` / ``a0`` / ``-61``…），
模板里写死的 ``Normal`` / ``Heading1`` 随之消失，``word/document.xml`` 一并重排。

那份产物对 Word 完全合法，但对我们有三重破坏：

* "样式层零改动 / 模板保真"当场失效——产物不再是模板的产物；
* **本机装没装 Word 成了产物字节的输入**，NFR-02 的确定性随之失效，
  golden 快照在两种机器上必然不同；
* 下游 QA 按模板口径核对时全线误报（未定义样式 ID / 产物段落数 / 分节外分页点）。

故 :func:`refresh_with_office` 只在**一份临时副本**上让 Word 算，算完把每个目录条目的
``PAGEREF`` 域结果读回来，写进**我们自己的**包。模板部件一个字节不动。

这条约束曾经被违反过，症状值得记下来：产物**能被 Word 打开**（Word 自己写的），
所以"打不开"这种最直观的检查抓不到它；而离线检查（lxml 良构 / python-docx 可读 /
包可解压）本来就抓不住它。当时暴露出来的是一句看起来毫不相干的判定——
"产物引用了 42 个未定义的样式 ID"——因为 QA 手里那份样式表已经不是模板的了。
"""

from __future__ import annotations

import importlib.util
import os
import re
import shutil
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from docfidelity.contracts.errors import EngineError

from .doc import Element, qn, set_text
from .fields import FIELD_BEGIN, FIELD_END, FIELD_SEPARATE, field_marker
from .package import DOCUMENT_PART, DocxPackage

#: ``PAGEREF`` 域指令——目录条目的页码就是它的结果，也是域刷新唯一要回读的东西。
_PAGEREF_FIELD_RE = re.compile(r"^\s*PAGEREF\b")


# --------------------------------------------------- 域结果回读（刷新专用）
@dataclass
class _FieldFrame:
    """一个尚未闭合的域（``begin`` 已见到、``end`` 还没）。"""

    instruction: str = ""
    collecting: bool = False
    texts: list[Element] = field(default_factory=list)


@dataclass(frozen=True)
class FieldResult:
    """段落里一个域的结果文本，以及承载它的 ``w:t`` 节点。"""

    instruction: str
    text: str
    nodes: tuple[Element, ...]


def field_results(paragraph: Element) -> list[FieldResult]:
    """按文档顺序读出段落里每个域的结果文本与承载它的 ``w:t`` 节点。

    配对**按指令**而不是按嵌套深度：目录条目的 ``PAGEREF`` 既可能挂在段落层
    （``begin`` 在段落里、``end`` 也在段落里），也可能嵌在跨条目的 ``TOC`` 域内的
    ``w:hyperlink`` 里——后者的深度随"条目是第几条"而变，用固定深度筛必然漏。
    """
    stack: list[_FieldFrame] = []
    results: list[FieldResult] = []
    for run in paragraph.iter(qn("w:r")):
        marker = field_marker(run)
        if marker == FIELD_BEGIN:
            stack.append(_FieldFrame())
            continue
        if marker == FIELD_END:
            if not stack:
                continue
            frame = stack.pop()
            if frame.collecting:
                results.append(
                    FieldResult(
                        instruction=frame.instruction,
                        text="".join(node.text or "" for node in frame.texts),
                        nodes=tuple(frame.texts),
                    )
                )
            continue
        if stack:
            for node in run.iter(qn("w:instrText")):
                stack[-1].instruction += node.text or ""
        if marker == FIELD_SEPARATE:
            if stack and _PAGEREF_FIELD_RE.match(stack[-1].instruction):
                stack[-1].collecting = True
            continue
        if stack and stack[-1].collecting:
            stack[-1].texts.extend(run.iter(qn("w:t")))
    return results


def toc_entry_results(pkg: DocxPackage, *, part: str = DOCUMENT_PART) -> list[FieldResult]:
    """按文档顺序读出目录条目的 ``PAGEREF`` 域结果（一个条目一条）。

    **只读路径**：用 ``read_tree``，不改字节。要写回页码请另调
    :func:`apply_toc_page_numbers`——它负责置脏。
    """
    found: list[FieldResult] = []
    for paragraph in pkg.part(part).read_tree.iter(qn("w:p")):
        matched = [
            item for item in field_results(paragraph) if _PAGEREF_FIELD_RE.match(item.instruction)
        ]
        if matched:
            found.append(matched[-1])
    return found


def apply_toc_page_numbers(
    pkg: DocxPackage, numbers: Sequence[str], *, part: str = DOCUMENT_PART
) -> int:
    """把算好的页码写回目录条目的 ``PAGEREF`` 域结果；返回改写的条目数。

    条目数与 ``numbers`` 数量不一致时**一处不改**并返回 0：宁可留着占位页码
    （``w:updateFields`` 会让 Word 打开时自己算一遍），也不要把 A 条目的页码写到
    B 条目上——那种错法页面上看不出来，读者看到的每个页码都是对的数字、错的位置。
    """
    entries = toc_entry_results(pkg, part=part)
    if not entries or len(entries) != len(numbers):
        return 0
    changed = 0
    for entry, number in zip(entries, numbers, strict=False):
        if not entry.nodes or entry.text == number:
            continue
        set_text(entry.nodes[0], number)
        for node in entry.nodes[1:]:
            set_text(node, "")
        changed += 1
    if changed:
        pkg.part(part).mark_dirty()
    return changed


def _read_toc_numbers(path: Path) -> list[str] | None:
    """从 Word 存下来的副本里读出目录页码；读不出来返回 ``None``。"""
    try:
        with DocxPackage.open(path, read_only=True) as pkg:
            return [item.text.strip() for item in toc_entry_results(pkg)]
    except EngineError:
        return None


def _write_toc_numbers(path: Path, numbers: Sequence[str]) -> bool:
    """把页码写进**我们自己的**产物包（只碰 ``document.xml`` 里那几个 ``w:t``）。"""
    try:
        with DocxPackage.open(path, read_only=False) as pkg:
            if len(toc_entry_results(pkg)) != len(numbers):
                return False
            if apply_toc_page_numbers(pkg, numbers) == 0:
                # 页码已经一致（或本就没有目录条目）：不必为了"写一次"去重压一次包。
                return True
            pkg.save(path)
    except EngineError:
        return False
    return True


# --------------------------------------------------------------- Office 降级
def office_available() -> bool:
    """本机是否具备可用的 Word（COM）——域刷新的前置条件。

    探测只做一次，且是**唯一**的一处实现：``render`` 的刷新步骤要在调用前先知道
    "能否刷新"（好决定是降级还是真刷），而 :func:`refresh_with_office` 自己也要
    先判一次。两处各写一遍平台/导入判断，迟早会漂移成"探测说有、调用说没有"。
    """
    if sys.platform != "win32":
        return False
    try:
        return importlib.util.find_spec("win32com.client") is not None
    except (ImportError, ValueError):
        # 父包存在但自身导入期报错（残缺安装）时 ``find_spec`` 会抛；
        # 那不是"可用"，如实返回 False。
        return False


def refresh_with_office(path: str | Path) -> bool:
    """用本地 Word 刷新域与目录；**失败返回 ``False``，不抛异常**（DD-4.6）。

    刷新在产物的**临时副本**上做，只有目录页码会被回读进产物（理由见模块文档）。
    ``Word.Document.Save()`` 会整包重写并把模板样式压成 Word 自己的 styleId 方案——
    直接让它写回产物，等于把"本机有没有 Word"变成产物字节的输入。

    返回布尔而不是抛错是刻意的：域刷新是**增强**，不是构建的必要条件。
    在没有 Office 的服务器上抛异常会让整个构建失败，而正确的行为是产出一个
    "目录需在 Word 中刷新"的产物，并把提示写进 QA 报告。

    签名与 DD-4.6 一致（只有 ``path``）：调用不带超时，"Word 卡住"这件事本函数
    兜不住，如实上抛给调用方的进程模型，不在签名里许诺一个做不到的保证。
    """
    if not office_available():
        return False
    try:
        import pythoncom
        import win32com.client
    except ImportError:  # pragma: no cover - 前置探测已覆盖，这里只是兜底
        return False

    target = Path(path).resolve()
    if not target.is_file():
        return False

    try:
        handle, scratch_name = tempfile.mkstemp(prefix="docfidelity-refresh-", suffix=".docx")
        os.close(handle)
        scratch = Path(scratch_name)
        shutil.copyfile(target, scratch)
    except OSError:
        return False

    application = None
    document = None
    initialized = False
    computed: list[str] | None = None
    try:
        pythoncom.CoInitializeEx(pythoncom.COINIT_APARTMENTTHREADED)
        initialized = True
        application = win32com.client.DispatchEx("Word.Application")
        application.Visible = False
        application.DisplayAlerts = 0
        document = application.Documents.Open(str(scratch))
        document.Fields.Update()
        for table_of_contents in document.TablesOfContents:
            table_of_contents.Update()
        document.Save()
        computed = _read_toc_numbers(scratch)
    except Exception:
        # 降级契约要求这里吞掉一切异常：Office 未安装、文档被占用、宏安全提示
        # 都会以各种 COM 异常形式出现，而它们都不应让构建失败。
        computed = None
    finally:
        _close_quietly(document, application)
        if initialized:
            pythoncom.CoUninitialize()
        scratch.unlink(missing_ok=True)

    if computed is None:
        return False
    return _write_toc_numbers(target, computed)


def _close_quietly(document: object, application: object) -> None:
    """静默清理 Word 进程。清理失败没有补救手段，也不应覆盖主流程结论。"""
    for handle, action in ((document, "close"), (application, "quit")):
        if handle is None:
            continue
        try:
            if action == "close":
                handle.Close(False)  # type: ignore[attr-defined]
            else:
                handle.Quit()  # type: ignore[attr-defined]
        except Exception:
            pass


def degrade_note(*, artifact: str | None = None, reason: str | None = None) -> str:
    """生成"目录需在 Word 中刷新"的提示文本，供 QA 报告与界面呈现。"""
    target = f"（{artifact}）" if artifact else ""
    detail = f"原因：{reason}。" if reason else ""
    return (
        f"目录页码尚未刷新{target}。{detail}"
        "产物可在 Word 中打开并按 Ctrl+A、F9 刷新域，或在本机安装 Office 后重新构建。"
    )


__all__ = [
    "FieldResult",
    "apply_toc_page_numbers",
    "degrade_note",
    "field_results",
    "office_available",
    "refresh_with_office",
    "toc_entry_results",
]
