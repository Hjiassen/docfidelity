"""内核单测的公共夹具与架构断言（DD-2.3（3）/ 验收 32）。

本目录下的测试必须能在**无 HTTP、无数据库**的环境中全绿——这是「内核零 Web 依赖」
的可执行证明（决策四）。任何让内核沾上 Web 栈的改动都会在这里失败。
"""

from __future__ import annotations

import pkgutil
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

import docfidelity

#: 内核模块清单中不得出现的顶层模块
FORBIDDEN_KERNEL_DEPS = frozenset(
    {
        "fastapi",
        "starlette",
        "flask",
        "uvicorn",
        "sqlalchemy",
        "alembic",
        "sse_starlette",
        "pydantic_settings",
        "server",
    }
)


def _kernel_submodules() -> set[str]:
    return {module.name.split(".")[0] for module in pkgutil.iter_modules(docfidelity.__path__)}


@pytest.mark.unit
def test_kernel_has_no_web_deps() -> None:
    """内核子模块清单中不得出现任何 Web / DB 依赖。"""
    offenders = _kernel_submodules() & FORBIDDEN_KERNEL_DEPS
    assert not offenders, f"内核出现了 Web / DB 依赖：{sorted(offenders)}"


@pytest.mark.unit
def test_kernel_import_does_not_pull_web_stack() -> None:
    """导入全部内核模块后，进程内不得出现 Web / DB 模块。"""
    import importlib

    for module in pkgutil.iter_modules(docfidelity.__path__):
        importlib.import_module(f"docfidelity.{module.name}")

    leaked = FORBIDDEN_KERNEL_DEPS & set(sys.modules)
    assert not leaked, f"导入内核后进程内出现 Web / DB 模块：{sorted(leaked)}"


@pytest.fixture(scope="session")
def docx_factory(tmp_path_factory: pytest.TempPathFactory) -> Callable[..., Path]:
    """构造真实 docx 夹具的工厂（见 ``tests/fixtures/docx_builder.py``）。

    用真实文件而不是手写 XML：``ooxml`` 的全部风险都来自"跟 Word 的真实产物
    打交道"，玩具 XML 上测出来的绿到了真实模板上照样崩。
    """
    from tests.fixtures.docx_builder import build_sample_docx

    def _build(name: str, **kwargs: object) -> Path:
        target = tmp_path_factory.mktemp(f"ooxml-{name}") / f"{name}.docx"
        return build_sample_docx(target, **kwargs)

    return _build


@pytest.fixture(scope="session")
def sample_docx(docx_factory: Callable[..., Path]) -> Path:
    """默认样例模板：含表格样式、条件格式、目录、越界图片、编号。"""
    return docx_factory("sample")


@pytest.fixture(scope="session")
def template_docx(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """面向 ``registry`` 入库流水线的模板夹具。

    比 ``sample_docx`` 多出流水线真正要处理的结构：封面独占一节、修订记录表、
    页眉占位符、可识别的封面版本号。详见 ``tests/fixtures/docx_builder.py``。
    """
    from tests.fixtures.docx_builder import build_template_docx

    target = tmp_path_factory.mktemp("registry-template") / "template.docx"
    return build_template_docx(target)
