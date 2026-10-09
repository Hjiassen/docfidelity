"""``render`` 轮的可复用夹具（测试专用）。

为什么直接用 ``registry`` 入库出来的真契约
------------------------------------------
渲染层只吃契约，而"契约长什么样"直接决定渲染行为：层级→样式映射、块定位、
占位符清单、表格原型——这些全都要**真**的才有意义。手工编一份 ``TemplatePackage``
（像 ``compose_fixture.template_package`` 那样）能跑通签名，却跑不出"模板里没有
该级标题样式"这类真实分支。故这里走 ``ingest_template``，与生产链路同一条解析路径。

夹具里出现 ``概述`` / ``范围`` 这类示例文档词是可以的：``tests/fixtures/`` 在类型
无关检索的白名单里——它们是**数据**，不是引擎代码。
"""

from __future__ import annotations

from pathlib import Path

from docfidelity.contracts.ir import ContentIR, ContentUnit
from docfidelity.contracts.profile import DocTypeProfile
from docfidelity.contracts.template import TemplatePackage
from docfidelity.ooxml import DocxPackage
from docfidelity.registry import ingest_template
from docfidelity.render import RenderContext

from .compose_fixture import api_profile
from .docx_builder import build_template_docx

__all__ = [
    "CHAPTER_PARAGRAPH",
    "TEMPLATE_EXAMPLE_PARAGRAPH",
    "body_texts",
    "chapter_ir",
    "docx_part_bytes",
    "ingest_contract",
    "make_context",
    "plain_ir",
    "template_docx_path",
]

#: 注入内容的可见标记：测试靠它断言"注入内容确实进了正文"。
CHAPTER_PARAGRAPH = "注入的正文第一段。"

#: 模板示例内容区里的正文（注入后必须消失）。
TEMPLATE_EXAMPLE_PARAGRAPH = "此处填写项目背景。"


def template_docx_path(directory: Path, name: str = "template.docx") -> Path:
    """生成"像真模板"的 docx（封面独占一节 + 修订记录表 + 页眉占位 + 多级标题）。"""
    return build_template_docx(directory / name)


def ingest_contract(path: Path) -> tuple[TemplatePackage, DocTypeProfile]:
    """入库模板原件，返回 ``(模板契约, 文档类型档案)``。

    入库失败直接抛 AssertionError：夹具坏了不该被当成"渲染有问题"。
    """
    result = ingest_template(path)
    assert result.package is not None, "模板夹具入库失败，后续断言都失去意义"
    return result.package, api_profile()


def plain_ir(units: list[ContentUnit] | None = None) -> ContentIR:
    """最小内容 IR：一章 + 一段（可覆盖 ``units``）。"""
    return ContentIR(
        project_id="demo",
        doc_key="detail",
        profile_id="api-compact",
        template_ref="TPL-DEMO@1.0",
        units=units if units is not None else chapter_ir().units,
    )


def chapter_ir(
    *,
    with_table: bool = False,
    with_figure: bool = False,
    with_lists: bool = False,
    with_pagebreak: bool = False,
    with_subheading: bool = False,
) -> ContentIR:
    """一份可拼接的章节 IR（按需打开表格 / 图片 / 列表 / 分页 / 子标题）。"""
    children = [ContentUnit(unit_id="c1#p0", type="para", text=CHAPTER_PARAGRAPH)]
    if with_lists:
        children.append(ContentUnit(unit_id="c1#l0", type="bullets", text="要点一\n要点二"))
        children.append(ContentUnit(unit_id="c1#l1", type="numbered", text="第一步"))
    units = [ContentUnit(unit_id="c1", type="heading", level=1, text="概述", children=children)]

    if with_table:
        units.append(
            ContentUnit(
                unit_id="c2",
                type="heading",
                level=1,
                text="范围",
                children=[
                    ContentUnit(
                        unit_id="c2#t0",
                        type="table",
                        fields={
                            "columns": ["项目", "说明"],
                            "rows": [["A", "甲"], ["B", "乙"], ["C", "丙"]],
                            "caption": "表 2-1 范围对照",
                        },
                    )
                ],
            )
        )
    if with_figure:
        units.append(
            ContentUnit(
                unit_id="c3",
                type="heading",
                level=1,
                text="图示",
                children=[
                    ContentUnit(
                        unit_id="c3#f0",
                        type="figure",
                        fields={
                            "image_ref": "diagram.png",
                            "caption": "图 3-1 示意图",
                            "width_px": 240,
                            "height_px": 160,
                            "dpi": 96,
                        },
                    )
                ],
            )
        )
    if with_subheading:
        units.append(ContentUnit(unit_id="c4", type="heading", level=2, text="附录"))
    if with_pagebreak:
        units.append(ContentUnit(unit_id="c5", type="pagebreak"))

    return ContentIR(
        project_id="demo",
        doc_key="detail",
        profile_id="api-compact",
        template_ref="TPL-DEMO@1.0",
        units=units,
    )


def make_context(
    path: Path,
    *,
    ir: ContentIR | None = None,
    contract: TemplatePackage | None = None,
    profile: DocTypeProfile | None = None,
    asset_loader: RenderContext.AssetLoader | None = None,
) -> RenderContext:
    """构造一个渲染上下文（工作副本 + 契约 + IR）。

    每次调用都**重新打开**一个可写工作副本：``render`` 会就地改写包，
    复用同一个包会让第二次渲染看到被改过的模板。
    """
    from docfidelity.render import open_working_copy

    contract = contract or ingest_contract(path)[0]
    profile = profile or api_profile()
    return RenderContext(
        pkg=open_working_copy(path),
        template=contract,
        profile=profile,
        ir=ir or plain_ir(),
        asset_loader=asset_loader,
        protected_blocks=contract.blocks.blocks,
    )


def docx_part_bytes(path: Path) -> dict[str, bytes]:
    """读出包内全部部件的原始字节（用于"未触碰部件字节全等"断言）。"""
    import zipfile

    with zipfile.ZipFile(path) as archive:
        return {
            info.filename: archive.read(info.filename)
            for info in archive.infolist()
            if not info.is_dir()
        }


def body_texts(path: Path) -> list[tuple[str, str]]:
    """产物正文的直接子元素 ``(局部名, 文本)``（段落取可见文本，其余为空串）。"""
    from docfidelity.ooxml import (
        DOCUMENT_PART,
        find_first,
        localname,
        paragraph_text,
    )

    with DocxPackage.open(path, read_only=True) as pkg:
        body = find_first(pkg.part(DOCUMENT_PART).read_tree, "w:body")
        assert body is not None, "产物正文缺少 w:body"
        return [
            (localname(child), paragraph_text(child) if localname(child) == "p" else "")
            for child in body
        ]
