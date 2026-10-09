#!/usr/bin/env python3
"""同源多型演示：**同一份事实源 → 三份结构不同的文档**（TASK-P0-18 / 需求 4.9.5 / 10.10）。

它证明什么
----------
需求 10.10 要求示例资产做到"可切换至少三类示例文档，当场看到「同一份事实源 → 三种文档
产出」"。本脚本就是那句话的可执行版本：``examples/project.json`` 声明"一个项目三份文档"，
``examples/sources.json`` 声明两条事实源，``examples/data/`` 放两份输入文件，喂给
``examples/profiles/`` 下的三份档案，跑完整条五阶段流水线，产出三份 ``.docx``。

======================  ======================  ============================================
文档（``doc_key``）     模板                    结构形态
======================  ======================  ============================================
``api-sectioned``       demo-software-design  一条接口一节，节内参数表 + 响应表（逐条展开）
``api-summary``         **同一个模板包**       一模块一张汇总表（粒度换到模块）
``procedure-sop``       demo-operations       非软件域：作业步骤逐条一节 + 阈值表 + 签核表
======================  ======================  ============================================

前两份文档**共用同一个模板包**且读**同一份事实包**（同一个 ``interface-surface`` 来源），
差别只在投影（``layout`` / ``columns`` / ``group_by`` / ``unit``）。这正是"引擎不认识
文档类型"这条宣称的证据：换类型 = 换声明。

"同一份事实源"≠"每份文档都接全部来源"
--------------------------------------
上图里的"同一份"指的是**同一批示例输入文件**（``examples/data/``），而不是"每条事实都
进每份文档"。项目配置按文档声明 ``source_ids``，本脚本照它挑选来源——真实链路上这件事由
``server.jobs.payload`` 做。三份文档都接全量来源会得到三份"未使用领域对象"的 FR-M5-10
判定，那不是文档的毛病，是"没说清这份文档要用哪些来源"。

输入文件 ``data/operations.xlsx`` 由本脚本生成
--------------------------------------------
它是**二进制**，而二进制资产必须可复现，否则"改了什么"只能靠翻 commit。故工作簿由
:func:`write_workbook` 按固定内容 + 固定 zip 时间戳落盘；每次运行都会重写一遍，
而字节完全一致（跑完 ``git status`` 应当仍然干净）。

用法::

    PYTHONPATH="src;." .venv/Scripts/python.exe examples/build_demo.py
    PYTHONPATH="src;." .venv/Scripts/python.exe examples/build_demo.py --only procedure-sop
"""

from __future__ import annotations

import argparse
import json
import sys
import zipfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Final

EXAMPLES: Final = Path(__file__).resolve().parent
REPO: Final = EXAMPLES.parent
DEMO_TEMPLATES: Final = REPO / "demo-templates"
DATA: Final = EXAMPLES / "data"
DEFAULT_ROOT: Final = REPO / "var" / "examples"

#: 工作簿的 zip 条目时间戳。与 ``scripts/make_demo_templates.py`` 同一个理由：
#: 入库资产（这里是示例输入文件）的字节只能由内容决定，不能由运行时刻决定。
ZIP_EPOCH: Final[tuple[int, int, int, int, int, int]] = (2026, 1, 1, 0, 0, 0)

WORKBOOK_NAME: Final = "operations.xlsx"
WORKBOOK_SHEET: Final = "作业步骤"
REVISION_SHEET: Final = "修订记录"

#: ``作业步骤`` 表：表头行 + 数据行。列名必须与 ``sources.json`` 的 ``field_map`` 左值一致。
STEP_HEADERS: Final[tuple[str, ...]] = ("步骤", "作业内容", "责任人", "判定阈值", "签核")
STEP_ROWS: Final[tuple[tuple[str, ...], ...]] = (
    ("01", "开机自检并确认试剂余量", "操作员", "余量 >= 20 %", "2026-03-01 张三"),
    ("02", "装载样本架并录入条码", "操作员", "条码校验通过", "2026-03-01 张三"),
    ("03", "运行空白校准", "检验技师", "本底 <= 0.05 mg/dL", "2026-03-01 李四"),
    ("04", "执行样本检测", "检验技师", "重复性 CV <= 3 %", "2026-03-02 李四"),
    ("05", "复核异常结果并留痕", "审核人", "偏差 <= 2 SD", "2026-03-02 王五"),
    ("06", "导出报告并归档", "审核人", "报告编号连续", "2026-03-02 王五"),
)

