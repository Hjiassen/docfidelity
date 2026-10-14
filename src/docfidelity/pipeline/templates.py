"""V1 模板用例（DD-7.1 / DD-13.1）；V2 门面位于 ``template_imports``。

覆盖三件事，都是"服务端要模板、但不许认识 ``registry``"这件事的不同侧面：

* **入库**（P05 第 2~5 步）：:func:`import_template` / :func:`summarize` /
  :func:`register_package` / :func:`verify_package`；
* **读回**（P04 详情页）：:func:`read_package` / :func:`read_overrides`；
* **用到已登记的包**（P04 的版本对比与导出）：:func:`diff_packages` /
  :func:`export_package`。

为什么需要这一层
----------------
DD-13.1 对 ``server`` 的硬约束是"不得 import ``ooxml`` / ``registry`` 内部实现"，
而同一节又要求"界面需要的每一步能力都必须由 ``pipeline`` 暴露为用例函数"。模板入库
（P05 第 2~5 步）正是这样一步能力：服务端必须能触发解析、拿到入库报告、把包写进库，
但它一行都不该认识 ``registry``。

本模块因此与 :mod:`.decisions` 对称：``decisions`` 是作业级决策协议
的模块，本模块承接 V1 模板库用例——边界由 ``.importlinter`` 的 forbidden
契约锁住（``pipeline-agents-seam`` / ``pipeline-registry-seam``）。

这一层**不**做什么
------------------
* **不做 HTTP**：不拼响应体、不读 multipart、不建作业，也不定义错误码 → 状态映射。
* **不决定落点**：包写到哪由调用方给（服务端给 ``<root>/imports/<id>/package``）。
* **不判"能不能登记"**：版本冲突怎么处置（改版本 / 覆盖 / 取消）是人的决定，
  本模块只把冲突**报出来**（``E_VERSION_CONFLICT`` 由 ``write_package`` 抛）。
* **不删任何东西**：覆盖登记要清空目标目录，那件事由服务端（持有 ``storage_root``
  的一方）做，见 :mod:`server.templates`。

失败为什么不抛异常
------------------
DD-7.1 给入库定的失败姿态是**分级**的：``unpack`` 失败 → 允许仅登记原件、稍后重试；
其余阶段失败 → 记 Finding 并继续。这条姿态必须能被界面表达成"原件已收到、解析未完成、
可以再试一次"，所以 :func:`import_template` **不抛**任何模板相关失败——最重的
``unpack`` 失败落在 :attr:`TemplateImportResult.register_only` 上，落盘失败落在
``stages`` 里一条 ``package_io`` 失败上（此时 ``package_dir`` 为 ``None``）。
调用方自己的问题（上传闸门、版本冲突、路径越界）由**调用方**在调用前后判定：
本模块既不读 multipart，也不决定落点，更不判"能不能登记"。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Final

from docfidelity import registry
from docfidelity.contracts.errors import EngineErrorInfo
from docfidelity.contracts.qa import Finding
from docfidelity.contracts.template import (
    BlockKind,
    ChapterNode,
    OutlineSkeleton,
    TemplateOverrides,
    TemplatePackage,
)
from docfidelity.platform.fs import zip_directory
from docfidelity.qa import assign_finding_ids

__all__ = [
    "BLOCK_KINDS",
    "ContractSummary",
    "FieldDelta",
    "ImportStage",
    "RegisteredPackage",
    "SectionChange",
    "TemplateImportResult",
    "VersionDiff",
    "diff_packages",
    "export_package",
    "fingerprint_problems",
    "import_template",
    "read_overrides",
    "read_package",
    "register_package",
    "summarize",
    "verify_package",
    "write_overrides",
]

#: 契约摘要卡里**逐类**列出的块类别。取 :data:`BlockKind` 的闭集而不是"出现了哪些就列哪些"：
#: 摘要是一张固定形状的卡，"这次没有目录块"也应当显示为 0，而不是那一行消失
#: （消失会让使用者以为界面忘了统计）。
BLOCK_KINDS: Final[tuple[BlockKind, ...]] = ("cover", "revision", "toc", "header", "table")


@dataclass(frozen=True)
class ImportStage:
    """一个入库阶段的执行结果（P05 第 2 步的阶段进度）。

    与 :class:`docfidelity.registry.IngestResult` 的 ``StageOutcome`` 一一对应，
    但**不带** registry 的类型出门：服务端与前端都不该被迫认识 registry 的数据结构。
    """

    stage: str
    ok: bool
    note: str | None = None
    error: EngineErrorInfo | None = None


@dataclass(frozen=True)
class ContractSummary:
    """契约摘要卡（P05 第 3 步）：**只放能从契约里数出来的量**。

    每个字段都是"数一下"，没有一个需要推断——推断会让摘要与契约本身对不上，
    而这张卡的全部价值就是"它就是契约的样子"。
    """

    style_count: int = 0
    paragraph_style_count: int = 0
    table_style_count: int = 0
    used_paragraph_styles: int = 0
    used_table_styles: int = 0
    numbering_levels: int = 0
    numbering_instances: int = 0
    heading_bindings: int = 0
    block_counts: dict[str, int] = field(default_factory=dict)
    table_prototypes: int = 0
    slot_count: int = 0
    declared_slot_count: int = 0
    chapter_count: int = 0
    max_heading_level: int = 0
    placeholder_count: int = 0
    placeholder_parts: dict[str, int] = field(default_factory=dict)

    @property
    def slot_completion(self) -> float:
        """槽位完成度 = **声明了 ``unit_types`` 的槽位**占全部槽位的比例。

        分母为 0 时返回 1.0 而不是 0.0：一份根本没有槽位的模板（例如只有封面与
        修订记录）不是"完成度为零"，它没有槽位这回事。返回 0.0 会让界面把它显示成
        "什么都没做"，而它其实已经完整解析了。
        """
        if self.slot_count == 0:
            return 1.0
        return self.declared_slot_count / self.slot_count


@dataclass
class TemplateImportResult:
    """一轮入库的完整结果（P05 第 2 步 + 第 3 步的全部数据）。

    ``register_only=True`` 表示**只登记了原件**、没有契约：界面此时只展示
    "原件已收到，解析未完成"，并提供"稍后重试"（同一条记录重跑
    :func:`import_template`），而不是把一次可恢复的解析失败说成不可用。
    """

    source_filename: str
    source_size_bytes: int
    source_hash: str | None
    created_at: datetime
    stages: list[ImportStage] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    package: TemplatePackage | None = None
    package_dir: Path | None = None
    package_hash: str | None = None
    integrity: list[str] = field(default_factory=list)
    register_only: bool = False
    template_id: str = ""
    version: str = ""
    template_id_source: str = ""
    version_source: str = ""

    @property
    def ok(self) -> bool:
        return self.package is not None and not self.register_only

    @property
    def summary(self) -> ContractSummary | None:
        return None if self.package is None else summarize(self.package)

    def failed_stages(self) -> list[ImportStage]:
        return [stage for stage in self.stages if not stage.ok]


def import_template(
    source: str | Path,
    *,
    import_id: str,
    template_id: str | None = None,
    version: str | None = None,
    doc_type: str | None = None,
    notes: str | None = None,
    target_dir: str | Path | None = None,
    block_assets: Mapping[str, bytes] | None = None,
    now: datetime | None = None,
) -> TemplateImportResult:
    """解析一份模板原件；给了 ``target_dir`` 就顺手把模板包写在那里。

    ``import_id`` 是**待确认项标识的前缀**（``<import_id>@<序号>``），与门禁判定的
    ``<report_id>@<序号>`` 同一套编号规则（唯一实现 ``qa.assign_finding_ids``）：
    待确认项要能被人指认、能在两次读取之间保持稳定，"报告里第几条"这种坐标不行
    ——它得由调用方自己数，而重新排序会让同一个坐标指向另一条项。

    模板相关失败**不抛**（见模块文档）：最重的 ``unpack`` 失败落
    :attr:`TemplateImportResult.register_only`，落盘失败落 ``stages``。
    """
    moment = now or datetime.now(tz=timezone.utc)
    outcome = registry.ingest_template(
        source,
        template_id=template_id,
        version=version,
        doc_type=doc_type,
        notes=notes,
        target_dir=target_dir,
        block_assets=dict(block_assets) if block_assets is not None else None,
    )

    result = TemplateImportResult(
        source_filename=(
            outcome.snapshot.filename if outcome.snapshot is not None else Path(source).name
        ),
        source_size_bytes=outcome.snapshot.size_bytes if outcome.snapshot is not None else 0,
        source_hash=outcome.snapshot.sha256 if outcome.snapshot is not None else None,
        created_at=moment,
        stages=[
            ImportStage(stage=item.stage, ok=item.ok, note=item.note, error=item.error)
            for item in outcome.stage_outcomes
        ],
        findings=list(outcome.findings),
        package=outcome.package,
        package_dir=None if outcome.write_report is None else outcome.write_report.target_dir,
        package_hash=None if outcome.write_report is None else outcome.write_report.package_hash,
        register_only=outcome.register_only,
    )
    # 标识要在**出报告之前**赋好：报告一旦交出去就已经被引用，此后再编号等于换了一次坐标。
    assign_finding_ids(result.findings, import_id)

    if outcome.identity is not None:
        result.template_id = outcome.identity.template_id
        result.version = outcome.identity.version
        result.template_id_source = outcome.identity.template_id_source
        result.version_source = outcome.identity.version_source

    # 完整性校验与"解析成功"是**两件事**：解析成功只说明契约产出来了，完整性还看
    # 落在磁盘上的包能不能被读回（缺文件、被外部改过）。只在两个条件都成立时才登记
    # 为"通过"，否则出现"界面说入库成功、生成期读包却报 E_PACKAGE_INCOMPLETE"。
    if result.package_dir is not None:
        result.integrity = registry.verify_package(result.package_dir)
    return result


def summarize(package: TemplatePackage) -> ContractSummary:
    """契约摘要卡的数据。纯计数，不做任何推断。"""
    outline: OutlineSkeleton = package.outline
    blocks = package.blocks.blocks
    counts: dict[str, int] = {
        kind: sum(1 for block in blocks if block.kind == kind) for kind in BLOCK_KINDS
    }
    levels = sum(len(item.levels) for item in package.numbering.abstract_num)
    chapter_levels = _chapter_levels(outline.chapter_tree)
    placeholders = package.placeholders.entries
    parts: dict[str, int] = {}
    for entry in placeholders:
        parts[entry.locator.part] = parts.get(entry.locator.part, 0) + 1
    return ContractSummary(
        style_count=len(package.styles),
        paragraph_style_count=sum(1 for item in package.styles if item.type == "paragraph"),
        table_style_count=sum(1 for item in package.styles if item.type == "table"),
        used_paragraph_styles=len(package.used_styles.p_style),
        used_table_styles=len(package.used_styles.tbl_style),
        numbering_levels=levels,
        numbering_instances=len(package.numbering.num),
        heading_bindings=len(package.numbering.heading_bindings),
        block_counts=dict(counts),
        table_prototypes=counts.get("table", 0),
        slot_count=len(outline.slots),
        declared_slot_count=sum(1 for slot in outline.slots if slot.unit_types),
        chapter_count=len(outline.chapter_tree),
        max_heading_level=max(chapter_levels, default=0),
        placeholder_count=len(placeholders),
        placeholder_parts=dict(parts),
    )


def _chapter_levels(nodes: Sequence[ChapterNode]) -> list[int]:
    """骨架树里实际出现的全部层级（含子节点）。只用于"最深几级"这一个数。"""
    collected: list[int] = []
    for node in nodes:
        collected.append(node.level)
        collected.extend(_chapter_levels(node.children))
    return collected


def read_package(package_dir: str | Path) -> TemplatePackage:
    """读回模板包并做完整性校验（``E_PACKAGE_INCOMPLETE``）。"""
    return registry.read_package(package_dir)


def verify_package(package_dir: str | Path) -> list[str]:
    """完整性校验的问题清单（**空列表 = 通过**）。登记前预检用它一次性列出全部问题。"""
    return registry.verify_package(package_dir)


def read_overrides(package_dir: str | Path) -> TemplateOverrides | None:
    """读包内的人工确认留痕；``None`` 表示从未确认过（见 ``package_io.read_overrides``）。"""
    return registry.read_overrides(package_dir)


def write_overrides(package_dir: str | Path, overrides: TemplateOverrides) -> str:
    """把人工确认留痕写进包内 ``overrides.json``，返回相对路径。"""
    return registry.write_overrides(package_dir, overrides)


@dataclass(frozen=True)
class RegisteredPackage:
    """一次登记写盘的结果。"""

    package: TemplatePackage
    package_dir: Path
    package_hash: str
    written: tuple[str, ...] = ()


@dataclass(frozen=True)
class FieldDelta:
    """一条字段级变更（来自 :func:`diff_packages` 的翻译）。"""

    path: str
    section: str
    category: str
    #: ``added`` / ``removed`` / ``changed`` 三者之一。
    kind: str
    before: str | None = None
    after: str | None = None
    #: 人读的一行描述（判定侧生成，界面直接显示，不自行拼字符串）。
    description: str = ""


@dataclass(frozen=True)
class SectionChange:
    """一个契约分区的增量。"""

    section: str
    #: 该分区改动的影响说明（哪些产出会受影响），由判定侧给出。
    impact: str
    #: 是否构成**硬阻断**（构建前预检据此拒绝）。与 :attr:`changed` 是两件事：
    #: 有的分区改了不影响产出（如备注），有的分区一改就必须重跑。
    blocking: bool
    changed: bool
    changes: list[FieldDelta] = field(default_factory=list)


@dataclass(frozen=True)
class VersionDiff:
    """两个模板版本之间的差异——``pipeline`` 自己拥有的**可序列化**形状。

    为什么要翻译一层而不是直接用 ``registry.TemplateVersionDiff``：后者是内核实现的
    类型，``server`` 不得 import（DD-13.1）。服务端要能把差异渲染成表格，就必须有人
    把它译成契约形状，而那个"有人"只能是接缝（同 :func:`summarize` 对
    ``registry`` 的包摘要做的事）。
    """

    before_ref: str
    after_ref: str
    identical: bool
    #: 没有硬阻断即 ``True``（构建前预检据此决定是否放行）。
    ok: bool
    summary: str
    changelog: list[str] = field(default_factory=list)
    sections: list[SectionChange] = field(default_factory=list)

    @property
    def blocking_sections(self) -> list[str]:
        return [item.section for item in self.sections if item.blocking]

    @property
    def changed_sections(self) -> list[str]:
        return [item.section for item in self.sections if item.changed]


def register_package(
    source_dir: str | Path,
    target_dir: str | Path,
    *,
    template_id: str,
    version: str,
    overrides: TemplateOverrides | None = None,
) -> RegisteredPackage:
    """把暂存的模板包按最终身份写进模板库。

    ``template_id`` / ``version`` 与暂存包里的清单不同时**以参数为准**并重写清单：
    这正是 P05 第 5 步"重复登记时选择新版本"的实现——人可以在确认登记这一刻把
    版本号改掉，而不必重新解析一遍（解析结果与版本号无关）。

    清单里的 ``package_hash`` 由 ``write_package`` 重算，而**内容摘要不含清单**，
    所以改版本号不会改变 ``package_hash``——这不是巧合，是"版本号是引用键、
    不是内容"这条设计的直接结果。

    Raises
    ------
    EngineError
        ``E_VERSION_CONFLICT``：``target_dir`` 已有内容。**不静默覆盖**：调用方
        若要覆盖，必须先显式清空目标目录（见 :mod:`server.templates` 的处置说明）。
        ``E_PACKAGE_INCOMPLETE``：暂存包读不回来。
    """
    package = registry.read_package(source_dir)
    if package.manifest.template_id != template_id or package.manifest.version != version:
        package = package.model_copy(
            update={
                "manifest": package.manifest.model_copy(
                    update={"template_id": template_id, "version": version}
                )
            }
        )
    report = registry.write_package(
        package, target_dir, block_assets=registry.read_block_assets(source_dir)
    )
    if overrides is not None:
        registry.write_overrides(report.target_dir, overrides)
    return RegisteredPackage(
        package=package,
        package_dir=report.target_dir,
        package_hash=report.package_hash,
        written=tuple(report.written),
    )


def diff_packages(before_dir: str | Path, after_dir: str | Path) -> VersionDiff:
    """两个已登记的模板包版本的字段级差异（P04 的"版本对比"）。

    ``before_dir`` / ``after_dir`` 是**包目录**而不是 ``TemplatePackage``：块原件
    （``blocks/`` 里的图片与嵌入对象）也参与差异判定，而它只在磁盘上。让调用方自己
    先 ``read_package`` 再传进来，等于把"块原件也要读"这件事漏给每一处调用点——
    漏一处就是"图片换了但对比说没变"。

    差异的**判定**唯一实现在 :func:`docfidelity.registry.diff_packages`；本函数只把
    它的结果翻译成 :class:`VersionDiff`——一份 ``pipeline`` 自己拥有的、可序列化的形状。
    这样做的理由是 ``registry.TemplateVersionDiff`` 是**内核实现的类型**，
    ``server`` 不得 import 它（DD-13.1）；服务端要能拿到差异内容，就只能有人把它
    译成契约形状，而那个"有人"必须是接缝。

    Raises
    ------
    EngineError
        ``E_PACKAGE_INCOMPLETE``：任一版本读不回来。
    """
    raw = registry.diff_packages(
        registry.read_package(before_dir),
        registry.read_package(after_dir),
        block_assets_before=registry.read_block_assets(before_dir),
        block_assets_after=registry.read_block_assets(after_dir),
    )
    return VersionDiff(
        before_ref=raw.before_ref,
        after_ref=raw.after_ref,
        identical=raw.identical,
        ok=raw.ok,
        summary=raw.summary(),
        changelog=list(raw.changelog()),
        sections=[
            SectionChange(
                section=delta.section,
                impact=delta.impact,
                blocking=delta.blocking,
                changed=delta.changed,
                changes=[
                    FieldDelta(
                        path=change.path,
                        section=change.section,
                        category=change.category,
                        kind=change.kind,
                        before=change.before,
                        after=change.after,
                        description=change.describe(),
                    )
                    for change in delta.changes
                ],
            )
            for delta in raw.deltas
        ],
    )


def fingerprint_problems(package_dir: str | Path) -> list[str]:
    """重算并比对包内指纹，返回问题清单（**空列表 = 通过**）。

    与 :func:`verify_package` 分工：那个查的是"``package_hash`` 与清单是否一致"
    （包有没有被外部改过），本函数查的是"``fingerprint.json`` 与包内容是否一致"
    （指纹有没有被改过 / 算完之后内容又动过）。P04 的"指纹"页签要后者。

    ``source`` 分区**跳过**：原件在包目录里本就无法重算，跳过的是"原件是否变了"，
    不是"包内容是否变了"——后者由其余分区覆盖（见 ``registry.verify_fingerprint``）。
    """
    package = read_package(package_dir)
    return registry.verify_fingerprint(
        package, block_assets=registry.read_block_assets(package_dir)
    )


def export_package(package_dir: str | Path) -> bytes:
    """把一个已登记的模板包打成 zip 字节（DD-13.2 的 ``GET /templates/{tid}/package``）。

    **同一份包导出两次，字节完全相同**——时间戳被固定成 zip 格式的纪元值
    （1980-01-01），条目按相对路径排序写入。这不是洁癖：导出件是"交给第三方复现"
    的凭据，若同一份包每次导出的字节都不同，收件方就无法用摘要核对"你发我的就是你
    库里的那一份"，而包哈希本身又算不进来（它在包内清单里）。
    """
    root = Path(package_dir)
    return zip_directory(root)
