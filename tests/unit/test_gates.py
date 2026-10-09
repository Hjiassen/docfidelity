"""门禁脚本的自检：正向通过 + 负向能抓到违规。

为什么需要这个文件：「门禁通过」本身不构成证明。一个永远返回 0 的检查器，
和一条真正生效的检查器，输出是一样的。所以这里对三个门禁脚本做**双向**断言。

三类门禁的判定逻辑位置不同，自检的层次也就不同：

* ``check_line_limit`` / ``check_kernel_no_web`` 的判定内联在脚本里 —— 直接测其
  核心函数。
* ``check_forbidden_words`` 已改为**薄壳**：判定逻辑只有一处实现
  （``qa.type_agnostic``），发现力由 ``test_qa_type_agnostic`` 覆盖。这里只自检
  薄壳的**接线** —— 壳若把「命中」或「检索不可用」翻译成退出码 0，就诞生了一个
  永远通过的 CI 门禁，而且没人看得出来。
"""

from __future__ import annotations

import ast
import importlib
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_script(module_name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        module_name, REPO_ROOT / "scripts" / f"{module_name}.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # 必须先注册到 sys.modules：@dataclass 会按 cls.__module__ 反查模块命名空间
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


forbidden = _load_script("check_forbidden_words")
line_limit = _load_script("check_line_limit")
kernel = _load_script("check_kernel_no_web")
acceptance = _load_script("acceptance_check")

# 禁止词门禁的判定逻辑并不在脚本里，而在唯一实现处。薄壳自检只针对它，故直接
# 持有该模块句柄，用 monkeypatch 替换它的扫描器，观察薄壳翻译出的退出码。
type_agnostic = importlib.import_module("docfidelity.qa.type_agnostic")


class TestForbiddenWordsGate:
    """禁止词门禁是**薄壳**：判定逻辑只有一处实现（``qa.type_agnostic``），
    发现力由 ``test_qa_type_agnostic`` 覆盖。

    这里只自检薄壳的**接线** —— 壳若把「命中」或「检索不可用」翻译成退出码 0，
    就诞生了一个永远通过的 CI 门禁，而且没人看得出来。
    """

    def test_clean_repository_exits_zero(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert forbidden._run(["check_forbidden_words.py"]) == 0
        assert "OK" in capsys.readouterr().out

    def test_a_hit_is_reported_and_exits_non_zero(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        def fake_scanner(_roots: object) -> list[type_agnostic.Hit]:
            return [
                type_agnostic.Hit("src/x.py", 7, "doc_type_name", "详细设计", "字符串字面量命中")
            ]

        monkeypatch.setattr(type_agnostic, "scan_sources", fake_scanner)
        assert forbidden._run(["check_forbidden_words.py"]) == 1
        out = capsys.readouterr().out
        assert "FAIL" in out
        assert "src/x.py:7" in out

    def test_unavailable_scanner_fails_loudly(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        def boom(_roots: object) -> list[type_agnostic.Hit]:
            raise RuntimeError("检索不可用")

        monkeypatch.setattr(type_agnostic, "scan_sources", boom)
        assert forbidden._run(["check_forbidden_words.py"]) == 1
        assert "FAIL" in capsys.readouterr().out

    def test_missing_wordlist_fails_loudly(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        tmp_path: Path,
    ) -> None:
        monkeypatch.setattr(type_agnostic, "WORDLIST_PATH", tmp_path / "missing.txt")
        assert forbidden._run(["check_forbidden_words.py"]) == 1
        assert "FAIL" in capsys.readouterr().out


class TestKernelNoWebGate:
    def test_detects_plain_import(self) -> None:
        assert kernel._violations_in(ast.parse("import fastapi\n")) == [(1, "fastapi")]

    def test_detects_from_import(self) -> None:
        claims = kernel._violations_in(ast.parse("from sqlalchemy.orm import Session\n"))
        assert claims == [(1, "sqlalchemy")]

    def test_detects_random(self) -> None:
        assert kernel._violations_in(ast.parse("import random\n")) == [(1, "random")]

    def test_allows_relative_import(self) -> None:
        assert kernel._violations_in(ast.parse("from .base import FormatModel\n")) == []

    def test_allows_own_package_import(self) -> None:
        assert kernel._violations_in(ast.parse("from docfidelity.contracts import ir\n")) == []


class TestLineLimitGate:
    def test_threshold_is_800(self) -> None:
        assert line_limit.MAX_LINES == 800

    @pytest.mark.parametrize(
        ("content", "expected"),
        [
            ("", 0),
            ("a\n", 1),
            ("a\nb\n", 2),
            ("a\nb", 2),
            ("a\nb\nc", 3),
        ],
    )
    def test_counts_lines(self, tmp_path: Path, content: str, expected: int) -> None:
        target = tmp_path / "sample.py"
        target.write_text(content, encoding="utf-8")
        assert line_limit._count_lines(target) == expected

    def test_repository_passes(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert line_limit.main(["check_line_limit.py"]) == 0
        assert "OK" in capsys.readouterr().out


class TestAcceptanceRegister:
    """P0 验收自检的**登记表**自检（TASK-P0-22）。

    这里不跑那 45 条（要几分钟，且由 `scripts/acceptance_check.py --with-tests` 负责），
    只钉住三件会让自检**静默失效**的事：

    ① 登记进 `AUTO_CHECKS` 却没有任何验收项指向它 —— 检查写了却永远不执行，而它在报告里
       与"查过且通过"长得一模一样（本轮实测踩到：`line-limit` 与 `env-not-committed`
       两个检查躺在字典里 Never called，因为判定循环只取 `item.target` 一个名字）。
       验收 33 的判据原文本来就点名了 `lint-imports` 与行数上限**两件事**；
    ② 验收项指向一个不存在的检查名 —— 会被 `main` 的兜底 `except` 收成 FAIL，但那时错误
       信息是 `KeyError`，读起来像环境坏了；
    ③ 编号缺漏或重复 —— 45 条少一条，汇总行照样打印"PASS 44"，没人会数。
    """

    def test_numbers_are_one_to_forty_five(self) -> None:
        assert [item.number for item in acceptance.ITEMS] == list(range(1, 46))

    def test_static_web_inventory_does_not_prove_browser_acceptance(self) -> None:
        assert acceptance.check_web_pages().state == acceptance.MANUAL

    def test_static_exchange_wiring_does_not_prove_cross_environment_reproduction(self) -> None:
        assert acceptance.check_profile_exchange().state == acceptance.MANUAL

    def test_one_click_requires_a_real_container_run(self) -> None:
        item = next(item for item in acceptance.ITEMS if item.number == 31)
        assert item.kind == "manual"

    def test_manual_evidence_is_preserved_in_cli_results(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setitem(
            acceptance.AUTO_CHECKS,
            "web-pages",
            lambda: acceptance.Outcome(acceptance.MANUAL, "需浏览器操作验证"),
        )
        assert acceptance.main(["--only", "27", "--json"]) == 0
        rows = json.loads(capsys.readouterr().out)
        assert rows[0]["state"] == acceptance.MANUAL

    def test_release_gate_rejects_incomplete_evidence(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert acceptance.main(["--only", "21", "--require-complete"]) == 1

    def test_every_registered_check_is_reachable(self) -> None:
        referenced = {
            name
            for item in acceptance.ITEMS
            if item.kind == "auto"
            for name in acceptance._targets(item)
        }
        assert set(acceptance.AUTO_CHECKS) - referenced == set()

    def test_every_target_names_a_registered_check(self) -> None:
        referenced = {
            name
            for item in acceptance.ITEMS
            if item.kind == "auto"
            for name in acceptance._targets(item)
        }
        assert referenced - set(acceptance.AUTO_CHECKS) == set()

    def test_suite_selector_keeps_the_marker_expression_whole(self) -> None:
        """`-m "unit or integration or replay"` 必须**整串**占一个 argv 元素。

        被按空白切开之后 pytest 会去找名叫 `or` 的目录，报
        `file or directory not found: or` —— 看起来像环境问题，其实是这里少了一层引号。
        """
        for _, argv in acceptance.SUITE_COMMANDS:
            assert argv.index("-m") >= 0
            assert argv[argv.index("-m") + 1].strip() != ""
            assert "or" not in argv, argv