REVISION_HEADERS: Final[tuple[str, ...]] = ("版本", "修订说明", "修订人", "日期")
REVISION_ROWS: Final[tuple[tuple[str, ...], ...]] = (
    ("V1.0", "首次发布", "张三", "2026-03-01"),
    ("V1.1", "补充复核步骤", "李四", "2026-03-02"),
)


# ============================================================== 输入文件（工作簿）
def _sheet_xml(rows: Sequence[Sequence[str]]) -> str:
    """按 ``inlineStr`` 写一张表——不引入 ``sharedStrings.xml``，少一处可漂移的部件。"""
    body: list[str] = []
    for row_index, row in enumerate(rows, start=1):
        cells: list[str] = []
        for column_index, value in enumerate(row):
            ref = f"{_column_name(column_index)}{row_index}"
            cells.append(f'<c r="{ref}" t="inlineStr"><is><t>{_escape(value)}</t></is></c>')
        body.append(f'<row r="{row_index}">{"".join(cells)}</row>')
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f"<sheetData>{''.join(body)}</sheetData></worksheet>"
    )


def _column_name(index: int) -> str:
    """0 基列号 → ``A`` / ``AB``。"""
    letters = ""
    index += 1
    while index:
        index, remainder = divmod(index - 1, 26)
        letters = chr(ord("A") + remainder) + letters
    return letters


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _workbook_parts() -> dict[str, bytes]:
    rels = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    sheets = ((WORKBOOK_SHEET, _sheet_xml([STEP_HEADERS, *STEP_ROWS])),)
    sheets += ((REVISION_SHEET, _sheet_xml([REVISION_HEADERS, *REVISION_ROWS])),)
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        + "".join(
            f'<Override PartName="/xl/worksheets/sheet{index}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            for index in range(1, len(sheets) + 1)
        )
        + "</Types>"
    )
    workbook = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
        f' xmlns:r="{rels}"><sheets>'
        + "".join(
            f'<sheet name="{_escape(name)}" sheetId="{index}" r:id="rId{index}"/>'
            for index, (name, _) in enumerate(sheets, start=1)
        )
        + "</sheets></workbook>"
    )
    workbook_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        + "".join(
            f'<Relationship Id="rId{index}" Type="{rels}/worksheet"'
            f' Target="worksheets/sheet{index}.xml"/>'
            for index in range(1, len(sheets) + 1)
        )
        + "</Relationships>"
    )
    parts = {
        "[Content_Types].xml": content_types,
        "_rels/.rels": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            f'<Relationship Id="rId1" Type="{rels}/officeDocument" Target="xl/workbook.xml"/>'
            "</Relationships>"
        ),
        "xl/workbook.xml": workbook,
        "xl/_rels/workbook.xml.rels": workbook_rels,
    }
    for index, (_, payload) in enumerate(sheets, start=1):
        parts[f"xl/worksheets/sheet{index}.xml"] = payload
    return {name: value.encode("utf-8") for name, value in parts.items()}


def write_workbook(path: Path | None = None) -> Path:
    """把 ``作业步骤`` 工作簿写到 ``examples/data/``（内容与字节都是确定的）。"""
    target = path or (DATA / WORKBOOK_NAME)
    target.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, payload in _workbook_parts().items():
            info = zipfile.ZipInfo(name, date_time=ZIP_EPOCH)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = 0o644 << 16
            archive.writestr(info, payload)
    return target


# ============================================================== 冻结输入
def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def declarations() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """读 ``domains.json`` / ``sources.json``；返回 (领域模型, 来源)。"""
    domains = _read_json(EXAMPLES / "domains.json")
    bundle = _read_json(EXAMPLES / "sources.json")
    return domains, bundle["sources"]


def project() -> Any:
    """``examples/project.json``：项目级声明（文档清单 + 分页 + 术语 + 规则集）。

    刻意用**真契约**（``ProjectConfig``）而不是自造的字典：这样"示例里的项目声明
    合不合法"由契约自己回答，也保证演示脚本与 ``server`` 那条链路读的是同一套字段
    名（``documents[].source_ids`` 等）。演示脚本在这里扮演的是**项目配置**的角色。
    """
    from docfidelity.contracts.config import ProjectConfig

    return ProjectConfig.model_validate(_read_json(EXAMPLES / "project.json"))


def profile_of(profile_id: str) -> dict[str, Any]:
    """按 ``profile_id`` 读档案本体（文件名 = ``profile_id``，不设第二套命名）。"""
    path = EXAMPLES / "profiles" / f"{profile_id}.json"
    if not path.is_file():
        raise SystemExit(f"档案不存在：{path}")
    profile = _read_json(path)
    if str(profile.get("profile_id")) != profile_id:
        raise SystemExit(
            f"档案文件名与 profile_id 不一致：{path} 里是 {profile.get('profile_id')!r}"
        )
    return profile


