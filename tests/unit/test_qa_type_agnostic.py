"""类型无关检索器：AST 三层命中、白名单、**扫不到文件不得静默通过**（DD-10.6）。

「引擎与文档类型无关」要成为工程属性，唯一办法是可自动检索。故本模块直接验证
检索器的**发现力**（真的能在源码里抓到文档类型名 / 领域概念标识符 / 类型分支字面量），
以及它的三条**防漏**设计：

1. ``docstring`` 不算命中——注释与说明文字里必然出现"文档类型"这类词，文本检索会
   把规则变成"总在误报"，最终被人关掉。
2. ``trace`` / ``origin`` 不算命中——那两个字段存 ``kind:key`` 引用句柄，其中出现
   文档类型名是**数据**（DD-3.2（2）的已知边界）；不剔除的话，任何带追溯的 IR 都会被判违规。
3. **扫不到文件即报错**，不返回空列表——"命中 0"与"根本没扫"在返回值上完全一样，
   那是这条门禁最危险的失效方式（它会让 CI 一路绿灯）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from docfidelity.contracts.errors import ConfigurationError
from docfidelity.contracts.ir import ContentIR, ContentUnit, OriginRef
from docfidelity.qa import TYPE_AGNOSTIC_RULE_ID, Hit, load_wordlist, scan, scan_ir, scan_sources
from docfidelity.qa.type_agnostic import (
    EXCLUDE_PARTS,
    KIND_DISCRIMINATOR_ATTRS,
    SCAN_ROOTS,
    WHITELIST_DIRS,
    WHITELIST_FILES,
    WORDLIST_PATH,
)

pytestmark = pytest.mark.unit


def _clean_ir() -> ContentIR:
    return ContentIR(
        project_id="probe",
        doc_key="probe",
        profile_id="probe-doc",
        template_ref="TPL@1.0",
        units=[ContentUnit(unit_id="c1#p0", type="para", text="一段没有任何类型名的正文")],
    )


# ------------------------------------------------------------------ 词表
def test_wordlist_has_the_three_groups() -> None:
    groups = load_wordlist()
    assert set(groups) == {"doc_type_name", "domain_concept", "branch_literal"}
    assert all(groups.values())


def test_wordlist_path_is_the_single_source() -> None:
    """CI 脚本与门禁规则都读它——两处各写一份必然漂移。"""
    assert WORDLIST_PATH.is_file()
    assert WORDLIST_PATH.name == "forbidden_words.txt"


def test_scan_roots_default_to_the_whole_kernel() -> None:
    assert SCAN_ROOTS == ("src/docfidelity",)
    assert "build" in EXCLUDE_PARTS and ".venv" in EXCLUDE_PARTS


def test_whitelist_covers_data_directories() -> None:
    """示例档案与夹具必然出现类型名，因为那是**数据**而不是引擎代码。"""
    assert "tests/fixtures/" in WHITELIST_DIRS
    assert "docs/" in WHITELIST_DIRS
    assert "src/docfidelity/contracts/domain.py" in WHITELIST_FILES


# ------------------------------------------------------------------ IR 侧
def test_clean_ir_reports_nothing() -> None:
    assert scan_ir(_clean_ir()) == []


def test_doc_type_name_in_the_engine_surface_is_found() -> None:
    ir = ContentIR(
        project_id="probe",
        doc_key="probe",
        profile_id="probe-doc",
        template_ref="TPL@1.0",
        units=[ContentUnit(unit_id="概要设计#1", type="para", text="正文")],
    )
    hits = scan_ir(ir)
    assert hits
    assert all(hit.path == "<ir>" and hit.line == 0 for hit in hits)
    assert any(hit.word == "概要设计" for hit in hits)


def test_trace_handles_are_whitelisted() -> None:
    """``trace`` 存 ``kind:key`` 引用句柄，属**数据**。"""
    ir = ContentIR(
        project_id="probe",
        doc_key="probe",
        profile_id="probe-doc",
        template_ref="TPL@1.0",
        units=[ContentUnit(unit_id="c1#p0", type="para", text="正文", trace=["概要设计:1"])],
    )
    assert scan_ir(ir) == []


def test_origin_handles_are_whitelisted() -> None:
    ir = ContentIR(
        project_id="probe",
        doc_key="probe",
        profile_id="probe-doc",
        template_ref="TPL@1.0",
        units=[
            ContentUnit(
                unit_id="c1#p0",
                type="para",
                text="正文",
                origin=OriginRef(source="详细设计", locator="row 3"),
            )
        ],
    )
    assert scan_ir(ir) == []


def test_fact_values_are_not_the_engine_surface() -> None:
    """事实取值不属于**引擎表面**：事实源里写着类型名并不违反任何承诺。

    约束的是"引擎的词汇"而不是"使用者的数据"——把数据也判成 blocker 会让
    合法事实无法接入。
    """
    ir = ContentIR(
        project_id="probe",
        doc_key="probe",
        profile_id="probe-doc",
        template_ref="TPL@1.0",
        units=[ContentUnit(unit_id="c1#p0", type="para", text="本文是一份详细设计的说明")],
    )
    assert scan_ir(ir) == []


# ------------------------------------------------------------------ 源码侧
def test_repository_kernel_is_clean() -> None:
    """真仓库扫一遍：内核里不得出现文档类型名或领域概念名。"""
    assert scan_sources() == []


def test_missing_roots_raise_instead_of_reporting_success() -> None:
    """扫不到文件必须报错——否则"没扫"与"没命中"无法区分，CI 会一路绿灯。"""
    with pytest.raises(ConfigurationError):
        scan_sources(["no/such/directory"])
    with pytest.raises(ConfigurationError):
        scan_sources(["tests/fixtures"])  # 全是白名单目录 → 一个文件都没读


def test_scan_combines_source_and_ir(tmp_path: Path) -> None:
    source = tmp_path / "probe.py"
    source.write_text('"""详细设计。"""\n', encoding="utf-8")
    # 只放 docstring：本身不算命中
    assert scan_sources([str(tmp_path)]) == []
    assert scan(roots=[str(tmp_path)], ir=_clean_ir()) == []


# ------------------------------------------------------------------ 发现力
def test_doc_type_name_literal_is_found(tmp_path: Path) -> None:
    source = tmp_path / "probe.py"
    source.write_text('LABEL = "详细设计"\n', encoding="utf-8")
    hits = scan_sources([str(tmp_path)])
    assert [(hit.category, hit.word) for hit in hits] == [("doc_type_name", "详细设计")]


def test_docstring_is_not_a_hit(tmp_path: Path) -> None:
    """注释与说明文字里必然出现这些词；把它们判成违规会让规则被关掉。"""
    source = tmp_path / "probe.py"
    source.write_text('"""本模块处理详细设计模板。"""\n\nX = 1\n', encoding="utf-8")
    assert scan_sources([str(tmp_path)]) == []


def test_domain_concept_identifier_is_found(tmp_path: Path) -> None:
    source = tmp_path / "probe.py"
    source.write_text("def f(字段):\n    return 字段\n", encoding="utf-8")
    hits = scan_sources([str(tmp_path)])
    assert {hit.category for hit in hits} == {"domain_concept"}
    assert {hit.word for hit in hits} == {"字段"}


def test_branch_literal_is_found(tmp_path: Path) -> None:
    """只查 ``x.kind == "..."`` / ``x.doc_type == "..."`` 形式的类型分支。"""
    source = tmp_path / "probe.py"
    source.write_text('def f(x):\n    if x.kind == "table":\n        return 1\n', encoding="utf-8")
    hits = scan_sources([str(tmp_path)])
    assert [(hit.category, hit.word) for hit in hits] == [("branch_literal", "table")]


def test_engine_own_enum_branch_is_not_a_hit(tmp_path: Path) -> None:
    """``x.unit == "…"`` 这类引擎自有封闭枚举的判别不属于"类型分支"。

    否则连 ``UnitType`` 都判不了——门禁会把引擎判自己内容的代码也判违规。
    """
    source = tmp_path / "probe.py"
    source.write_text('def f(x):\n    if x.unit == "table":\n        return 1\n', encoding="utf-8")
    assert scan_sources([str(tmp_path)]) == []


def test_membership_test_is_still_a_branch_hit(tmp_path: Path) -> None:
    """``x.kind in ("a", "table")`` 与 ``x.kind == "table"`` 同义，须同样命中。"""
    source = tmp_path / "probe.py"
    source.write_text(
        'def f(x):\n    if x.kind in ("a", "table"):\n        return 1\n', encoding="utf-8"
    )
    hits = scan_sources([str(tmp_path)])
    assert [(hit.category, hit.word) for hit in hits] == [("branch_literal", "table")]


def test_domain_concept_literal_in_a_message_is_not_a_hit(tmp_path: Path) -> None:
    """中文错误文案里必然出现"字段"这类词——只查标识符，不查字符串字面量。"""
    source = tmp_path / "probe.py"
    source.write_text('raise ValueError("缺少字段")\n', encoding="utf-8")
    assert scan_sources([str(tmp_path)]) == []


def test_branch_only_counts_declared_discriminators() -> None:
    assert frozenset({"kind", "doc_type"}) == KIND_DISCRIMINATOR_ATTRS


def test_syntax_error_is_surfaced_not_swallowed(tmp_path: Path) -> None:
    """坏文件必须显式失败：静默跳过等于"这个文件没检查"。"""
    source = tmp_path / "broken.py"
    source.write_text("def f(:\n", encoding="utf-8")
    hits = scan_sources([str(tmp_path)])
    assert [hit.category for hit in hits] == ["syntax_error"]


# ------------------------------------------------------------------ 命中记录
def test_hit_as_text_is_actionable() -> None:
    hit = Hit(path="src/x.py", line=12, category="branch_literal", word="table", detail="判别分支")
    text = hit.as_text()
    assert "src/x.py:12" in text
    assert "branch_literal" in text
    assert "table" in text


def test_rule_id_is_the_catalogue_one() -> None:
    assert TYPE_AGNOSTIC_RULE_ID == "FR-M5-17"
