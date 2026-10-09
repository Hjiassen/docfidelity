"""类型无关检索器（DD-10.6 / NFR-28 / 验收 42 / 附录 B）。

「引擎与文档类型无关」如果只是口号，它会在实现的第一周就被违反（需求 4.9 的原文）。
把口号变成工程属性的唯一办法是**可自动检索**：引擎代码里出现任何文档类型名或领域
概念名，CI 就失败。本模块就是那个检索器的**唯一实现**——``scripts/check_forbidden_words.py``
与门禁规则 ``FR-M5-17`` 都调它，两处各写一份必然漂移，而漂移的表现是
"CI 报命中、门禁说干净"（或反之），两边都有人信。

三类命中（与设计文档附录 B 的语义严格对齐）
------------------------------------------
===================  ==================================================================
``doc_type_name``    文档类型名。「引擎不认识任何文档类型」——故**非 docstring 的字符串
                     字面量**中出现即命中（错误文案里写死类型名同样违规）。
``domain_concept``   领域概念名。「**作为代码标识符或分支条件时**」——故只查标识符
                     （变量 / 参数 / 函数 / 类 / 属性名）与判别分支里的字面量，**不查**
                     普通字符串字面量（中文错误文案必然出现"字段"这类词）。
``branch_literal``   针对具体 ``kind`` 的类型分支。只查形如 ``x.kind == "..."`` /
                     ``x.doc_type == "..."`` 的比较；``x.unit == "table"`` 这类引擎自有
                     封闭枚举的判别**不属于**此列，否则连 ``UnitType`` 都判不了。
===================  ==================================================================

为什么要基于 AST 而不是文本
---------------------------
注释与 docstring 里几乎必然出现"文档类型"这类词（本模块的文档就是），文本检索会把
它们全判成违规，结果是**规则被关掉**——一条总在误报的规则等于不存在。

白名单
------
``docs/**``、``examples/**``、``demo-templates/**``、``tests/fixtures/**``，
以及 ``src/docfidelity/contracts/domain.py``（仅作**示例声明**的字段名）。
示例档案与前两者必然出现 ``endpoint`` 这类词，因为那是**数据**而不是引擎代码。
"""

from __future__ import annotations

import ast
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Final

from docfidelity.contracts.errors import ConfigurationError
from docfidelity.contracts.ir import ContentIR

__all__ = [
    "EXCLUDE_PARTS",
    "KIND_DISCRIMINATOR_ATTRS",
    "SCAN_ROOTS",
    "TYPE_AGNOSTIC_RULE_ID",
    "WHITELIST_DIRS",
    "WHITELIST_FILES",
    "WORDLIST_PATH",
    "Hit",
    "ir_words",
    "load_wordlist",
    "scan",
    "scan_ir",
    "scan_sources",
]

#: 门禁规则编号（附录 C：FR-M5-17 类型无关 | integrity | blocker）。
TYPE_AGNOSTIC_RULE_ID: Final = "FR-M5-17"

#: 词表的**唯一**来源。CI 脚本与门禁规则都读它，禁止在任何一处另写一份。
WORDLIST_PATH: Final = Path(__file__).resolve().parent / "rules" / "forbidden_words.txt"

#: 仓库根（``<repo>/src/docfidelity/qa/type_agnostic.py`` 上溯四级）。
REPO_ROOT: Final = Path(__file__).resolve().parents[3]

#: 默认扫描根。DD-10.6 列出的是 `{contracts,model,compose,render,qa,pipeline}` 六个模块，
#: 那是**最小**范围；这里默认扫**整个内核**——范围更宽只会更严，不会放过违规。
SCAN_ROOTS: Final[tuple[str, ...]] = ("src/docfidelity",)

WHITELIST_FILES: Final[tuple[str, ...]] = ("src/docfidelity/contracts/domain.py",)
WHITELIST_DIRS: Final[tuple[str, ...]] = (
    "docs/",
    "examples/",
    "demo-templates/",
    "tests/fixtures/",
)

EXCLUDE_PARTS: Final[frozenset[str]] = frozenset(
    {".git", "__pycache__", ".venv", ".mypy_cache", ".ruff_cache", "build", "dist"}
)

#: 判定「是否属于类型分支」的判别属性名。只有对这两个属性做字面量比较才算类型分支。
KIND_DISCRIMINATOR_ATTRS: Final[frozenset[str]] = frozenset({"kind", "doc_type"})

