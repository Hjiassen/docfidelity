#!/usr/bin/env python3
"""P0 验收 1–45 的**可重跑**自检入口（TASK-P0-22 / 需求第 11 章）。

为什么要有一个脚本
----------------
"逐项自检"若只是一份手写的表格，它本身就是一句声明——而本仓库的纪律是"能自动校验的
约束才是约束"（NFR-28 那一节的原话）。故这里把每条验收项**真跑一遍**：能当场判定的
给出结论，不能的显式标成 ``MANUAL`` 并写清"该做什么"，绝不折成一句"通过"。

条目表在 :mod:`acceptance_items`（45 条 × 判据 + 证据 + 判定方式，单独一个文件是为了
`check_line_limit.py` 的 800 行上限）；本模块放判定实现与 CLI。

四种判定
-------
* ``PASS``   —— 自动判定通过；
* ``FAIL``   —— 自动判定不通过；默认只有该状态使脚本以非 0 退出；
* ``SUITE``  —— 证据是测试用例；默认只列出来（``--with-tests`` 才真跑，整套要几分钟）；
* ``MANUAL`` —— 需要人做或需要外部环境（真实浏览器 / 配置模型主流程与出口取证 /
  跨环境导入 / GitHub 与 PyPI 发布权限）。

``SUITE`` 项的判定口径是"**两个套件都绿**"而不是"这一条自己的用例绿"：套件是一条命令
跑完的，逐条去挑 node id 会让一次自检变成四十五条命令（且要重跑几十次同一批用例）。
故 `--with-tests` 报的是"CI 绿"这一件事，条目表里的 `target` 负责说清是哪几条用例在管它。

用法::

    python scripts/acceptance_check.py                 # 只跑自动判定（约十几秒）
    python scripts/acceptance_check.py --with-tests    # 连测试证据一起跑
    python scripts/acceptance_check.py --only 30 42 45
    python scripts/acceptance_check.py --json          # 机器可读

**与 `README` / CI 的分工**：CI 跑的是各条门禁本身（更快、更细）；本脚本回答的是
"上游第 11 章那 45 条，今天各自靠什么成立"。两者的证据来源刻意是**同一些命令**，
免得出现"CI 绿、自检说没依据"。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Final
from urllib.parse import urlsplit

# 直接执行本文件时 `sys.path[0]` 就是 `scripts/`，但通过 `python -m` 或从别处调用时
# 不是——显式补一次，别让"换个方式跑就 ImportError"变成一条环境问题。
_SCRIPTS_DIR = str(Path(__file__).resolve().parent)
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from acceptance_items import (  # noqa: E402 - 上一步刚把 scripts/ 放进搜索路径
    ITEMS,
    REPO_ROOT,
    REQUIRED_FILES,
    SUITE_COMMANDS,
    WEB_PAGES,
    Item,
)

#: 判定状态。完整性模式也会因未验证项返回非零。
PASS: Final = "PASS"
FAIL: Final = "FAIL"
SUITE: Final = "SUITE"
MANUAL: Final = "MANUAL"

#: 源码里允许出现的 URL 宿主集合。六个都是 XML 命名空间标识符或协议常量，
#: 没有一个是网络目的地——这正是"出口只有一条"的静态侧证据（验收 30/40）。
ALLOWED_URL_HOSTS: Final = frozenset(
    {
        "purl.org",
        "schemas.microsoft.com",
        "schemas.openxmlformats.org",
        "www.w3.org",
        "json-schema.org",
        "www.apache.org",
    }
)

#: 传染性许可证（验收 23）。命中即失败，不看它是不是间接依赖的一部分。
COPYLEFT_MARKERS: Final = ("GPL", "AGPL", "SSPL", "EUPL")

#: 基础设施连接串（不是应用发起的调用）：DD-18.1 把 postgres 列为可选部署件。
#: 单列出来是为了**分类**而不是放行——它的默认值仍须为空，见 `check_single_egress_field`。
INFRASTRUCTURE_EGRESS_FIELDS: Final = frozenset({"database_url"})

#: 验收 45 的四个标记：包内随档案走的模板包目录名 / 一步导入的**路由** / 包内那两份
#: 随包附带的说明（需求 2.6.1 与 FR-M2-25 各点了一样）。
#: 路由与文件名都按**字面量**比对而不是"包含 import 字样"：``imports`` 这个词谁都写得出来，
#: 而"有没有那条端点"只有路由表能回答。同一句判据在用例侧另有一条更硬的对照
#: （``tests/api/test_server_profile_package.py`` 真的从空库导一遍）。
SEED_TEMPLATE_BUNDLE_MARKER: Final = "templates/"
IMPORT_PACK_MARKER: Final = '"/profiles/import"'
SAMPLE_INPUT_MARKER: Final = "样例输入.json"
APPLICABILITY_MARKER: Final = "适用判据.md"

#: 门禁输出里"结论那一行"的标志词，按优先级排。取最后一行会让 lint-imports 回出
#: 分层表的尾巴（"…pipeline） KEPT"），那句读起来像结论但不是结论。
_SUMMARY_MARKERS: Final = ("Contracts:", "OK ", "FAIL", "error", "found", "already formatted")


@dataclass(frozen=True)
class Outcome:
    """一次自动判定的结果。``detail`` 要写**可核对的事实**，不写"看起来没问题"。"""

    state: str
    detail: str


def _summarise(output: str) -> str:
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    for marker in _SUMMARY_MARKERS:
        for line in reversed(lines):
            if marker in line:
                return line
    return lines[-1] if lines else ""


#: 门禁子进程一律带上的环境。`src;.` 是本仓库脚本自己的写法
#: （见 `scripts/make_demo_templates.py` / `update_golden.py` 的用法行），
#: 因为这里是 src-layout 且没有 editable 安装——`lint-imports` 与
#: `make_demo_templates` 都靠它才找得到 `docfidelity`。少了它就是
#: "Could not find package 'docfidelity'"，读起来像环境坏了，其实是这里没传。
_GATE_ENV: Final = {
    **os.environ,
    "PYTHONPATH": os.pathsep.join(("src", ".")),
    "PYTHONUTF8": "1",
    "DOCFIDELITY_MODEL_ENABLED": "false",
}


def _run(argv: list[str]) -> tuple[int, str]:
    """跑一条子进程命令，回 ``(退出码, 结论行)``。"""
    completed = subprocess.run(
        argv,
        cwd=REPO_ROOT,
        env=_GATE_ENV,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return completed.returncode, _summarise(completed.stdout + "\n" + completed.stderr)


def _python() -> str:
    return sys.executable


# ============================================================ 自动判定：门禁类
def check_lint_imports() -> Outcome:
    """验收 33：无循环依赖、无越界导入（`.importlinter` 的 16 条契约全保持）。"""
    suffix = ".exe" if sys.platform == "win32" else ""
    console = Path(sys.executable).with_name(f"lint-imports{suffix}")
    argv = (
        [str(console)]
        if console.exists()
        else [_python(), "-m", "importlinter.cli", "lint-imports"]
    )
    rc, tail = _run(argv)
    return Outcome(PASS if rc == 0 else FAIL, tail)


def check_kernel_no_web() -> Outcome:
    """验收 32：内核可在无 Web 框架、无数据库的环境下独立运行。"""
    rc, tail = _run([_python(), "scripts/check_kernel_no_web.py"])
    return Outcome(PASS if rc == 0 else FAIL, tail)


def check_line_limit() -> Outcome:
    """NFR-09（验收 33 的配套）：单文件不超过 800 行。"""
    rc, tail = _run([_python(), "scripts/check_line_limit.py"])
    return Outcome(PASS if rc == 0 else FAIL, tail)


def check_schemas_reproducible() -> Outcome:
    """验收 15/26：契约 schema 可从代码重新导出且与入库版本一致。"""
    rc, tail = _run([_python(), "scripts/export_schemas.py", "--check"])
    return Outcome(PASS if rc == 0 else FAIL, tail)


def check_golden_reproducible() -> Outcome:
    """验收 10/24：golden 结构快照可复现；漂移逐份报行。"""
    rc, tail = _run([_python(), "scripts/update_golden.py", "--check"])
    return Outcome(PASS if rc == 0 else FAIL, tail)


def check_demo_templates_reproducible() -> Outcome:
    """验收 16：示例模板包可由脚本重建且与入库版本逐字节一致。"""
    rc, tail = _run([_python(), "scripts/make_demo_templates.py", "--check"])
    return Outcome(PASS if rc == 0 else FAIL, tail)


# ============================================================ 自动判定：结构类
def _scan_type_agnostic() -> list[str]:
    """验收 42 的判据本体：引擎代码里零个禁用词（含类型分支字面量）。"""
    src = str(REPO_ROOT / "src")
    if src not in sys.path:
        sys.path.insert(0, src)
    from docfidelity.qa.type_agnostic import scan_sources

    return [hit.as_text() for hit in scan_sources()]


def check_type_agnostic() -> Outcome:
    """验收 42：`contracts` / `model` / `compose` / `render` / `qa` 里没有文档类型名、领域
    概念名，也没有针对具体 `kind` 的分支（任务原文的四个模块都在默认扫描根之内）。"""
    try:
        hits = _scan_type_agnostic()
    except Exception as exc:  # 检索不可用必须失败，不得读成"没有命中"
        return Outcome(FAIL, f"类型无关检索无法执行：{type(exc).__name__}: {exc}")
    if hits:
        return Outcome(FAIL, f"{len(hits)} 处命中，首条：{hits[0]}")
    return Outcome(PASS, "禁用词命中 0（源码 AST，含 branch_literal）")


def check_no_hardcoded_outbound() -> Outcome:
    """验收 30 的静态子检查：源码里没有指向网络目的地的 URL 字面量。

    XML 命名空间（`schemas.openxmlformats.org` 那一类）与协议常量不算——它们是标识符，
    不会被请求。真正要挡的是"顺手写死一个外部服务地址"，因为它绕过了唯一出口
    `settings.model_base_url`。
    """
    offenders: list[str] = []
    for root in ("src/docfidelity", "server"):
        for path in (REPO_ROOT / root).rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            for match in re.finditer(r"https?://[^\s\"')\]]+", text):
                host = urlsplit(match.group(0)).hostname or ""
                if host not in ALLOWED_URL_HOSTS:
                    offenders.append(f"{path.relative_to(REPO_ROOT)}: {match.group(0)}")
    if offenders:
        return Outcome(FAIL, f"{len(offenders)} 处硬编码外网地址，首条：{offenders[0]}")
    return Outcome(
        PASS, f"0 处硬编码外网地址（白名单 = {len(ALLOWED_URL_HOSTS)} 个 XML 命名空间宿主）"
    )


def check_single_egress_field() -> Outcome:
    """验收 30/40：「出口白名单只有一条」在**配置模型**上成立。

    判据是"恰好一个**应用层**字段能装一个出网地址，且数据边界视图由它推出"。另立一个
    开关就允许"声称无外发、实际有"——那正是这一条要堵的形状。

    `database_url` 单列成**基础设施**连接串（DD-18.1 把 postgres 列为可选）：它指向的是
    运维自己选的库，不是应用发起的调用，故不进 `data_boundary.external_calls`。但它的
    默认值必须是空的——内置一个远端库才是真正的越界。
    """
    if str(REPO_ROOT / "src") not in sys.path:
        sys.path.insert(0, str(REPO_ROOT / "src"))
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    from server.settings import ServerSettings

    url_fields = {
        name
        for name in ServerSettings.model_fields
        if name.endswith(("url", "endpoint", "base_url"))
    }
    app_fields = sorted(url_fields - INFRASTRUCTURE_EGRESS_FIELDS)
    if app_fields != ["model_base_url"]:
        return Outcome(
            FAIL,
            f"应用层能装出网地址的字段是 {app_fields}，期望只有 ['model_base_url']"
            f"（基础设施字段白名单 {sorted(INFRASTRUCTURE_EGRESS_FIELDS)}）",
        )

    default_db = ServerSettings.model_fields["database_url"].default
    if default_db not in (None, ""):
        return Outcome(
            FAIL, f"database_url 带了一个内置默认值 {default_db!r}——那是硬编码的数据出口"
        )

    # 三种组合都要对上：漏掉"开关关着但填了端点"那一档，就会放过一个真正的越界。
    cases = (
        (True, "http://127.0.0.1:1/v1", True),
        (True, None, False),
        (False, "http://127.0.0.1:1/v1", False),
    )
    actual = [
        ServerSettings(_env_file=None, model_enabled=enabled, model_base_url=url)
        .data_boundary()
        .external_calls
        for enabled, url, _ in cases
    ]
    expected = [flag for _, _, flag in cases]
    if actual != expected:
        return Outcome(FAIL, f"external_calls 不是由「开关 + 端点」推出的：{actual} ≠ {expected}")
    return Outcome(
        PASS,
        "应用层唯一出网字段 = model_base_url；database_url 为基础设施项且无内置默认值；"
        "external_calls 由开关与端点合取推出（三档组合逐一核对）",
    )


def check_env_not_committed() -> Outcome:
    """验收 30 的配套：`.env` 与数据目录**不在版本控制里**，且已被忽略。"""
    rc, _ = _run(["git", "ls-files", "--error-unmatch", ".env"])
    if rc == 0:
        return Outcome(FAIL, "`.env` 已被纳入版本控制——密钥会随仓库分发")
    uncovered = [
        name for name in (".env", ".devdata") if _run(["git", "check-ignore", "-q", name])[0] != 0
    ]
    if uncovered:
        return Outcome(FAIL, f"未入库也未被忽略，随时可能被 `git add .` 带进去：{uncovered}")
    return Outcome(PASS, "`.env` 与 `.devdata` 均未入库，且被 .gitignore 覆盖")


def check_open_source_prereqs() -> Outcome:
    """验收 20/31：`git clone` 之后能跑起来所需的文件一件不少，且阶段提供者的入口点已声明。"""
    import tomllib

    missing = [name for name in REQUIRED_FILES if not (REPO_ROOT / name).is_file()]
    if missing:
        return Outcome(FAIL, f"缺 {len(missing)} 件：{missing}")

    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    stages = data.get("project", {}).get("entry-points", {}).get("docfidelity.build_stages", {})
    if not stages:
        return Outcome(FAIL, "未声明 docfidelity.build_stages 入口点 ⇒ 装完也发起不了构建")
    if not data.get("project", {}).get("scripts"):
        return Outcome(FAIL, "未声明 [project.scripts]")
    return Outcome(PASS, f"{len(REQUIRED_FILES)} 件齐备；入口点 {sorted(stages)}")


def check_license_compat() -> Outcome:
    """验收 23：`LICENSE` 是宽松许可，且**直接依赖**里没有传染性许可。"""
    import tomllib

    license_text = (REPO_ROOT / "LICENSE").read_text(encoding="utf-8")
    if "Apache License" not in license_text:
        return Outcome(FAIL, "LICENSE 不是 Apache-2.0（验收 23 要求与直接依赖兼容）")

    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    names = [
        re.split(r"[\[<>=!; ]", spec, maxsplit=1)[0].strip()
        for spec in data.get("project", {}).get("dependencies", [])
    ]
    from importlib.metadata import PackageNotFoundError, metadata

    offenders: list[str] = []
    for name in names:
        try:
            info = metadata(name)
        except PackageNotFoundError:
            offenders.append(f"{name}（未安装，许可未知）")
            continue
        declared = " ".join(
            filter(
                None, [info.get("License-Expression"), info.get("License"), info.get("Classifier")]
            )
        )
        up = declared.upper()
        if any(marker in up for marker in COPYLEFT_MARKERS):
            offenders.append(f"{name}: {declared.strip()[:60]}")
    if offenders:
        return Outcome(FAIL, f"传染性/未知许可：{offenders}")
    return Outcome(PASS, f"LICENSE = Apache-2.0；{len(names)} 个直接依赖均无传染性许可")


def check_schema_families() -> Outcome:
    """验收 26：对外格式**均自带版本标识**（8 个家族逐个查 `format_version`）。"""
    families = sorted(p.name for p in (REPO_ROOT / "docs" / "spec").iterdir() if p.is_dir())
    missing = [
        name
        for name in families
        if "format_version"
        not in (REPO_ROOT / "docs" / "spec" / name / "schema.json").read_text(encoding="utf-8")
    ]
    if missing:
        return Outcome(FAIL, f"{missing} 的 schema 里没有 format_version")
    if len(families) < 8:
        return Outcome(FAIL, f"只导出 {len(families)} 个家族：{families}")
    return Outcome(PASS, f"{len(families)} 个家族全部带 format_version：{', '.join(families)}")


def check_web_pages() -> Outcome:
    """验收 27/35：十四页都在，且每一页都在 `routes.ts` 里有一条路由常量。"""
    pages_dir = REPO_ROOT / "web" / "src" / "pages"
    names = {path.name for path in pages_dir.glob("*.tsx")}
    missing_pages = [page for page in WEB_PAGES if not any(name.startswith(page) for name in names)]
    if missing_pages:
        return Outcome(FAIL, f"缺 {len(missing_pages)} 页：{missing_pages}")

    routes = (REPO_ROOT / "web" / "src" / "app" / "routes.ts").read_text(encoding="utf-8")
    # 路由键就是 `pNN`（`routes.ts` 的取名约定），逐页查一次——页面写好了却没接路由，
    # 是"实现完了但进不去"，只有把两边都查一遍才发现得了。
    missing_routes = [page.lower() for page in WEB_PAGES if f"{page.lower()}" not in routes]
    if missing_routes:
        return Outcome(FAIL, f"这些页在 routes.ts 里没有路由键：{missing_routes}")
    return Outcome(
        MANUAL,
        f"{len(WEB_PAGES)} 页与路由齐备；本机浏览器实测见验收说明，本脚本不自动验证环境与交互",
    )


def check_demo_assets_cover_types() -> Outcome:
    """验收 43/44：示例资产覆盖 ≥2 个模板包、≥3 类文档类型，且含一类非软件域。

    "非软件域"这一条**不由本脚本判定**（谁是软件域谁是别的域，没有可自动检索的判据），
    它靠 `demo-operations` 的 `doc_type` 声明与 `template.json` 的 `notes` 自述；
    这里只钉住"至少有两套出身不同的模板包 + 三份档案"这个下界。
    """
    templates = sorted(
        p.parent.name for p in (REPO_ROOT / "demo-templates").glob("*/template.json")
    )
    profiles = sorted(p.stem for p in (REPO_ROOT / "examples" / "profiles").glob("*.json"))
    if len(templates) < 2:
        return Outcome(FAIL, f"示例模板包只有 {templates}")
    if len(profiles) < 3:
        return Outcome(FAIL, f"示例档案只有 {profiles}")
    return Outcome(
        MANUAL,
        f"模板包 {len(templates)} 套（{', '.join(templates)}）× 档案 {len(profiles)} 份"
        f"（{', '.join(profiles)}）；资产数量不能证明事实与样式保真，需核验三类产物及零代码接入过程",
    )


def check_profile_exchange() -> Outcome:
    """验收 45：档案连同模板包导出后，收件方**导入即可复现**（FR-M2-25 / 需求 2.6.1）。

    四件事各自可核对，缺任一条那句话都只是说法：

    ① 导出端点存在，且包内既有档案本体（含已算好的指纹）**也有它引用的模板包**——
       只发档案 JSON 的话，收件方还得自己去找模板，那不是"打包交付"；
    ② 有**一步导入**的通路（`POST /profiles/import`），把包丢进去即可复现；
    ③ 包内带**适用判据说明**（需求 2.6.1 明确要求导出件必须携带）；
    ④ 包内带**自制样例输入**（FR-M2-25 的另一件）。

    这里查的是**源码里的接线**（端点声明了没有、包里打了哪几样）。"它真的能从空库
    导进去"由 ``tests/api/test_server_profile_package.py`` 用真资产走一遍——本项
    在 ``--with-tests`` 下与那个文件一起给出结论。静态判据永远只说明"接线在"，
    说不了"通"；本仓库的分工一贯如此（样板见 ``tests/unit/test_gates.py``）。
    """
    api_dir = REPO_ROOT / "server" / "api"
    profiles_api = (api_dir / "profiles.py").read_text(encoding="utf-8")
    # 包内带了哪几样东西的判据在**档案包模块**里（``server/profile_package.py``），
    # 登记与两个入口在 ``server/profiles.py``。两处都读：只看登记那一个文件的话，
    # "包里有没有模板包"这条判据会变成查一个不负责打包的文件。
    package_svc = (REPO_ROOT / "server" / "profile_package.py").read_text(encoding="utf-8")
    profiles_svc = (REPO_ROOT / "server" / "profiles.py").read_text(encoding="utf-8")

    gaps: list[str] = []
    if "/export" not in profiles_api:
        gaps.append("没有档案导出端点")
    if APPLICABILITY_MARKER not in package_svc:
        gaps.append("导出件里没有「适用判据.md」（需求 2.6.1）")
    if SAMPLE_INPUT_MARKER not in package_svc:
        gaps.append("导出件里没有自制样例输入（FR-M2-25）")
    if SEED_TEMPLATE_BUNDLE_MARKER not in package_svc + profiles_svc:
        gaps.append("导出件里没有随档案一起走的模板包（收件方还得自己找）")
    if IMPORT_PACK_MARKER not in profiles_api:
        gaps.append("没有「导入档案包」的一步通路（FR-M2-25 的另一半）")

    if gaps:
        return Outcome(FAIL, "；".join(gaps))
    return Outcome(
        MANUAL,
        "导出 / 导入接线齐备；tests/api/test_server_profile_package.py 覆盖空库往返；"
        "仍需在另一环境导入后构建并比对归一化产物，源码接线不能证明可复现",
    )


#: 自动判定项的名字 → 实现。名字在条目表里引用。
AUTO_CHECKS: Final[dict[str, Callable[[], Outcome]]] = {
    "lint-imports": check_lint_imports,
    "kernel-no-web": check_kernel_no_web,
    "line-limit": check_line_limit,
    "schemas-reproducible": check_schemas_reproducible,
    "golden-reproducible": check_golden_reproducible,
    "demo-templates-reproducible": check_demo_templates_reproducible,
    "type-agnostic": check_type_agnostic,
    "no-hardcoded-outbound": check_no_hardcoded_outbound,
    "single-egress-field": check_single_egress_field,
    "env-not-committed": check_env_not_committed,
    "open-source-prereqs": check_open_source_prereqs,
    "license-compat": check_license_compat,
    "schema-families": check_schema_families,
    "web-pages": check_web_pages,
    "demo-assets-cover-types": check_demo_assets_cover_types,
    "profile-exchange": check_profile_exchange,
}

#: 一条验收项可能由**多个**检查共同成立：验收 33 的判据原文点名了 `lint-imports`
#: **与**行数上限，验收 30 的判据原文是"唯一出网字段 … **+** `.env` 未入库"。
#: 所以 `Item.target` 允许用 `;` 分隔多个检查名，逐个跑、结论里逐条列出。
#:
#: 为什么要留这个口子：判定循环原先只取 `item.target` 一个名字，于是登记进
#: `AUTO_CHECKS` 却没被任何条目指向的检查**注册了却永远不执行**——而它在报告里
#: 与"查过且通过"长得一模一样。判据原文写了三件事、检查只做了一件，是最难发现的一类
#: 静默缺口（`--json` 输出里 `detail` 那句话会替它掩盖）。
TARGET_SEPARATOR: Final = ";"


def _targets(item: Item) -> list[str]:
    """一条 `kind=auto` 的验收项要跑哪几个检查。"""
    return [name.strip() for name in item.target.split(TARGET_SEPARATOR) if name.strip()]


def _run_suites() -> dict[str, tuple[bool, str]]:
    """跑两条测试命令，回每个套件的 ``(是否通过, 摘要)``。

    ``--basetemp`` **必须显式给到仓库外**：本机在默认 ``%TEMP%/pytest-of-*`` 的清理上
    `pytest_sessionfinish` 会卡死（P0-18 实测 RC=124、日志停在 `[100%]` 点阵，而用例
    其实全绿）。不传的话 `--with-tests` 会表现成"跑不完"，读起来像慢，其实是挂住。
    目录挂在系统临时目录下且每次运行换名，两条命令互不打架、仓库里也不留东西。
    """
    results: dict[str, tuple[bool, str]] = {}
    for name, selector in SUITE_COMMANDS:
        basetemp = Path(tempfile.gettempdir()) / f"docfidelity-acceptance-{name}"
        argv = [_python(), "-m", "pytest", *selector, f"--basetemp={basetemp}"]
        rc, summary = _run(argv)
        results[name] = (rc == 0, summary or f"退出码 {rc}")
    return results


def _format_table(rows: Iterable[tuple[str, ...]], headers: tuple[str, ...]) -> str:
    widths = [len(header) for header in headers]
    materialised = [list(row) for row in rows]
    for row in materialised:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))
    lines = [
        "  ".join(header.ljust(widths[index]) for index, header in enumerate(headers)).rstrip(),
        "  ".join("-" * width for width in widths),
    ]
    for row in materialised:
        lines.append(
            "  ".join(cell.ljust(widths[index]) for index, cell in enumerate(row)).rstrip()
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="P0 验收 1–45 自检（TASK-P0-22）")
    parser.add_argument("--only", nargs="*", type=int, default=None, help="只检查这几项")
    parser.add_argument("--with-tests", action="store_true", help="连测试证据一起跑（几分钟）")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出")
    parser.add_argument(
        "--require-complete", action="store_true", help="存在未运行或未验证项时也以非零退出"
    )
    args = parser.parse_args(argv)

    selected = [item for item in ITEMS if args.only is None or item.number in args.only]

    outcomes: dict[str, Outcome] = {}
    wanted = {name for item in selected if item.kind == "auto" for name in _targets(item)}
    for name in sorted(wanted):
        try:
            outcomes[name] = AUTO_CHECKS[name]()
        except Exception as exc:  # 检查自身炸了也是 FAIL，不得读成"通过"
            outcomes[name] = Outcome(FAIL, f"检查无法执行：{type(exc).__name__}: {exc}")

    suites: dict[str, tuple[bool, str]] = {}
    if args.with_tests:
        suites = _run_suites()

    rows: list[tuple[str, ...]] = []
    failures: list[int] = []
    for item in selected:
        if item.kind == "auto":
            checks = [(name, outcomes[name]) for name in _targets(item)]
            # 多个检查共同成立时，**全过才算过**；结论里逐条列出，免得一句"通过"
            # 盖住其中一条其实没跑或没过。
            states = {outcome.state for _, outcome in checks}
            state = FAIL if FAIL in states else MANUAL if MANUAL in states else PASS
            detail = "；".join(
                outcome.detail if len(checks) == 1 else f"{name}: {outcome.detail}"
                for name, outcome in checks
            )
        elif item.kind == "suite":
            if not suites:
                state, detail = SUITE, item.evidence
            else:
                ok = all(passed for passed, _ in suites.values())
                state = PASS if ok else FAIL
                detail = "；".join(f"{name}: {summary}" for name, (_, summary) in suites.items())
        else:
            state, detail = MANUAL, item.target
        if state == FAIL:
            failures.append(item.number)
        rows.append((str(item.number), item.title, state, detail))

    if args.json:
        print(
            json.dumps(
                [
                    {
                        "number": item.number,
                        "title": item.title,
                        "how": item.how,
                        "evidence": item.evidence,
                        "state": row[2],
                        "detail": row[3],
                    }
                    for item, row in zip(selected, rows, strict=True)
                ],
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        print("P0 验收自检（需求第 11 章 45 项 / TASK-P0-22）\n")
        print(_format_table(rows, ("#", "验收项", "判定", "依据")))
        print()
        counts: dict[str, int] = {}
        for row in rows:
            counts[row[2]] = counts.get(row[2], 0) + 1
        print(
            "汇总："
            + "  ".join(f"{state} {counts.get(state, 0)}" for state in (PASS, FAIL, SUITE, MANUAL))
        )
        if failures:
            print(f"\nFAIL 项：{failures}")
        if not args.with_tests:
            print("\n（SUITE 项只是列了证据，未执行；加 --with-tests 真跑。）")

    incomplete = args.require_complete and any(row[2] != PASS for row in rows)
    return 1 if failures or incomplete else 0


if __name__ == "__main__":
    sys.exit(main())
