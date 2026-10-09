"""渲染目检：``docx`` → PDF → PNG，按关键词定位页面（DD-10.5 / FR-M5-12）。

按关键词定位，不按页数猜
------------------------
需求 1.2 第 6 条与硬规则 4 是同一条：**禁止以页数间接判断**。页数由排版引擎决定，
同一份内容在换模板、换字号、换 Office 版本后都会变；按"第 3 页"去抽图，抽到的
往往是别的东西，而报告上写着"已目检"。故本模块只做一件事：把每页渲染成文本，
**按关键词命中**挑页——命不中就不给页，且不给页这件事会被如实报出来。

降级是**结论的一部分**，不是异常
--------------------------------
容器里没有 Word、也没有 LibreOffice 是常态（DD-20 风险 3）。此时本模块返回
``degraded=True`` 加一个**能读的原因**，而不是抛错、也不是返回空结果：
"没做目检"必须与"目检通过"在报告里可区分（与契约 ``Report`` 对"门禁未执行 vs
门禁全通过"的同一条纪律）。

''一个渲染器都没有'' 与 ''渲染器坏了'' 也是两件事：前者是环境事实（降级），
后者是缺陷（照样降级，但原因里带上异常类型，免得把"soffice 崩了"记成"没装"）。
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from docfidelity.ooxml import office_available

__all__ = [
    "SPOTCHECK_RULE_ID",
    "SpotcheckResult",
    "default_converter",
    "default_extractor",
    "office_converter",
    "soffice_converter",
    "spotcheck",
]

#: 目检的规则编号（附录 C：FR-M5-12 渲染目检 | spotcheck | major）。
SPOTCHECK_RULE_ID: Final = "FR-M5-12"

#: 默认最多抽几页（超出只报计数：目检是"抽看"，不是"逐页审"）。
DEFAULT_MAX_PAGES: Final = 8

#: 转换超时（秒）。渲染服务卡住时不能把构建一起挂死。
_TIMEOUT_SECONDS: Final = 180

#: ``docx -> pdf`` 转换器：返回 PDF 路径；不可用 / 失败返回 ``None``。
Converter = Callable[[Path, Path], "Path | None"]
#: ``pdf -> 每页文本`` 抽取器：返回按页顺序的文本；不可用返回 ``None``。
Extractor = Callable[[Path], "list[str] | None"]


@dataclass
class SpotcheckResult:
    """目检结果。``degraded`` 为真时 ``reason`` 必须非空（DD-10.5）。"""

    degraded: bool = False
    reason: str | None = None
    pages: list[int] = field(default_factory=list)
    pages_total: int = 0
    keywords: list[str] = field(default_factory=list)
    images: list[str] = field(default_factory=list)

    def as_payload(self) -> dict[str, Any]:
        return {
            "degraded": self.degraded,
            "reason": self.reason,
            "pages": list(self.pages),
            "pages_total": self.pages_total,
            "keywords": list(self.keywords),
            "images": list(self.images),
        }


# ------------------------------------------------------------------ 转换器
def soffice_converter(docx: Path, out_dir: Path) -> Path | None:
    """用 LibreOffice 转 PDF；未安装则返回 ``None``。"""
    binary = shutil.which("soffice") or shutil.which("libreoffice")
    if binary is None:
        return None
    try:
        completed = subprocess.run(
            [binary, "--headless", "--convert-to", "pdf", "--outdir", str(out_dir), str(docx)],
            capture_output=True,
            timeout=_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    produced = out_dir / f"{docx.stem}.pdf"
    return produced if produced.is_file() else None


def office_converter(docx: Path, out_dir: Path) -> Path | None:
    """用 Word COM 转 PDF；未安装 Word（非 Windows / 无 pywin32）则返回 ``None``。"""
    if not office_available():
        return None
    target = out_dir / f"{docx.stem}.pdf"
    try:
        import pythoncom
        import win32com.client

        application = None
        document = None
        initialized = False
        try:
            pythoncom.CoInitializeEx(pythoncom.COINIT_APARTMENTTHREADED)
            initialized = True
            application = win32com.client.DispatchEx("Word.Application")
            application.Visible = False
            application.DisplayAlerts = 0
            document = application.Documents.Open(str(docx))
            document.SaveAs(str(target), FileFormat=17)  # 17 = wdFormatPDF
        finally:
            try:
                if document is not None:
                    document.Close(False)
            finally:
                try:
                    if application is not None:
                        application.Quit()
                finally:
                    if initialized:
                        pythoncom.CoUninitialize()
    except Exception:  # COM 调用失败一律按"不可用"处理，原因由调用方决定
        return None
    return target if target.is_file() else None


def default_converter(docx: Path, out_dir: Path) -> Path | None:
    """默认转换链：Word COM 优先（保真度更高），退回 LibreOffice。"""
    for converter in (office_converter, soffice_converter):
        produced = converter(docx, out_dir)
        if produced is not None:
            return produced
    return None


def default_extractor(pdf: Path) -> list[str] | None:
    """用 PyMuPDF 抽取每页文本；未安装则返回 ``None``。"""
    try:
        import fitz
    except ImportError:
        return None
    try:
        with fitz.open(str(pdf)) as document:
            return [page.get_text() for page in document]
    except Exception:
        return None


# ------------------------------------------------------------------ 目检
def spotcheck(
    docx: str | Path,
    *,
    keywords: Sequence[str] = (),
    max_pages: int = DEFAULT_MAX_PAGES,
    work_dir: str | Path | None = None,
    converter: Converter | None = None,
    extractor: Extractor | None = None,
    render_images: bool = False,
) -> SpotcheckResult:
    """把 ``docx`` 渲染成 PDF 并按关键词挑页（DD-10.5）。

    ``converter`` / ``extractor`` 可注入，使本函数在**没有 Office 的 CI** 上也能被
    测试覆盖：注入一个假转换器即可验证"挑页逻辑正确"，注入一个返回 ``None`` 的
    转换器即可验证"降级带原因"。
    """
    path = Path(docx)
    if not path.is_file():
        return SpotcheckResult(
            degraded=True, reason=f"待目检的文件不存在：{path}", keywords=list(keywords)
        )

    out_dir = Path(work_dir) if work_dir is not None else path.parent
    out_dir.mkdir(parents=True, exist_ok=True)

    convert = converter or default_converter
    pdf = convert(path, out_dir)
    if pdf is None:
        return SpotcheckResult(
            degraded=True,
            reason="未找到可用的 docx→PDF 转换器（Word COM 与 LibreOffice 均不可用）",
            keywords=list(keywords),
        )

    extract = extractor or default_extractor
    page_texts = extract(pdf)
    if page_texts is None:
        return SpotcheckResult(
            degraded=True,
            reason="无法抽取 PDF 文本（未安装 PyMuPDF 或该 PDF 不可解析）",
            keywords=list(keywords),
        )

    wanted = [word for word in keywords if word]
    picked = [
        index
        for index, text in enumerate(page_texts, start=1)
        if not wanted or any(word in text for word in wanted)
    ]
    images: list[str] = []
    if render_images and picked:
        images = _render_pages(pdf, picked[:max_pages], out_dir, stem=path.stem)

    return SpotcheckResult(
        degraded=False,
        reason=None,
        pages=picked[:max_pages],
        pages_total=len(page_texts),
        keywords=list(keywords),
        images=images,
    )


def _render_pages(pdf: Path, pages: Sequence[int], out_dir: Path, *, stem: str) -> list[str]:
    """把指定页渲染成 PNG；PyMuPDF 不可用或失败时返回空列表（不降级整个目检）。"""
    try:
        import fitz
    except ImportError:
        return []
    saved: list[str] = []
    try:
        with fitz.open(str(pdf)) as document:
            for number in pages:
                page = document[number - 1]
                target = out_dir / f"{stem}.p{number}.png"
                page.get_pixmap().save(str(target))
                saved.append(str(target))
    except Exception:
        return saved
    return saved
