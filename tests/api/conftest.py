"""``server`` 接口测试的公共夹具（模板 / 档案 / 来源三个文件共用）。

为什么有个 ``conftest``
----------------------
三个文件都要"一份真 docx 夹具"与"同一套断言在两种仓储下各跑一遍"。各写一份的
代价不只是重复：``engine`` / ``repo`` 一旦有一处忘了参数化，那一处就会**只**在内存
仓储下变绿，而"协议不变、上层一行未改"这句结论正是靠参数化在验证的。

``storage_root`` 必须指向 ``tmp_path``（而不是仓库里的 ``var/``）
----------------------------------------------------------------
``create_app`` 的 ``root`` 只影响运行器工作区，模板库 / 档案库 / 暂存目录用的是
**settings** 的根。不给的话一次用例会把包写进工作树，并让下一次运行撞上
"版本已存在"——那种失败与代码无关，却每次都要重新查一遍。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import Engine

from server.storage import (
    MemoryRepository,
    Repository,
    SqlAlchemyRepository,
    create_engine_for,
    run_migrations,
)


@pytest.fixture(scope="session")
def template_docx(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """夹具模板（session 级：构造一次供全部用例读字节）。"""
    from tests.fixtures.docx_builder import build_template_docx

    target = tmp_path_factory.mktemp("server-template") / "template.docx"
    return build_template_docx(target)


@pytest.fixture(scope="session")
def template_bytes(template_docx: Path) -> bytes:
    return template_docx.read_bytes()


@pytest.fixture
def engine(tmp_path: Path) -> Engine:
    """真跑一遍 Alembic 迁移再开引擎：表结构由迁移决定，不由 ``create_all`` 决定。"""
    url = f"sqlite:///{(tmp_path / 'df.db').as_posix()}"
    run_migrations(url)
    return create_engine_for(url)


@pytest.fixture(params=["memory", "sqlalchemy"])
def repo(request: pytest.FixtureRequest, engine: Engine) -> Repository:
    """两种仓储实现下的同一条断言——协议不变、上层一行未改，等价才是被验证的事实。"""
    if request.param == "memory":
        return MemoryRepository()
    return SqlAlchemyRepository(engine)
