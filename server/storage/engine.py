"""引擎与会话装配（P0-14 第 2 轮；DD-13.1 的 ``server/storage``）。

三条纪律
--------
1. **SQLite 内存库必须锁住一条连接**。``sqlite://`` / ``sqlite:///:memory:`` 默认
   "一个连接一个库"，于是"建表用连接 A、写作业用连接 B"会得到 ``no such table``
   ——一个看起来像"迁移没跑"而实际是"连了另一个库"的错误。``StaticPool`` 把连接
   固定下来，这不是性能优化，是让"内存库"这个词成立。
2. **``check_same_thread=False``**。worker 是后台线程，仓储会被多线程调用，SQLite 的
   连接级线程检查会直接抛异常。真正保证并发安全的是事务与项目锁租约（DD-11.5），
   不是这个开关——所以打开它并不会把"没设计并发"变成"有并发"。
3. **建表有两条路径，其中一条只给测试**。生产走 Alembic（``run_migrations``）；
   ``create_schema`` 走 ``Base.metadata``，只服务测试与"内存库跑一下"的演示。
   两条路径是否一致由 ``tests/api/test_server_sqlalchemy.py`` 的
   「迁移结果 == ORM 元数据」用例盯住——那是"两处 schema"唯一的防漂移手段。

会话工厂用 ``expire_on_commit=False``：仓储在事务提交后仍要读回已写入的对象
（``add_project`` 要返回它），默认的 ``expire_on_commit=True`` 会让提交后的属性访问
再发一次 SELECT——在事务已结束的连接上表现为偶发的 ``DetachedInstanceError``。
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

from alembic import command
from alembic.config import Config as AlembicConfig
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from .tables import Base

__all__ = [
    "ALEMBIC_INI_NAME",
    "MEMORY_SCHEME",
    "create_engine_for",
    "create_schema",
    "is_memory_url",
    "migration_config",
    "run_migrations",
    "session_factory_for",
]

#: 内存库的连接串前缀（``sqlite://`` 与 ``sqlite+pysqlite:///:memory:`` 都算）。
MEMORY_SCHEME: Final = ":memory:"

ALEMBIC_INI_NAME: Final = "alembic.ini"

#: 本文件相对仓库根的位置：``<root>/server/storage/engine.py``。
_REPO_ROOT_DEPTH: Final = 2


def is_memory_url(url: str) -> bool:
    """连接串是否指向内存库。

    判据按 **scheme 之后的部分**写，不按字符串后缀写：``sqlite://`` 与 ``sqlite:///``
    都是内存库（库里没有文件路径），而 ``sqlite:///:memory:`` 也是。用后缀相等去判会
    踩到 ``rstrip("/")`` 把两个斜杠一起啃掉（``"sqlite://"`` → ``"sqlite:"``），
    于是"内存库"这个词在最经典的写法上反而不成立。
    """
    scheme, separator, rest = url.partition("://")
    if not separator or not scheme.startswith("sqlite"):
        return False
    return not rest.strip("/") or rest.endswith(MEMORY_SCHEME)


def create_engine_for(url: str, *, echo: bool = False) -> Engine:
    """建引擎。内存 SQLite 自动套用 ``StaticPool`` / ``check_same_thread=False``。"""
    if is_memory_url(url):
        return create_engine(
            url,
            echo=echo,
            future=True,
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
    return create_engine(url, echo=echo, future=True)


def session_factory_for(engine: Engine) -> sessionmaker[Session]:
    """会话工厂（``expire_on_commit=False``，理由见模块文档）。"""
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


def create_schema(engine: Engine) -> None:
    """按 ORM 元数据建表。**只给测试与内存演示**；生产用 :func:`run_migrations`。"""
    Base.metadata.create_all(engine)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[_REPO_ROOT_DEPTH]


def migration_config(url: str) -> AlembicConfig:
    """构造指向本仓库迁移目录的 Alembic 配置，并把连接串钉成 ``url``。

    为什么用代码构造而不是让调用方先 ``cd``：迁移目录是**本仓库的一部分**
    （``server/storage/migrations``），用相对路径找 ``alembic.ini`` 会让"从哪个目录
    启动"变成一个隐藏前置条件，而在服务里那不是使用者能控制的事。
    """
    root = _repo_root()
    config = AlembicConfig(str(root / ALEMBIC_INI_NAME))
    config.set_main_option("script_location", str(root / "server" / "storage" / "migrations"))
    config.set_main_option("sqlalchemy.url", url)
    return config


def run_migrations(url: str, *, revision: str = "head") -> None:
    """把库升到 ``revision``（默认 head）。部署与测试共用同一条路径。"""
    command.upgrade(migration_config(url), revision)
