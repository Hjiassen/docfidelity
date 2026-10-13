"""golden 结构快照：示例资产的形态与可复现性（DD-17.1 / DD-17.2 / TASK-P0-19）。

判据
----
1. 三份示例产物（同源多型）的结构与 ``tests/golden/<doc_key>.json`` 一致；
2. **连续 3 次构建逐字节相同**——DD-17.1 那句"连续 3 次构建幂等"的最严口径；
3. 基线覆盖每一声明的 ``doc_key``（新增文档忘了生成基线要当场红，而不是悄悄少一份）。

为什么"结构"和"字节"两样都要
----------------------------
结构快照是给人看的差异（"段落 172 → 168、表 14 → 13 条"）；字节比对说不出哪里变了。
而字节比对能抓住结构快照**永远看不见**的一件事——它的投影刻意对易变标识（时间戳、
关系 ID、书签编号）不敏感，于是"结构没变但产物其实不可复现"在它眼里是完全正常的。
缺任一个都有盲区。

**不碰 Word**：示例构建在 payload 里固定 ``office_enabled=false``
（``examples/build_demo.py`` 的 ``--office`` 默认关），故本层在无 Office 的 CI 上
结论一致。这不是图省事——本机装没装 Word 会改变产物里的目录页码，把它当输入就等于
让"基线"随机器变（NFR-02）。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from docfidelity.ooxml import DocxPackage
from docfidelity.qa.snapshot import diff_snapshots, structure_snapshot

pytestmark = [pytest.mark.integration, pytest.mark.golden]

REPO_ROOT = Path(__file__).resolve().parents[2]
GOLDEN_ROOT = REPO_ROOT / "tests" / "golden"

#: 示例项目的三份档案。写死而不是从声明推：基线文件本身就是写死的，
#: 让用例的输入也硬编码，才能保证"新增了一份文档"不会悄悄少测一条。
DOC_KEYS: tuple[str, ...] = ("api-sectioned", "api-summary", "procedure-sop")


def _declared_doc_keys() -> list[str]:
    import examples.build_demo as demo

    return sorted(str(entry.doc_key) for entry in demo.project().enabled_documents())


def _build(root: Path, *extra: str) -> None:
    import examples.build_demo as demo

    assert demo.main(["--root", str(root), *extra]) == 0, f"示例构建失败：{root}"


def _artifact(root: Path, doc_key: str) -> Path:
    hits = sorted(root.rglob(f"demo-{doc_key}.docx"))
    assert len(hits) == 1, f"{doc_key} 的产物不止一份或一份都没有：{hits}"
    return hits[0]


@pytest.fixture(scope="module")
def built(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """跑一次全量示例构建。模块级复用：一次约 4 秒，三条用例各跑一遍没有意义。"""
    root = tmp_path_factory.mktemp("golden")
    _build(root)
    return root


@pytest.mark.parametrize("doc_key", DOC_KEYS)
def test_the_product_matches_its_baseline(built: Path, doc_key: str) -> None:
    """产物结构与基线一致。断言是**差分列表为空**而不是"两个 dict 相等"：
    失败时 pytest 会把逐条差异打出来，不必再去手工 diff 两份 JSON。"""
    baseline = json.loads((GOLDEN_ROOT / f"{doc_key}.json").read_text(encoding="utf-8"))
    with DocxPackage.open(_artifact(built, doc_key), read_only=True) as pkg:
        actual = structure_snapshot(pkg, doc_key=doc_key)
    assert diff_snapshots(baseline, actual) == []


def test_three_consecutive_builds_are_byte_identical(tmp_path: Path) -> None:
    """连续 3 次构建逐字节相同。

    只对一份文档做：幂等性检验的是引擎里有没有"用当前时刻"的写法，与文档内容无关，
    而三次全量构建要十几秒。判据落在**字节**上，因为结构快照看不到 zip 容器——
    ``DocxPackage.save`` 曾经用默认的条目时间戳（= 现在），那时结构一模一样、
    字节次次不同，"连续 3 次构建幂等"这句话是假的却测不出来。
    """
    digests: list[str] = []
    for index in range(3):
        root = tmp_path / f"run{index}"
        _build(root, "--only", "api-summary")
        digests.append(hashlib.sha256(_artifact(root, "api-summary").read_bytes()).hexdigest())
    assert len(set(digests)) == 1, f"三次构建的产物字节不同：{digests}"


def test_the_baseline_covers_every_declared_document() -> None:
    """基线集合必须与声明的 ``doc_key`` 集合**相等**。

    不等即失败（而不是"只查交集"）：新增一份文档却忘了跑 ``update_golden.py``，是
    这条判据最可能漏掉的情形，而它漏掉的正是新文档——最需要基线的那一个。
    """
    files = sorted(path.stem for path in GOLDEN_ROOT.glob("*.json"))
    assert files == _declared_doc_keys()


def test_the_demo_build_never_reaches_for_word(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """示例构建**不得探测或启动本机 Word**。

    这条守的是"基线与本机装没装 Office 无关"这个前提。它容易被一次顺手的重构破坏
    （比如把 ``office_enabled`` 从 payload 里去掉、回落到自动探测），而破坏后的症状
    是"在装了 Word 的机器上基线对不上"——本机上跑不出来。
    """
    from docfidelity.render import toc

    def unexpected(*_args: object, **_kwargs: object) -> None:
        pytest.fail("示例构建探测或启动了本机 Word：产物将不再可复现")

    monkeypatch.setattr(toc, "office_available", unexpected)
    monkeypatch.setattr(toc, "refresh_with_office", unexpected)
    _build(tmp_path, "--only", "api-summary")
    assert _artifact(tmp_path, "api-summary").is_file()


def test_repeated_cli_build_restores_the_persisted_event_sequence(tmp_path: Path) -> None:
    from docfidelity.pipeline import read_events

    _build(tmp_path, "--only", "api-summary")
    paths = list((tmp_path / "jobs").glob("*/events.jsonl"))
    assert len(paths) == 1
    before = read_events(paths[0])
    assert before
    _build(tmp_path, "--only", "api-summary")
    after = read_events(paths[0])
    assert after[: len(before)] == before
    assert len(after) > len(before)
    assert [event.event_id for event in after] == list(range(1, len(after) + 1))
