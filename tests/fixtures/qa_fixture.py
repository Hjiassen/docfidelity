"""``qa`` 轮的可复用夹具（测试专用）。

为什么走**真渲染**而不是手搓一个包
----------------------------------
门禁的判据全部长在产物上：内容区在哪、表样式有没有提供首行加粗、图片段落的
``wp:extent`` 落到哪一分节。手搓一份"看起来像产物"的 docx 能跑通签名，却跑不出
"分节属性在段落里"这类真实形态——而那正是这些规则唯一会出错的地方。
故这里与生产链路同一条路径：入库真模板 → 真渲染 → 在产物上跑门禁。

夹具里出现 ``概述`` / ``范围`` / ``图示`` 这类示例词是可以的：``tests/fixtures/``
在类型无关检索的白名单里——它们是**数据**，不是引擎代码。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from docfidelity.contracts.config import Terminology, TerminologyEntry
from docfidelity.contracts.ir import ContentIR
from docfidelity.contracts.profile import (
    AppliesStatement,
    ChapterSkeletonItem,
    DocTypeProfile,
    GenreSpec,
)
from docfidelity.contracts.qa import Finding, TraceMatrix
from docfidelity.contracts.template import TemplatePackage
from docfidelity.ooxml import DocxPackage
from docfidelity.qa import QaContext
from docfidelity.render import RenderPolicy, RenderResult, open_working_copy, render

from .docx_builder import build_template_docx, png_bytes
from .render_fixture import chapter_ir, ingest_contract

__all__ = [
    "QA_DOC_TYPE",
    "RenderFixture",
    "qa_context",
    "qa_profile",
    "qa_terminology",
    "render_fixture",
]

#: 夹具档案的文档类型标识。刻意**不是**任何真实类型名——引擎对取值不做假设，
#: 夹具也不该给引擎塞一个真实类型名（那会让禁用词门禁的结论失去意义）。
QA_DOC_TYPE = "qa-demo"

#: 夹具档案声明的章节骨架，与 ``chapter_ir`` 的标题一一对应（无槽位 → 不产生槽位告警）。
_SKELETON_TITLES: tuple[str, ...] = ("概述", "范围", "图示")

#: 图片资源加载器：按固定尺寸造图，尺寸在版心内（不触发 FR-M5-07 的越界分支）。
_FIGURE_SIZE = (240, 160)


@dataclass
class RenderFixture:
    """一次真渲染的结果与它的输入。"""

    path: Path
    template_path: Path
    template: TemplatePackage
    ir: ContentIR
    result: RenderResult

    def open(self) -> DocxPackage:
        """以只读方式打开产物（``open`` 已把全部部件读进内存，不需要显式关闭）。"""
        return DocxPackage.open(self.path, read_only=True)


def render_fixture(
    tmp_path: Path,
    *,
    ir: ContentIR | None = None,
    policy: RenderPolicy | None = None,
    asset: Callable[[str], bytes] | None = None,
    office: bool | None = False,
) -> RenderFixture:
    """真渲染一份产物（走 ``registry`` 入库 + ``render`` 全链路）。

    ``office`` 缺省钉死 ``False``：域刷新是整个渲染里唯一会去**起 Word** 的一步
    （真 ``WINWORD.EXE`` 进程）。门禁测试要的是产物本身，不该随开发机装没装 Word
    而变；``True`` 只在该测试确实要验"刷新成功"那条分支时才传。
    """
    template_path = build_template_docx(tmp_path / "template.docx")
    contract, profile = ingest_contract(template_path)
    content = ir if ir is not None else chapter_ir(with_table=True, with_figure=True)
    loader = asset or (lambda _ref: png_bytes(*_FIGURE_SIZE))
    out_path = tmp_path / "artifact.docx"
    result = render(
        pkg=open_working_copy(template_path),
        template=contract,
        profile=profile,
        ir=content,
        out_path=out_path,
        policy=policy or RenderPolicy(),
        asset_loader=loader,
        office=office,
    )
    return RenderFixture(
        path=out_path,
        template_path=template_path,
        template=contract,
        ir=content,
        result=result,
    )


def qa_profile() -> DocTypeProfile:
    """门禁夹具档案：骨架与夹具 IR 对齐，且**不声明槽位**。

    不声明槽位是刻意的：这条夹具要用来验证"一份干净产物不该有任何发现"，
    而声明了槽位再让它们全部落位只是把另一条规则的结果预先写死。
    """
    return DocTypeProfile(
        profile_id="qa-demo",
        doc_type=QA_DOC_TYPE,
        title="门禁演示档案",
        applies=AppliesStatement(
            layout_source="TPL-DEMO@1.0",
            structure_enumerable="章节骨架见 genre.chapter_skeleton",
            content_traceable="夹具事实源",
        ),
        genre=GenreSpec(
            chapter_skeleton=[
                ChapterSkeletonItem(level=1, title=title) for title in _SKELETON_TITLES
            ],
            allowed_unit_types=[
                "heading",
                "para",
                "bullets",
                "numbered",
                "table",
                "figure",
                "screenshot",
            ],
        ),
    )


def qa_terminology() -> Terminology:
    """术语表：一条规范写法 + 一条禁用旧称，用来验证 FR-M5-08 的两个方向。"""
    return Terminology(
        scope="project",
        entries=[
            TerminologyEntry(term="演示系统", forbidden=["演示系统平台"]),
        ],
    )


def qa_context(
    fixture: RenderFixture,
    *,
    profile: DocTypeProfile | None = None,
    terminology: Terminology | None = None,
    traces: TraceMatrix | None = None,
    baseline: DocxPackage | None = None,
    baseline_ref: str = "",
    spotcheck: object | None = None,
    upstream: Sequence[Finding] = (),
    with_profile: bool = True,
) -> QaContext:
    """构造门禁上下文。

    ``with_profile=False`` 用来验证"缺档案时依赖渲染视图的规则**被跳过**并写进摘要"，
    而不是静默给一个结论。``baseline`` / ``terminology`` / ``traces`` / ``spotcheck``
    缺省即对应规则被跳过（能力门禁），故只有显式传入时那几条规则才参检。
    """
    return QaContext(
        pkg=fixture.open(),
        template=fixture.template,
        ir=fixture.ir,
        profile=(profile if profile is not None else qa_profile()) if with_profile else None,
        terminology=terminology,
        traces=traces,
        baseline=baseline,
        baseline_ref=baseline_ref,
        spotcheck=spotcheck,
        upstream=tuple(upstream),
    )