def sources_for(entry: Any, sources: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """按 ``DocumentEntry.source_ids`` 挑本次文档要接的来源。

    真实链路上这是 ``server.jobs.payload._freeze_sources`` 的活。演示脚本不挑的话，
    每份文档都会拿到**整包**事实，于是三份产物各挂一条 FR-M5-10"未使用的领域对象"——
    而那不是文档的毛病，是"没说清这份文档要用哪些来源"。
    """
    by_id = {str(item["source_id"]): item for item in sources}
    missing = [source_id for source_id in entry.source_ids if source_id not in by_id]
    if missing:
        raise SystemExit(f"文档 {entry.doc_key!r} 引用了未声明的来源：{missing}")
    return [dict(by_id[source_id]) for source_id in entry.source_ids]


def payload_for(
    entry: Any,
    profile: Mapping[str, Any],
    *,
    domains: Sequence[Mapping[str, Any]],
    sources: Sequence[Mapping[str, Any]],
    config: Any,
    office: bool = False,
) -> dict[str, Any]:
    """把项目声明 + 档案 + 事实源拼成一份**冻结输入**（``server.jobs.freeze_payload`` 的形状）。

    ``office`` 默认 ``False``：示例资产与 golden 基线都要求"同一份输入产出同一份
    产物"，而域刷新会让产物字节取决于**跑在哪台机器上**（本机装没装 Word）。
    要真页码就用 ``--office`` 显式打开，接受"这份产物不再可复现"这个代价。
    """
    template_ref = str(entry.template_ref)
    slug = template_ref.split("@", 1)[0]
    package = DEMO_TEMPLATES / slug / "package"
    docx = DEMO_TEMPLATES / slug / "template.docx"
    if not package.is_dir() or not docx.is_file():
        raise SystemExit(
            f"模板包不存在：{package}。先运行 "
            'PYTHONPATH="src;." .venv/Scripts/python.exe scripts/make_demo_templates.py'
        )
    return {
        "project_id": config.project_id,
        "doc_key": str(entry.doc_key),
        "doc_type": str(profile["doc_type"]),
        "template_ref": template_ref,
        "profile_id": str(entry.profile_id),
        "source_ids": list(entry.source_ids),
        "profile": dict(profile),
        "domain_models": [dict(item) for item in domains],
        "sources": sources_for(entry, sources),
        "terminology": config.terminology.model_dump(mode="json"),
        "pagination": config.pagination.model_dump(mode="json"),
        "rule_sets": [ref.model_dump(mode="json") for ref in config.rule_sets],
        "source_root": str(DATA.resolve()),
        "template_package_dir": str(package.resolve()),
        "template_docx": str(docx.resolve()),
        "office_enabled": office,
        "office_reason": None if office else "示例构建固定关闭域刷新，使产物字节可复现",
    }


# ============================================================== 预检
def precheck(profile: Mapping[str, Any]) -> list[str]:
    """档案 × 模板包 可满足性（``compose.precheck``），返回 major 说明。"""
    from docfidelity.compose import satisfiable
    from docfidelity.contracts.profile import DocTypeProfile
    from docfidelity.registry import read_package

    parsed = DocTypeProfile.model_validate(dict(profile))
    refs = parsed.bindings.template_packages
    package = read_package(DEMO_TEMPLATES / refs[0].split("@", 1)[0] / "package")
    return [
        finding.message
        for finding in satisfiable(parsed, package)
        if finding.severity in {"major", "blocker"}
    ]


# ============================================================== 主流程
def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="同源多型示例：同一份事实源 → 三份文档")
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT, help="工作区根")
    parser.add_argument("--only", default=None, help="只跑一份文档（按 doc_key）")
    parser.add_argument(
        "--office",
        action="store_true",
        help="允许本机 Word 刷新目录页码（默认关闭：产物字节只由输入决定）",
    )
    parser.add_argument("--skip-check", action="store_true", help="跳过档案 × 模板包预检")
    args = parser.parse_args(argv)

    if str(REPO / "src") not in sys.path:
        sys.path.insert(0, str(REPO / "src"))

    from docfidelity.builder.cli import main as cli_main

    write_workbook()
    domains, sources = declarations()
    config = project()
    selected = [entry for entry in config.enabled_documents() if args.only in (None, entry.doc_key)]
    if not selected:
        names = ", ".join(entry.doc_key for entry in config.documents)
        print(f"没有匹配的文档：{args.only}（可选：{names}）", file=sys.stderr)
        return 2

    root = args.root
    payload_dir = root / "payloads"
    payload_dir.mkdir(parents=True, exist_ok=True)

    rows: list[tuple[str, str, str, str]] = []
    evidence: list[tuple[str, str]] = []
    exit_code = 0
    for entry in selected:
        profile = profile_of(entry.profile_id)
        if not args.skip_check:
            for problem in precheck(profile):
                print(f"预检未通过 [{entry.doc_key}] {problem}", file=sys.stderr)
                exit_code = 1
        path = payload_dir / f"{entry.doc_key}.json"
        path.write_text(
            json.dumps(
                payload_for(
                    entry,
                    profile,
                    domains=domains,
                    sources=sources,
                    config=config,
                    office=args.office,
                ),
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        code, summary = _run(cli_main, path, root)
        exit_code = exit_code or code
        rows.append((entry.doc_key, *_describe(root, summary)))
        evidence.append(
            (
                entry.doc_key,
                f"来源 {', '.join(entry.source_ids)}；"
                f"事实包摘要 {_source_digests(root, str(summary.get('job_id') or ''))}",
            )
        )

    _report(rows, evidence)
    return exit_code


def _source_digests(root: Path, job_id: str) -> str:
    """该文档实际接入的来源摘要（``facts.json`` 的 ``source_digests``）。

    这是"**同一份**事实源"这句话的可核对形式：两份 API 文档读同一个来源、同一份输入
    文件，摘要就必须逐字相同——它由文件内容算出，不由脚本承诺。
    """
    from docfidelity.pipeline.stages import Workspace

    if not job_id:
        return "（没有作业）"
    path = Workspace(root, job_id).work_dir / "facts.json"
    if not path.is_file():
        return "（没有事实包）"
    digests = _read_json(path).get("source_digests") or {}
    return " ".join(f"{name}={str(value)[:12]}" for name, value in sorted(digests.items()))


def _run(cli_main: Any, payload: Path, root: Path) -> tuple[int, dict[str, Any]]:
    """用同一个命令行入口跑一遍（演示路径与使用者手敲的那条完全一致）。"""
    import contextlib
    import io

    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = cli_main(["build", str(payload), "--root", str(root), "--json"])
    text = buffer.getvalue().strip()
    summary: dict[str, Any] = json.loads(text) if text else {}
    return int(code), summary


def _describe(root: Path, summary: Mapping[str, Any]) -> tuple[str, str, str]:
    """(阶段状态串, 门禁结论, 产物路径)。"""
    stages = list(summary.get("stages") or [])
    status = " ".join(f"{item['stage']}:{item['status']}" for item in stages)
    artifact = str(summary.get("artifact") or "（未写出 .docx）")
    return status, _verdict(root, str(summary.get("job_id") or "")), artifact


def _verdict(root: Path, job_id: str) -> str:
    """读门禁报告：``verdict`` + **去重后**的规则清单。

    读 ``report.json`` 而不是把各阶段的判定数加起来：同一处问题会在 ``render`` 与
    ``qa`` 各判一次（两者互为独立复查，刻意的），相加会把一个毛病数成两个；报告那
    一侧已经按"同一处同一结论"去重过，它才是给人看的那一页。
    """
    from docfidelity.pipeline.stages import Workspace

    if not job_id:
        return "（没有作业）"
    path = Workspace(root, job_id).work_dir / "report.json"
    if not path.is_file():
        return "（没有报告）"
    report = _read_json(path)
    rules = sorted({str(item.get("rule_id")) for item in report.get("findings") or []})
    return f"{report.get('verdict')}（{' '.join(rules) or '无判定'}）"


def _report(rows: Sequence[tuple[str, str, str, str]], evidence: Sequence[tuple[str, str]]) -> None:
    header = ("文档", "阶段状态", "门禁结论", "产物")
    widths = [max(len(row[index]) for row in [header, *rows]) for index in range(4)]
    print()
    print("  ".join(value.ljust(widths[index]) for index, value in enumerate(header)))
    print("  ".join("-" * width for width in widths))
    for row in rows:
        print("  ".join(value.ljust(widths[index]) for index, value in enumerate(row)))
    print()
    print("同源证据（事实包摘要由输入文件内容算出；读同一来源的文档必须逐字相同）")
    for doc_key, text in evidence:
        print(f"  {doc_key}: {text}")


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
