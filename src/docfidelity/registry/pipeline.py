"""入库流水线编排（DD-7.1）：阶段化、可中断可续跑。

失败处理是分级的，不是"一错全停"
--------------------------------
DD-7.1 对每个阶段规定了失败姿态，本模块严格照做，并且把姿态差异表达在**代码结构**里
而不是注释里：

============================  ==================================================
``unpack``                    失败即整轮中止，但**允许"仅登记原件、稍后重试"**
                              ——错误本身致命（``E_TEMPLATE_UNREADABLE``），
                              返回的结果标记 ``register_only=True``（P05 状态要求）。
``styles`` / ``page`` /       部分失败 → 记 Finding，**继续**。宁可产出一个
``numbering`` / ``blocks``    "样式少了一条"的模板包并明确告知，也不要因为一条
                              样式畸形就拒绝整份模板——后者使用者完全无从下手。
``outline`` / ``slots``       结构识别不出 → 低置信进待确认队列（不失败）。
``placeholders``              无失败姿态（读不到就是没有）。
``fingerprint``               失败仍产出包，只是该包**不参与版本比对**
                              （没有指纹就无从比）。包本身对生成期依然可用，
                              所以不因此中止；缺指纹由 ``verify_fingerprint`` 报出。
``package_io``                写盘失败 / 版本冲突 → 交界面选择（``E_VERSION_CONFLICT``）。
============================  ==================================================
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TypeVar

from docfidelity.contracts.errors import EngineError, EngineErrorInfo, ErrorCode
from docfidelity.contracts.qa import Finding
from docfidelity.contracts.template import (
    BlockIndex,
    Margins,
    NumberingContract,
    OutlineSkeleton,
    PageSetupContract,
    PageSize,
    PlaceholderList,
    StyleContract,
    TemplateManifest,
    TemplatePackage,
    UsedStylesContract,
)
from docfidelity.ooxml import read_numbering, read_styles, used_styles_in_package

from . import parser
from .fingerprint import compute_fingerprint, source_digest
from .package_io import PackageWriteReport, write_package

#: 阶段级失败的 Finding 规则名
PIPELINE_RULE_ID: str = "REG-PIPELINE"

#: 阶段顺序（DD-7.1）。``slots`` 并入 ``outline``：槽位由骨架推导，拆成两阶段
#: 只会让"骨架改了但槽位没跟上"这种不一致有机会出现。
STAGE_ORDER: tuple[str, ...] = (
    "unpack",
    "styles",
    "used_styles",
    "page",
    "numbering",
    "outline",
    "blocks",
    "placeholders",
    "fingerprint",
    "package_io",
)

T = TypeVar("T")


@dataclass
class StageOutcome:
    """一个阶段的执行结果。"""

    stage: str
    ok: bool
    error: EngineErrorInfo | None = None
    note: str | None = None


@dataclass
class IngestResult:
    """一轮入库的完整结果。

    ``register_only=True`` 表示**只登记了原件**、没有契约：后续可对同一条记录
    重试入库（P05 状态要求），界面此时只展示"原件已收到，解析未完成"。
    """

    stage_outcomes: list[StageOutcome] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    snapshot: parser.SourceSnapshot | None = None
    identity: parser.TemplateIdentity | None = None
    package: TemplatePackage | None = None
    register_only: bool = False
    write_report: PackageWriteReport | None = None

    @property
    def ok(self) -> bool:
        return self.package is not None and not self.register_only

    @property
    def pending_review(self) -> list[Finding]:
        """需要人工确认的条目（低置信），界面据此渲染待确认队列。"""
        return list(self.findings)

    def failed_stages(self) -> list[StageOutcome]:
        return [outcome for outcome in self.stage_outcomes if not outcome.ok]


def ingest_template(
    source: str | Path,
    *,
    template_id: str | None = None,
    version: str | None = None,
    doc_type: str | None = None,
    notes: str | None = None,
    target_dir: str | Path | None = None,
    block_assets: dict[str, bytes] | None = None,
) -> IngestResult:
    """把一个模板原件解析成契约；给了 ``target_dir`` 就顺手写出模板包。

    ``doc_type`` / ``notes`` 是**入库时由人补的属性**，不是从文档里解析出来的：
    模板面向哪种文档类型由档案制度决定（同一份模板可以被不同档案复用），
    备注更是纯人工输入。它们随 ``source_filename`` 一起写进清单，且**不参与**
    ``package_hash``（``HASHED_PATHS`` 排除清单）——否则"给这份模板加一句备注"
    会被显示成一次模板内容变更。
    """
    result = IngestResult()

    # ``unpack`` 在 DD-7.1 里是**一个**阶段（产出"部件清单、原件快照"），因此只记
    # 一条 outcome。但它内部有两个可独立失败的动作，顺序是刻意的：**先取快照**，
    # 因为"仅登记原件、稍后重试"靠的就是快照——打开失败时它必须已经在手上。
    snapshot, snapshot_error = _try_snapshot(source)
    if snapshot is None:
        _record_failure(result, "unpack", snapshot_error)
        result.register_only = True
        return result
    result.snapshot = snapshot

    handle = _stage(result, "unpack", lambda: parser.open_template(source), default=None)
    if handle is None:
        result.register_only = True
        return result

    identity, identity_findings = parser.detect_identity(
        handle, snapshot, template_id=template_id, version=version
    )
    result.identity = identity
    result.findings.extend(identity_findings)

    styles = _stage(result, "styles", lambda: read_styles(handle), default=list[StyleContract]())
    used_styles = _stage(
        result,
        "used_styles",
        lambda: used_styles_in_package(handle),
        default=UsedStylesContract(),
    )
    page_setup = _stage(
        result,
        "page",
        lambda: parser.parse_page_setup(handle),
        default=_fallback_page_setup(),
    )
    numbering = _stage(
        result, "numbering", lambda: read_numbering(handle), default=NumberingContract()
    )

    outline, outline_findings = _stage(
        result,
        "outline",
        lambda: parser.parse_outline(handle),
        default=(OutlineSkeleton(), list[Finding]()),
    )
    blocks, block_findings = _stage(
        result,
        "blocks",
        lambda: parser.parse_blocks(handle),
        default=(BlockIndex(), list[Finding]()),
    )
    placeholders = _stage(
        result,
        "placeholders",
        lambda: parser.parse_placeholders(handle),
        default=PlaceholderList(),
    )
    result.findings.extend(outline_findings)
    result.findings.extend(block_findings)

    package = TemplatePackage(
        manifest=TemplateManifest(
            template_id=identity.template_id,
            version=identity.version,
            doc_type=doc_type,
            source_hash=snapshot.sha256,
            package_hash="",  # 由 write_package 计算并回填
            created_at=datetime.now(timezone.utc),
            source_filename=snapshot.filename,
            notes=notes,
        ),
        styles=styles,
        page_setup=page_setup,
        numbering=numbering,
        used_styles=used_styles,
        outline=outline,
        blocks=blocks,
        placeholders=placeholders,
        fingerprint=None,
    )
    # 指纹必须在写盘**之前**算出来：``write_package`` 只在 ``fingerprint`` 非空时
    # 落 ``fingerprint.json``，所以顺序反了的话，指纹永远只存在于内存里。
    fingerprint = _stage(
        result,
        "fingerprint",
        lambda: compute_fingerprint(
            package,
            block_assets=block_assets,
            source_digest_value=source_digest(handle),
        ),
        default=None,
    )
    if fingerprint is not None:
        # 用 ``model_copy`` 而不是原地赋值：契约模型一律视为不可变，
        # 就地改会让"这份包和它的指纹是同时产出的"这条保证出现例外。
        package = package.model_copy(update={"fingerprint": fingerprint})
    result.package = package

    if target_dir is None:
        result.stage_outcomes.append(
            StageOutcome(stage="package_io", ok=True, note="未指定输出目录")
        )
        return result
    result.write_report = _stage(
        result,
        "package_io",
        lambda: write_package(package, target_dir, block_assets=block_assets),
        default=None,
    )
    return result


def parse_template(
    source: str | Path,
    *,
    template_id: str | None = None,
    version: str | None = None,
    doc_type: str | None = None,
    notes: str | None = None,
) -> TemplatePackage:
    """解析一份模板原件，返回模板包契约（**不落库**）。

    这是 :func:`ingest_template` 的**工具门面**（需求 5.5 的 ``template_parse``）：
    入口是扁平的 JSON 面（原件路径 + 三个入库属性），出口是契约模型，正好落在
    "模型的动作空间 = 输入输出皆为可序列化 Pydantic 模型"这条纪律上。

    与 :func:`ingest_template` 的分工是**只解析、不写盘**：落库仍由
    :func:`~docfidelity.registry.package_io.write_package` 承担（对应 ``registry_put``
    工具）。这样"写盘"这个高危动作只留在需要审批的那一个工具上，而不是顺带藏在
    解析工具的可选参数里——后者会让"解析一下"与"往库里写一份"共用一个调用面。

    Raises
    ------
    EngineError
        原件打不开或解析不出契约。**绝不返回半成品**：调用方拿到一个"空包"会把它
        当成"这份模板本来就没有内容"，而真正的失败是"原件根本没打开"。
    """
    result = ingest_template(
        source,
        template_id=template_id,
        version=version,
        doc_type=doc_type,
        notes=notes,
    )
    if result.package is None:
        failed = next((outcome for outcome in result.stage_outcomes if not outcome.ok), None)
        code = (
            failed.error.code
            if failed is not None and failed.error is not None
            else ErrorCode.TEMPLATE_UNREADABLE
        )
        raise EngineError(
            code,
            f"模板原件解析未产出契约：{Path(source).name}",
            where={
                "source": str(source),
                "failed_stage": failed.stage if failed is not None else "unpack",
            },
            remediation=[
                "确认该文件是可打开的 .docx，且确为模板原件",
                "查看入库阶段的报错明细，定位缺了哪一分区",
            ],
        )
    return result.package


def _try_snapshot(
    source: str | Path,
) -> tuple[parser.SourceSnapshot | None, EngineError | None]:
    """尝试取原件快照，把失败**返回**而不是抛出。

    返回而不是抛出，是因为调用方要决定"要不要记为 unpack 阶段的 outcome"：
    快照失败与打开失败同属 ``unpack``，记两条同名 outcome 会让阶段顺序与
    DD-7.1 的对不上，也会让"哪一步真的失败了"变得含糊。
    """
    try:
        return parser.snapshot_source(source), None
    except EngineError as error:
        return None, error


def _record_failure(result: IngestResult, stage: str, error: EngineError | None) -> None:
    if error is None:
        return
    result.stage_outcomes.append(StageOutcome(stage=stage, ok=False, error=error.to_info()))
    result.findings.append(_stage_finding(stage, error))


def _stage_finding(stage: str, error: EngineError) -> Finding:
    return Finding(
        rule_id=PIPELINE_RULE_ID,
        severity="major",
        message=f"{stage} 阶段未能完成：{error.message}",
        suggestion=error.remediation[0] if error.remediation else None,
    )


def _stage(
    result: IngestResult,
    stage: str,
    thunk: Callable[[], T],
    *,
    default: T,
) -> T:
    """执行一个阶段：失败转 Finding 并继续，成功则记一条 outcome。

    ``EngineError`` 之外的异常**不吞**——一个 ``TypeError`` 说明代码有 bug，
    把它伪装成"阶段失败"会让 bug 永远藏在待确认队列里，没人会去翻。
    """
    try:
        value = thunk()
    except EngineError as error:
        _record_failure(result, stage, error)
        return default
    result.stage_outcomes.append(StageOutcome(stage=stage, ok=True))
    return value


def _fallback_page_setup() -> PageSetupContract:
    """版心解析失败时的兜底值。

    用 Letter + 1 英寸页边距而不是"没有版心"：几何计算到处都要版心，
    给它一个 ``None`` 会让后面每个调用点都得判空，而判空写漏一处就是崩溃。
    解析失败这件事已经通过 Finding 明确告知了。
    """
    return PageSetupContract(
        page_size=PageSize(width_twips=12240, height_twips=15840),
        margins=Margins(top_twips=1440, bottom_twips=1440, left_twips=1440, right_twips=1440),
    )


__all__: list[str] = [
    "PIPELINE_RULE_ID",
    "STAGE_ORDER",
    "IngestResult",
    "StageOutcome",
    "ingest_template",
]
