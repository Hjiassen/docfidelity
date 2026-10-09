#!/usr/bin/env python3
"""生成 ``demo-templates/`` 下的**自制示例模板包**（TASK-P0-18 / 需求 10.10 / OS-20）。

为什么是脚本而不是一份手搓的 .docx
----------------------------------
需求 10.10 要求示例资产"必须能一一对应地展示全部能力"。若仓库里只有一份二进制 docx，
就没有人说得清它到底声明了哪些能力，改完也没法重跑验证。故每个模板由三样东西构成，
三者的关系是**可复现**的：

===================  ==========================================================
``template.json``    身份 + **能力清单**（10.10 的能力项逐条对应）+ 未覆盖项与原因
``template.docx``    生成物：由本脚本从代码构造，不含任何私有资产、不经 Word
``package/``         入库产物：``registry.ingest_template`` 从 ``template.docx`` 解析
===================  ==========================================================

``--check`` 会用同一份 docx 重新入库，并与仓库里的 ``package/`` 逐文件比对——
示例资产因此不会悄悄漂移（"改了生成脚本却忘了重跑"是本文件存在的全部理由）。

依赖面
------
docx 的 XML 拼装复用 ``tests.fixtures.docx_builder``：那是本仓库里"手工构造 docx"的
唯一实现（``scripts/serve_dev.py`` 也引它）。把那段拼装复制进本脚本，等于给
"包内 XML 长什么样"造出第二个口径，而两套写法必然漂移。

用法::

    PYTHONPATH="src;." .venv/Scripts/python.exe scripts/make_demo_templates.py
    PYTHONPATH="src;." .venv/Scripts/python.exe scripts/make_demo_templates.py --check
"""

from __future__ import annotations

import argparse
import json
import sys
import zipfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_ROOT = REPO_ROOT / "demo-templates"
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

#: 示例资产是**入库资产**，不是运行期产物：它的字节必须只由本脚本决定。
#:
#: 两个不确定性来源都在"入库"这一步，且都与内容无关——
#:
#: 1. ``zipfile.writestr(str, ...)`` 用**当前时刻**写条目时间戳，故同一份 docx
#:    两次生成会得到两个字节串，``manifest.source_hash`` 随之漂移；
#: 2. ``TemplateManifest.created_at`` 是 ``datetime.now(timezone.utc)``。
#:
#: 两者都钉死成下面这两个常量。刻意**不改引擎**：真实入库时"何时入的库"
#: 本来就该是真实时刻，需要确定性的只有仓库里的示例资产，由生成器负责。
ZIP_EPOCH: tuple[int, int, int, int, int, int] = (2026, 1, 1, 0, 0, 0)
CREATED_AT = "2026-01-01T00:00:00Z"


@dataclass(frozen=True)
class TemplateSpec:
    """一个示例模板的身份、能力清单与骨架。

    ``headings`` 只用于构造 docx 的示例正文；生成期真正的章节顺序来自**档案**
    （``DocTypeProfile.genre.chapter_skeleton``），模板这一侧提供的是**原型**与
    前置版块（封面 / 修订记录 / 目录 / 页眉页脚）。
    """

    slug: str
    template_id: str
    version: str
    doc_type: str
    notes: str
    cover_lines: tuple[str, ...]
    revision_headers: tuple[str, ...]
    revision_row: tuple[str, ...]
    headings: tuple[tuple[int, str], ...]
    toc_entries: tuple[tuple[str, str, str], ...]
    table_headers: tuple[str, ...]
    header_text: str
    footer_text: str
    covers: tuple[str, ...]
    sample_table: str