#: docstring 判定的节点类型。
_DOCSTRING_OWNERS: Final[tuple[type[ast.AST], ...]] = (
    ast.Module,
    ast.ClassDef,
    ast.FunctionDef,
    ast.AsyncFunctionDef,
)


@dataclass(frozen=True)
class Hit:
    """一次命中：位置 + 类别 + 命中的词 + 处置提示。"""

    path: str
    line: int
    category: str
    word: str
    detail: str

    def as_text(self) -> str:
        return f"{self.path}:{self.line}  [{self.category}] '{self.word}'  {self.detail}"


# ------------------------------------------------------------------ 词表
@lru_cache(maxsize=4)
def load_wordlist(path: str | Path = WORDLIST_PATH) -> dict[str, frozenset[str]]:
    """解析分组词表，返回 ``{分组名: {词, ...}}``。

    结果被缓存：门禁在同一次构建里会多次问同一份词表，且词表是**只读**数据。
    缓存键是路径字符串（``Path`` 不可哈希地包含 mtime，语义上也不需要）。
    """
    groups: dict[str, set[str]] = {}
    current: str | None = None
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            current = line[1:-1].strip()
            groups.setdefault(current, set())
            continue
        if current is not None:
            groups[current].add(line)
    return {name: frozenset(words) for name, words in groups.items()}


def _group(name: str) -> frozenset[str]:
    return load_wordlist().get(name, frozenset())


def ir_words() -> frozenset[str]:
    """IR 扫描用的词表：``doc_type_name | domain_concept``。

    **刻意不含** ``branch_literal``。那一组管的是"形如 ``x.kind == "..."`` 的类型分支"，
    只在扫源码 AST 时有意义（见模块文档的三类命中表）。把它并进来会出事，因为
    ``compose.purity`` 对 IR 的检查是**子串**匹配（IR 是数据，没有 AST 可查）：
    ``endpoint`` / ``table`` / ``field`` / ``parameter`` 这些词必然出现在**合法** IR 里
    ——``unit_id`` 由档案的 ``for_each`` 派生、``type`` 就是 ``UnitType`` 这个封闭枚举的
    取值——于是每一份正常构建都被判成 blocker。

    单独成函数而不是在 :func:`scan_ir` 里就地合并：``compose`` 阶段体要在装配期做
    同一项自检（fail-fast），两处必须用同一份词表，否则"CI 说脏、构建说干净"。
    """
    return _group("doc_type_name") | _group("domain_concept")


# ------------------------------------------------------------------ 源码扫描
def _is_whitelisted(rel_path: str) -> bool:
    return rel_path in WHITELIST_FILES or any(rel_path.startswith(item) for item in WHITELIST_DIRS)


def _iter_sources(roots: tuple[str, ...]) -> Iterator[Path]:
    for root in roots:
        base = Path(root)
        if not base.is_absolute():
            base = REPO_ROOT / root
        if not base.exists():
            continue
        for path in sorted(base.rglob("*.py")):
            if EXCLUDE_PARTS.isdisjoint(path.parts):
                yield path


def _relative(file: Path) -> str:
    try:
        return file.relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return file.as_posix()


def _docstring_ids(tree: ast.AST) -> set[int]:
    """收集 docstring 节点对象的 ``id``（这些字符串是说明文字，不参与检索）。"""
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, _DOCSTRING_OWNERS):
            body = getattr(node, "body", [])
            first = body[0] if body else None
            if (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
            ):
                ids.add(id(first.value))
    return ids


def _doc_type_hits(
    tree: ast.AST, docstrings: set[int], doc_types: frozenset[str]
) -> Iterator[tuple[int, str, str]]:
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        if id(node) in docstrings:
            continue
        for word in sorted(doc_types):
            if word in node.value:
                yield node.lineno, "doc_type_name", word


def _identifier_hits(tree: ast.AST, concepts: frozenset[str]) -> Iterator[tuple[int, str, str]]:
    for node in ast.walk(tree):
        name: str | None = None
        if isinstance(node, ast.Name):
            name = node.id
        elif isinstance(node, ast.Attribute):
            name = node.attr
        elif isinstance(node, ast.arg):
            name = node.arg
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            name = node.name
        if name and name in concepts:
            # 上面每个分支命中的节点都带 lineno；``getattr`` 只是为了不让类型检查
            # 因为基类 AST 没有该属性而失败。
            yield getattr(node, "lineno", 0), "domain_concept", name


