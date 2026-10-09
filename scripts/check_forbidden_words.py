#!/usr/bin/env python3
"""类型无关检索器（CI / 门禁的 CLI 入口）。

规则来源：NFR-28 / 验收 42 —— 引擎代码中不得出现文档类型名与领域概念名。

**本脚本只是入口，实现只有一处**：``docfidelity.qa.type_agnostic``。词表亦然
（``src/docfidelity/qa/rules/forbidden_words.txt``）。三者（CLI、门禁规则 FR-M5-17、
词表）各写一份的后果是"CI 报命中、门禁说干净"——两边都有人信。

用法::

    python scripts/check_forbidden_words.py
    python scripts/check_forbidden_words.py <仓库相对路径 ...>

退出码：0 通过；1 存在命中或检索不可用。
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _run(argv: list[str]) -> int:
    # 源码树未安装为包（``pip install -e .`` 未执行），故先把 ``src`` 放进搜索路径。
    src = str(REPO_ROOT / "src")
    if src not in sys.path:
        sys.path.insert(0, src)
    from docfidelity.qa.type_agnostic import SCAN_ROOTS, WORDLIST_PATH, scan_sources

    if not WORDLIST_PATH.is_file():
        print(f"FAIL 词表缺失：{WORDLIST_PATH}")
        return 1

    roots = tuple(argv[1:]) or SCAN_ROOTS
    try:
        hits = scan_sources(roots)
    except Exception as exc:  # 检索不可用必须失败，不得当成"没有命中"
        print(f"FAIL 类型无关检索无法执行：{exc}")
        return 1

    if hits:
        print(f"FAIL 类型无关检索命中 {len(hits)} 处（NFR-28 / 验收 42）：")
        for hit in sorted(hits, key=lambda item: (item.path, item.line)):
            print(f"  {hit.as_text()}")
        print("处置：把文档类型 / 领域概念移出引擎代码，改由外部档案（profile / domain）声明。")
        return 1

    print("OK 类型无关检索通过（禁用词命中 0）")
    return 0


if __name__ == "__main__":
    sys.exit(_run(sys.argv))