SOFTWARE_DESIGN = TemplateSpec(
    slug="demo-software-design",
    template_id="demo-software-design",
    version="1",
    doc_type="software-design",
    notes="自制示例模板：软件交付类文档（多级编号 + 表格原型 + 长/宽图槽位 + 冗余模块）",
    cover_lines=(
        "示例软件交付说明书",
        "文件编号：SD-000",
        "版本：V1.0",
        "编制单位：此处填写编制单位",
    ),
    revision_headers=("序号", "版本", "修改内容", "修改人", "日期"),
    revision_row=("1", "V1.0", "首次发布", "编制人", "2026-01-01"),
    headings=((1, "概述"), (1, "接口清单"), (2, "参数说明"), (1, "数据表"), (1, "参考资料")),
    toc_entries=(("TOC1", "概述", "_Toc00000001"), ("TOC2", "接口清单", "_Toc00000002")),
    table_headers=("字段", "类型", "必填", "说明"),
    header_text="此处填写文件编号",
    footer_text="第 ",
    # 刻意**不**声明"冗余模块（参考资料）"：这一章被两份档案都用到了（``api-sectioned``
    # 与 ``api-summary`` 都在 ``chapter[4]`` 填内容），它在软件类模板里不是冗余模块。
    # 演示"模板裁剪"的是 demo-operations——``procedure-sop`` 不声明该章，产物因此少一节。
    # 少声明一项不是漏项：``tests/unit/test_demo_assets.py`` 会逐个模板核对
    # "自称的能力在 docx 里找不找得到"，多写一项当场失败。
    covers=(
        "多级标题 + 自动编号",
        "封面 + 修订记录 + 页眉页脚",
        "多种表格原型（含合并单元格、跨行跨列）",
        "目录域 + 页码 + 分节",
        "长图与宽图槽位",
        "带编写说明的占位内容",
        "列表段落原型（项目符号）",
    ),
    sample_table=(
        "示例表格：本表之后的一张表是「合并单元格 / 跨行跨列」原型，"
        "生成期随示例内容区一并裁掉；注入表格的原型取正文第一张表。"
    ),
)

OPERATIONS = TemplateSpec(
    slug="demo-operations",
    template_id="demo-operations",
    version="1",
    doc_type="operations-procedure",
    notes="自制示例模板：非软件域（作业规程 / 评估报告：步骤、阈值、签核栏）",
    cover_lines=("示例分析仪作业规程", "规程编号：OP-000", "版本：V1.0", "编制部门：此处填写"),
    revision_headers=("版本", "修订说明", "修订人", "日期"),
    revision_row=("V1.0", "首次发布", "编制人", "2026-01-01"),
    headings=(
        (1, "适用范围"),
        (1, "作业步骤"),
        (2, "步骤要点"),
        (1, "阈值与判定"),
        (1, "签核记录"),
        (1, "参考资料"),
    ),
    toc_entries=(("TOC1", "适用范围", "_Toc00000001"), ("TOC2", "作业步骤", "_Toc00000002")),
    table_headers=("步骤", "作业内容", "责任人", "判定阈值", "签核"),
    header_text="此处填写规程编号",
    footer_text="第 ",
    covers=(
        "多级标题 + 自动编号",
        "封面 + 修订记录 + 页眉页脚",
        "多种表格原型（含合并单元格、跨行跨列）",
        "目录域 + 页码 + 分节",
        "带编写说明的占位内容",
        "列表段落原型（项目符号）",
        "冗余模块（参考资料）",
    ),
    sample_table=(
        "示例表格：本表之后的一张表是「合并单元格 / 跨行跨列」原型，"
        "生成期随示例内容区一并裁掉；注入表格的原型取正文第一张表。"
    ),
)

TEMPLATES: tuple[TemplateSpec, ...] = (SOFTWARE_DESIGN, OPERATIONS)


# ==================================================================== docx 构造
def build_docx(spec: TemplateSpec, path: Path) -> Path:
    """按 spec 构造模板原件（先 python-docx，再补"只有真实模板才有"的结构）。"""
    from docx import Document
    from docx.enum.section import WD_SECTION

    document = Document()
    for line in spec.cover_lines:
        document.add_paragraph(line)
    # 封面到这里结束：``add_section`` 把当前 sectPr 挪进一个段落的 pPr，
    # 于是"首个分节符之前的连续段落"就是封面块（见 registry.parser.blocks）。
    document.add_section(WD_SECTION.NEW_PAGE)

    # 修订记录必须落在**第一个骨架标题之前**的前置版块里：若排在第一个标题之后，
    # 它会落进示例内容区，注入时随示例内容一起被裁掉（体例凭空消失，产物表面却正常）。
    _revision_table(document, spec.revision_headers, spec.revision_row)
    # Word 更新 TOC 会重建条目段落；域外分节保住目录独占一页的边界。
    document.add_section(WD_SECTION.NEW_PAGE)

    _content_region(document, spec)
    document.save(str(path))
    _patch_package(spec, path)
    _pin_zip_timestamps(path)
    return path


def _pin_zip_timestamps(path: Path) -> None:
    """把 zip 条目的时间戳钉成 :data:`ZIP_EPOCH`（见文件头的说明）。"""
    from tests.fixtures.docx_builder import _read_parts

    parts = _read_parts(path)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, payload in parts.items():
            info = zipfile.ZipInfo(name, date_time=ZIP_EPOCH)
            info.compress_type = zipfile.ZIP_DEFLATED
            # 显式指定 UNIX 来源与权限位：默认值随 ``sys.platform`` 变，
            # 会让同一份脚本在 Windows 与 Linux 上生成两种字节串。
            info.create_system = 3
            info.external_attr = 0o644 << 16
            archive.writestr(info, payload)


