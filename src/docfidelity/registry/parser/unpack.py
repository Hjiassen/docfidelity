"""入库流水线第 1 阶段：拆包、原件快照与身份识别（DD-7.1）。

失败语义
--------
非 docx / 包损坏 → ``E_TEMPLATE_UNREADABLE``。按 DD-7.1，该失败**允许"仅登记原件、
稍后重试"**：错误本身是致命的，但流水线会捕获它并落一条"只有原件、没有契约"的
登记记录（P05 的状态要求），所以这里不需要把它降级成可降级错误。

身份识别是启发式的
------------------
``template_id`` 与 ``version`` 是「封面 / 页眉上的文件编号」这类**人工约定**，
不同单位写法各异，任何识别都只能尽力而为。因此本模块：

* 显式传入的值**永远优先**于识别结果；
* 识别失败时回落到文件名与 ``1.0``，并**同时产出一条 Finding**——低置信必须
  进待确认队列（NFR-26 / 验收 39），不能静默采用一个猜出来的版本号。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

from docfidelity.contracts.errors import EngineError, ErrorCode
from docfidelity.contracts.qa import Finding
from docfidelity.ooxml import DocxPackage
from docfidelity.platform.hashing import sha256_file

from .common import core_property, first_text_sample, header_footer_parts

#: 身份识别规则名，进待确认队列时作为 rule_id
IDENTITY_RULE_ID: Final = "REG-IDENTITY"

#: 默认版本号。识别不出来时用它，但会同时产出低置信 Finding。
DEFAULT_VERSION: Final = "1.0"

#: 在封面 / 页眉里找版本号时扫描的段落数。封面通常很短，40 段足够覆盖，
#: 又不会扫到正文里去（正文里的 "1.2.3" 是条款号，不是版本号）。
IDENTITY_SCAN_PARAGRAPHS: Final = 40

_VERSION_PATTERN: Final = re.compile(r"[Vv]?\s*(\d+(?:\.\d+){1,3})(?![\d.])")

IdentitySource = Literal["explicit", "document", "filename"]
"""身份的来源，决定置信度。``filename`` 与默认版本号同属低置信。"""


@dataclass(frozen=True)
class SourceSnapshot:
    """模板原件快照。入库第一步就固定下来，后续所有阶段读的都是它。"""

    filename: str
    size_bytes: int
    sha256: str


@dataclass(frozen=True)
class TemplateIdentity:
    """模板身份：``template_id`` + ``version``，以及它们各自是从哪来的。"""

    template_id: str
    version: str
    template_id_source: IdentitySource
    version_source: IdentitySource = "explicit"

    @property
    def is_confident(self) -> bool:
        """两处都来自文档或显式指定时才算高置信。"""
        return self.template_id_source != "filename" and self.version_source != "filename"


def snapshot_source(path: str | Path) -> SourceSnapshot:
    """对模板原件做快照（文件名 + 大小 + 内容摘要）。

    摘要用**原件字节**而不是解析后的结构：它是"这份模板包对应哪一份原件"的
    唯一凭据，导入可追溯性检查（10.11）依赖它。
    """
    source = Path(path)
    if not source.is_file():
        raise EngineError(
            ErrorCode.TEMPLATE_UNREADABLE,
            f"模板文件不存在：{source}",
            where={"path": str(source)},
            remediation=["确认文件路径", "重新上传模板原件"],
        )
    return SourceSnapshot(
        filename=source.name,
        size_bytes=source.stat().st_size,
        sha256=sha256_file(source),
    )


def open_template(path: str | Path) -> DocxPackage:
    """以只读方式打开模板包，并确认它确实是一份 Word 文档。

    ``DocxPackage.open`` 已经能识别 zip 损坏与缺 ``[Content_Types].xml``；
    这里再补一道"有没有 ``word/document.xml``"——一个合法的 OOXML 包也可能是
    只有样式的模板部件（``.dotx`` 之外还可能被手工裁过），那种包解析不出骨架，
    与其在后续阶段报一堆莫名其妙的错，不如在这里就拒掉。
    """
    package = DocxPackage.open(path, read_only=True)
    if not package.has_part("word/document.xml"):
        raise EngineError(
            ErrorCode.TEMPLATE_UNREADABLE,
            "包内缺少 word/document.xml，无法作为模板解析",
            where={"path": str(path)},
            remediation=["确认该文件是 Word 文档（.docx / .dotx）"],
        )
    return package


def detect_identity(
    package: DocxPackage,
    snapshot: SourceSnapshot,
    *,
    template_id: str | None = None,
    version: str | None = None,
) -> tuple[TemplateIdentity, list[Finding]]:
    """识别模板身份，低置信时同时给出进待确认队列的 Finding。

    优先级：显式传入 > 核心属性 / 封面 / 页眉 > 文件名。
    """
    findings: list[Finding] = []

    resolved_id, id_source = _resolve_template_id(package, snapshot, template_id)
    resolved_version, version_source = _resolve_version(package, version)

    identity = TemplateIdentity(
        template_id=resolved_id,
        version=resolved_version,
        template_id_source=id_source,
        version_source=version_source,
    )
    if not identity.is_confident:
        findings.append(
            Finding(
                rule_id=IDENTITY_RULE_ID,
                severity="minor",
                message=(
                    f"模板编号与版本号只有部分来自文档本身（编号来源：{id_source}，"
                    f"版本来源：{version_source}），已按 {resolved_id}@{resolved_version} 暂记"
                ),
                evidence=f"{resolved_id}@{resolved_version}",
                suggestion="在入库时显式指定 template_id 与 version，或按模板体例补齐封面文件编号",
            )
        )
    return identity, findings


def _resolve_template_id(
    package: DocxPackage, snapshot: SourceSnapshot, explicit: str | None
) -> tuple[str, IdentitySource]:
    if explicit:
        return explicit, "explicit"
    for tag in ("subject", "title", "category"):
        value = core_property(package, tag)
        if value:
            return value, "document"
    return Path(snapshot.filename).stem, "filename"


def _resolve_version(package: DocxPackage, explicit: str | None) -> tuple[str, IdentitySource]:
    if explicit:
        return explicit, "explicit"

    # 先看封面（正文前若干段），再看页眉——编号和版本号通常就写在两处之一。
    candidates = [first_text_sample(package, "word/document.xml", IDENTITY_SCAN_PARAGRAPHS)]
    candidates.extend(
        first_text_sample(package, name, IDENTITY_SCAN_PARAGRAPHS)
        for name in header_footer_parts(package)
    )
    for text in candidates:
        match = _VERSION_PATTERN.search(text)
        if match is not None:
            return match.group(1), "document"
    return DEFAULT_VERSION, "filename"


__all__ = [
    "DEFAULT_VERSION",
    "IDENTITY_RULE_ID",
    "IDENTITY_SCAN_PARAGRAPHS",
    "IdentitySource",
    "SourceSnapshot",
    "TemplateIdentity",
    "detect_identity",
    "open_template",
    "snapshot_source",
]