def _iter_literals(node: ast.AST) -> list[ast.Constant]:
    """展开比较操作数里的字符串字面量（含 tuple / list / set 容器）。"""
    candidates: list[ast.AST] = (
        list(node.elts) if isinstance(node, ast.Tuple | ast.List | ast.Set) else [node]
    )
    return [
        item
        for item in candidates
        if isinstance(item, ast.Constant) and isinstance(item.value, str)
    ]


def _branch_hits(tree: ast.AST, branch_words: frozenset[str]) -> Iterator[tuple[int, str, str]]:
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        sides = [node.left, *node.comparators]
        discriminators: set[str] = set()
        for side in sides:
            if isinstance(side, ast.Attribute):
                discriminators.add(side.attr)
            elif isinstance(side, ast.Name):
                discriminators.add(side.id)
        if not (discriminators & KIND_DISCRIMINATOR_ATTRS):
            continue
        for side in sides:
            for literal in _iter_literals(side):
                if literal.value in branch_words:
                    yield literal.lineno, "branch_literal", literal.value


def scan_sources(roots: Iterable[str] = SCAN_ROOTS) -> list[Hit]:
    """扫描源码 AST，返回全部命中（``roots`` 为仓库相对路径）。

    **扫不到文件即报错，不返回空列表**：`roots` 拼错或包被装到了别处时，检索器会
    一个文件都没读到，而"命中 0"与"根本没扫"在返回值上完全一样——那是这条门禁
    最危险的失效方式（它会让 CI 一路绿灯）。
    """
    doc_types = _group("doc_type_name")
    concepts = _group("domain_concept")
    branch_words = _group("branch_literal")

    files = [file for file in _iter_sources(tuple(roots)) if not _is_whitelisted(_relative(file))]
    if not files:
        raise ConfigurationError(
            f"类型无关检索没有扫到任何源文件，拒绝给出'通过'结论：{list(roots)}",
            where={"roots": list(roots), "repo_root": str(REPO_ROOT)},
            remediation=["确认 SCAN_ROOTS 是仓库相对路径，或从仓库根运行本检索"],
        )

    hits: list[Hit] = []
    for file in files:
        rel = _relative(file)
        try:
            tree = ast.parse(file.read_text(encoding="utf-8"), filename=rel)
        except SyntaxError as exc:  # 让 CI 明确失败，而不是静默跳过
            hits.append(Hit(rel, exc.lineno or 0, "syntax_error", "", str(exc)))
            continue
        docstrings = _docstring_ids(tree)
        hits.extend(
            Hit(rel, line, category, word, "字符串字面量命中")
            for line, category, word in _doc_type_hits(tree, docstrings, doc_types)
        )
        hits.extend(
            Hit(rel, line, category, word, "标识符命中")
            for line, category, word in _identifier_hits(tree, concepts)
        )
        hits.extend(
            Hit(rel, line, category, word, "kind / doc_type 判别分支命中")
            for line, category, word in _branch_hits(tree, branch_words)
        )
    return hits


# ------------------------------------------------------------------ IR 扫描
def scan_ir(ir: ContentIR) -> list[Hit]:
    """扫描 IR 序列化输出（DD-10.6 的"IR 输出"一侧）。

    口径**完全**交给 ``compose.purity``：那里已经实现了 ``trace`` / ``origin`` 的剔除
    与「引擎表面 vs 事实取值」的区分。本函数只补上"再加一层词表"这件事——
    若这里另写一遍剔除逻辑，就会出现"CI 与门禁对同一份 IR 给出两个结论"。

    命中的 token 无法定位到行号（IR 是数据不是源码），故 ``line=0``、``path="<ir>"`。
    """
    from docfidelity.compose.purity import check_ir_purity

    words = ir_words()
    return [
        Hit("<ir>", 0, "ir_token", token, "IR 序列化输出命中（含引擎表面字段名与取值）")
        for token in check_ir_purity(ir, extra_words=sorted(words))
    ]


def scan(*, roots: Iterable[str] = SCAN_ROOTS, ir: ContentIR | None = None) -> list[Hit]:
    """源码 + IR 的合并检索（CI 与门禁的统一入口）。"""
    hits = scan_sources(roots)
    if ir is not None:
        hits.extend(scan_ir(ir))
    return hits