def _revision_table(document: Any, headers: Sequence[str], values: Sequence[str]) -> None:
    table = document.add_table(rows=2, cols=len(headers))
    table.style = "Table Grid"
    for column, title in enumerate(headers):
        table.cell(0, column).text = title
    for column, value in enumerate(values):
        table.cell(1, column).text = value


def _content_region(document: Any, spec: TemplateSpec) -> None:
    """示例内容区：骨架标题 + 列表原型 + 原型表 + 图槽位 + 占位说明 + 冗余章。

    这一段在生成期会被**整片删除**（``render.inject`` 的"裁掉示例内容区、按 IR 重建"），
    它存在的价值是提供原型（标题样式、正文段落、列表段落、表格）与占位符样本。故其中的
    冗余章「参考资料」不需要额外的裁剪规则——"注入即替换"本身就是模板裁剪。
    """
    levels = [level for level, _ in spec.headings]
    first_level = min(levels)
    for level, title in spec.headings:
        document.add_heading(title, level=level)
        if level == first_level:
            document.add_paragraph(_placeholder_line(spec))
    _bullet_example(document)
    _prototype_table(document, spec.table_headers)
    document.add_paragraph(spec.sample_table)
    # 合并单元格 / 跨行跨列原型**两份模板都给**：10.10 把这一项归到"表格规范化与列宽契约"，
    # 那是渲染能力，与文档类型无关。只在软件类模板里放，而 ``covers`` 又把它算在非软件域
    # 模板头上，就成了"资产自称的能力在 docx 里找不到对应结构"——正是本轮在查的那类
    # 缺陷（报告全绿、只有人眼看得出来）。``test_demo_assets`` 逐项钉住 covers ↔ 结构。
    _merged_table(document)
    if spec.slug == SOFTWARE_DESIGN.slug:
        _picture_slots(document)


def _bullet_example(document: Any) -> None:
    """项目符号列表原型（``unit=bullets`` 注入时逐条深拷贝它）。

    没有这一处会怎样：``PrototypeIndex.bullet_paragraph()`` **静默回落到正文原型**，
    列表内容照样进产物，只是每条前面的符号没了——判定层面全绿，只有人眼看得出来。
    """
    from tests.fixtures.docx_builder import add_bullet_paragraph

    for index in range(1, 3):
        paragraph = document.add_paragraph(f"示例要点 {index}（列表原型：项目符号）")
        add_bullet_paragraph(paragraph)


def _placeholder_line(spec: TemplateSpec) -> str:
    return f"此处填写{spec.headings[0][1]}内容；本章为编制说明，定稿前请逐项替换。"


def _prototype_table(document: Any, headers: Sequence[str]) -> None:
    """正文第一张表：注入表格的**原型**（渲染层逐行深拷贝它的行与单元格）。"""
    table = document.add_table(rows=2, cols=len(headers))
    table.style = "Table Grid"
    for column, title in enumerate(headers):
        table.cell(0, column).text = title
    for column in range(len(headers)):
        table.cell(1, column).text = f"示例取值 {column + 1}"


def _merged_table(document: Any) -> None:
    """合并单元格 / 跨行跨列原型（表格规范化与列宽契约的输入形态）。"""
    table = document.add_table(rows=4, cols=4)
    table.style = "Table Grid"
    table.cell(0, 0).merge(table.cell(0, 3)).text = "合并成一行四列的表头"
    table.cell(1, 0).merge(table.cell(3, 0)).text = "跨四行"
    table.cell(1, 1).merge(table.cell(1, 2)).text = "跨两列"
    table.cell(1, 3).text = "普通格"
    table.cell(2, 1).text = "普通格"
    table.cell(2, 2).merge(table.cell(2, 3)).text = "跨两列"


def _picture_slots(document: Any) -> None:
    """长图与宽图两种图注槽位（版心内等比缩放的输入形态）。

    **不参与产出**：图片字节在 ``ingest → render`` 之间还没有通路（``FactSnapshot``
    不带 assets、``render_stage`` 不注入 ``asset_loader``），见 ``examples/README.md``
    的"已知缺口"。模板这一侧保留原型，是为了让"槽位存在"这件事在模板包里可查。
    """
    from docx.shared import Emu
    from tests.fixtures.docx_builder import png_bytes

    for caption, size in (
        ("图 1-1 长图槽位（等比缩放后仍落在版心内）", (480, 1600)),
        ("图 1-2 宽图槽位（等比缩放后仍落在版心内）", (1600, 480)),
    ):
        document.add_paragraph(caption)
        suffix = f"{size[0]}x{size[1]}"
        document.add_picture(_png_stream(png_bytes(*size), suffix), width=Emu(_width_for(size)))


