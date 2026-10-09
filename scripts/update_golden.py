"""生成 / 校验 golden 结构快照（DD-17.1 / DD-17.2 / TASK-P0-19）。

它解决的是什么
--------------
"这次改动有没有动到产物的形态"需要一个能进仓库、能逐字比对的答案。判据落在**结构**
而不是字节：产物现在也逐字节可复现（``DocxPackage.save`` 钉死了 zip 条目时刻），
但字节摘要只会说"变了"——结构快照能说出"段落 172 → 168、表 14 → 13 条"。

快照由 ``qa.snapshot.structure_snapshot`` 产出（那是投影的唯一实现），基线落
``tests/golden/<doc_key>.json``，比对在 integration 层
（``tests/integration/test_golden_snapshot.py``）。漂移的唯一合法来源是**有意的**
产物形态改动——那时跑本脚本重生成，diff 进评审。

``--check`` 用同一份输入重跑构建再比一次，故它同时是"示例资产是否还能构建出同样的
形态"的守卫；改了引擎或模板而忘了重生成基线，这里当场红。

为什么要跑一次真构建
--------------------
快照必须从**真实产物**提取，而不是从 IR 或模板拼一份。拼出来的那份只会证明
"我以为产物是这样"，而本模块要回答的恰恰是"产物**实际**是这样吗"。

用法::

    PYTHONPATH="src;." .venv/Scripts/python.exe scripts/update_golden.py
    PYTHONPATH="src;." .venv/Scripts/python.exe scripts/update_golden.py --check
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_ROOT = REPO_ROOT / "tests" / "golden"

if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from docfidelity.ooxml import DocxPackage  # noqa: E402
from docfidelity.qa.snapshot import structure_snapshot  # noqa: E402


def build_root() -> Path:
    """构建落点。**在仓库内的 ``var/`` 下**（这是运行期目录，不入库）。"""
    return REPO_ROOT / "var" / "golden-build"


def build_into(root: Path) -> None:
    """跑一遍示例构建（三份档案），产物落 ``<root>/jobs/<job>/artifacts/``。"""
    import examples.build_demo as demo

    if root.exists():
        shutil.rmtree(root)
    code = demo.main(["--root", str(root)])
    if code != 0:
        raise SystemExit(f"示例构建失败（exit={code}），无法生成基线")


def doc_keys() -> list[str]:
    """示例项目里启用的 doc_key（按声明序）。

    取声明而不是扫目录：产物文件名是 ``output_filename()`` 拼出来的，扫目录会得到
    "有一份文档没被构建"与"文件名规则变了"两种情况长得一样的输入。
    """
    import examples.build_demo as demo

    return [str(entry.doc_key) for entry in demo.project().enabled_documents()]


def snapshots_of(root: Path) -> dict[str, dict[str, Any]]:
    """从 ``root`` 下的产物提取快照，键为 ``doc_key``。

    文件名与 ``doc_key`` 的对应关系是**断言**而不是猜测：对不上即报错。静默跳过一份
    会让"某份文档不再被构建"看起来像"基线里本来就没它"。
    """
    expected = {f"demo-{key}.docx": key for key in doc_keys()}
    found: dict[str, dict[str, Any]] = {}
    for path in sorted(root.rglob("artifacts/*.docx")):
        key = expected.get(path.name)
        if key is None:
            raise SystemExit(
                f"产物 {path.name} 对不上任何声明的 doc_key（期望：{sorted(expected)}）。"
                "命名规则变了就要同步本脚本，否则基线会静默漏掉文档。"
            )
        if key in found:
            raise SystemExit(f"{key} 有不止一份产物：{path}。构建根没清干净？")
        with DocxPackage.open(path, read_only=True) as pkg:
            found[key] = structure_snapshot(pkg, doc_key=key)
    missing = sorted(set(expected.values()) - set(found))
    if missing:
        raise SystemExit(f"这些文档没有产物：{missing}。构建没跑全？")
    return found


def render(snapshot: dict[str, Any]) -> str:
    """快照的落盘形式。``indent=2`` + 末尾换行：基线要能被人 diff 与评审。"""
    return json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成/校验 golden 结构快照")
    parser.add_argument(
        "--check",
        action="store_true",
        help="只比对不写盘；与仓库里的基线不一致则退出 1",
    )
    args = parser.parse_args(argv)

    root = build_root()
    build_into(root)
    snapshots = snapshots_of(root)

    if not args.check:
        OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
        for key, snapshot in sorted(snapshots.items()):
            (OUTPUT_ROOT / f"{key}.json").write_text(render(snapshot), encoding="utf-8")
        print(f"OK 已写入 {len(snapshots)} 份基线到 {OUTPUT_ROOT.relative_to(REPO_ROOT)}")
        return 0

    drift: list[str] = []
    for key, snapshot in sorted(snapshots.items()):
        target = OUTPUT_ROOT / f"{key}.json"
        if not target.is_file():
            drift.append(f"{key}：仓库里没有基线（{target.name}）")
            continue
        if target.read_text(encoding="utf-8") != render(snapshot):
            drift.append(f"{key}：产物与 {target.name} 不一致")
    if drift:
        for line in drift:
            print(f"DRIFT {line}", file=sys.stderr)
        print(
            "示例产物的结构已漂移。确认这是有意的改动后跑 "
            'PYTHONPATH="src;." .venv/Scripts/python.exe scripts/update_golden.py 重生成基线。',
            file=sys.stderr,
        )
        return 1
    print(f"OK golden 结构快照与产物一致（{len(snapshots)} 份）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