def _png_stream(payload: bytes, label: str) -> Any:
    import io

    stream = io.BytesIO(payload)
    stream.name = f"{label}.png"
    return stream


def _width_for(size: tuple[int, int]) -> int:
    """长图按高度受限、宽图按宽度受限；两者都会触发"只缩不放"的等比缩放。"""
    wide, tall = size
    if wide >= tall:
        return 5_000_000
    return 1_200_000


def _patch_package(spec: TemplateSpec, path: Path) -> None:
    """补上 python-docx 不会生成、但真实模板一定有的结构。"""
    from tests.fixtures.docx_builder import (
        _add_bookmarks,
        _add_rsids,
        _add_toc_styles,
        _bind_heading_numbering,
        _decorate_normal_style,
        _decorate_table_style,
        _numbering_xml,
        _parse,
        _read_parts,
        _register_numbering,
        _serialize,
        _settings_xml,
        _strip_list_numbering,
        _strip_rsids,
        _write_parts,
        attach_header_footer,
        inject_toc,
    )

    parts = _read_parts(path)
    document_root = _parse(parts["word/document.xml"])
    styles_root = _parse(parts["word/styles.xml"])

    _strip_rsids(document_root)
    _add_rsids(document_root)
    _add_bookmarks(document_root)
    inject_toc(document_root, spec.toc_entries)
    # 夹具默认把目录插到最前；发行示例的目录应位于修订记录之后、正文之前。
    namespace = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    body = document_root.find(f"{{{namespace}}}body")
    assert body is not None
    toc = body.find(f"{{{namespace}}}sdt")
    assert toc is not None
    body.remove(toc)
    first_heading = body.xpath(
        "./w:p[w:pPr/w:pStyle[@w:val='Heading1']]", namespaces={"w": namespace}
    )[0]
    body.insert(list(body).index(first_heading), toc)
    _add_toc_styles(styles_root)
    _decorate_normal_style(styles_root)
    # 表样式必须声明 firstRow 条件格式（加粗 + 底纹）：不声明的话，注入表格的表头行
    # 既不粗也无底色，而 FR-M5-03 会以 major 报到每一份产物上；引擎**不会**替模板
    # 补直设加粗——直设加粗反过来又会被 FR-M5-01 判违规。两头都不通的地方只有一处出口：
    # 模板自己把表样式写对。
    _decorate_table_style(styles_root)
    attach_header_footer(
        parts,
        document_root,
        header_text=spec.header_text,
        footer_text=spec.footer_text,
        styles_root=styles_root,
    )

    # 多级标题自动编号：Heading1 → ilvl0、Heading2 → ilvl1，同一个 numId。
    parts["word/numbering.xml"] = _numbering_xml()
    _register_numbering(parts, document_root)
    _bind_heading_numbering(styles_root)
    _strip_list_numbering(styles_root)

    if "word/settings.xml" not in parts:
        parts["word/settings.xml"] = _settings_xml()

    parts["word/document.xml"] = _serialize(document_root)
    parts["word/styles.xml"] = _serialize(styles_root)
    _write_parts(path, parts)


# ==================================================================== 入库与落盘
def generate(spec: TemplateSpec, root: Path) -> dict[str, Any]:
    """构造 docx、入库成模板包、写 ``template.json``；返回能力清单摘要。"""
    import shutil

    from docfidelity.registry import ingest_template

    directory = root / spec.slug
    package_dir = directory / "package"
    docx_path = directory / "template.docx"

    directory.mkdir(parents=True, exist_ok=True)
    build_docx(spec, docx_path)
    # 先清掉上一次的包：``registry.write_package`` 拒绝写进**非空**目标目录
    # （``E_VERSION_CONFLICT``：目录里已经有同 ``id@version`` 的包）。不清的话，
    # 重新生成会**静默保留旧包**——docx 换了、``package/`` 还是上一次的，而
    # ``ingest_template`` 只在 ``stage_outcomes`` 里记一条失败，返回值看着很正常。
    # 表现就是 ``--check`` 报"资产与生成结果不一致"，人却以为资产是刚生成的。
    if package_dir.exists():
        shutil.rmtree(package_dir)
    result = ingest_template(
        docx_path,
        template_id=spec.template_id,
        version=spec.version,
        doc_type=spec.doc_type,
        notes=spec.notes,
        target_dir=package_dir,
    )
    if result.write_report is None:
        raise SystemExit(
            f"{spec.slug}：模板包没写出来，驳回原因见入库阶段：{result.failed_stages()}"
        )
    _pin_created_at(package_dir)
    manifest = {
        "template_id": spec.template_id,
        "version": spec.version,
        "doc_type": spec.doc_type,
        "notes": spec.notes,
        "source": "template.docx",
        "package": "package",
        "covers": list(spec.covers),
        "gaps": [_gap("跨模板组合（封面取 A、修订表取 B）")],
        "ingest_findings": len(result.findings),
    }
    (directory / "template.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )
    return manifest


def _pin_created_at(package_dir: Path) -> None:
    """把清单里的 ``created_at`` 钉成 :data:`CREATED_AT`（见文件头的说明）。

    只改这一个字段，且用与引擎同一条序列化口径（``canonical_json``）落盘——
    换成 ``json.dump`` 会让整个包出现"只有清单是另一种排版"的第二种写法。
    """
    from docfidelity.platform.hashing import canonical_json

    path = package_dir / "contract" / "manifest.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("created_at") == CREATED_AT:
        return
    payload["created_at"] = CREATED_AT
    path.write_bytes(canonical_json(payload))


def _gap(item: str) -> dict[str, str]:
    return {
        "item": item,
        "reason": (
            "render 层未实现 BlockEntry.cross_template_ref：注入只认工作副本这一个包，"
            "档案的 bindings.template_packages 目前只用于登记期引用校验"
        ),
    }


def _package_files(package_dir: Path) -> list[str]:
    return sorted(
        item.relative_to(package_dir).as_posix()
        for item in package_dir.rglob("*")
        if item.is_file()
    )


def check(spec: TemplateSpec, root: Path, work: Path) -> list[str]:
    """重新入库到临时目录，与仓库里的资产逐文件比对；返回差异清单。

    ``work`` 是**输出根**（与 ``--root`` 同级语义），生成器自己拼 ``spec.slug``。
    """
    generate(spec, work)
    fresh = work / spec.slug
    committed = root / spec.slug
    problems: list[str] = []

    if not (committed / "template.docx").is_file():
        return [f"{spec.slug}：仓库里没有 template.docx，请先运行生成器"]

    if (committed / "template.docx").read_bytes() != (fresh / "template.docx").read_bytes():
        problems.append(f"{spec.slug}/template.docx 与重新生成的结果不一致")

    left_files = _package_files(committed / "package")
    right_files = _package_files(fresh / "package")
    if left_files != right_files:
        problems.append(f"{spec.slug}/package 文件清单不一致：{left_files} != {right_files}")
    for name in left_files:
        left = (committed / "package" / name).read_bytes()
        right = (fresh / "package" / name).read_bytes()
        if left != right:
            problems.append(f"{spec.slug}/package/{name} 内容不一致")
    return problems


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成 demo-templates/ 下的示例模板包")
    parser.add_argument(
        "--root", type=Path, default=OUTPUT_ROOT, help="输出根（默认 demo-templates）"
    )
    parser.add_argument("--work", type=Path, default=None, help="--check 的重生成目录")
    parser.add_argument("--check", action="store_true", help="只校验仓库里的资产是否与生成结果一致")
    args = parser.parse_args(argv)

    if args.check:
        import tempfile

        with tempfile.TemporaryDirectory(prefix="df-demo-") as tmp:
            work = args.work or Path(tmp)
            problems: list[str] = []
            for spec in TEMPLATES:
                problems.extend(check(spec, args.root, work))
        if problems:
            print("FAIL 示例模板资产与生成结果不一致：")
            for problem in problems:
                print(f"  {problem}")
            print("处置：重跑 scripts/make_demo_templates.py 并提交产物。")
            return 1
        print(f"OK 示例模板资产可复现（{len(TEMPLATES)} 个模板包）")
        return 0

    for spec in TEMPLATES:
        manifest = generate(spec, args.root)
        tiles = len(manifest["covers"]) + len(manifest["gaps"])
        print(f"{spec.slug}\t{docx_of(args.root, spec)}\t能力项 {tiles}")
    return 0


def docx_of(root: Path, spec: TemplateSpec) -> str:
    return str((root / spec.slug / "template.docx").relative_to(REPO_ROOT))


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
