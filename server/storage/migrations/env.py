"""Alembic 环境（DD-13.1 的 ``server/storage`` 落库迁移；P0-14 第 2 轮）。

连接串从哪来
------------
**不写进 ``alembic.ini``**（那是把口令放进版本库）。两条合法来源，按序尝试：

1. 调用方用 ``server.storage.engine.migration_config(url)`` 注入的 ``sqlalchemy.url``
   ——服务端与测试走这条；
2. 环境变量 ``DOCFIDELITY_DATABASE_URL``——运维直接跑 ``alembic upgrade head`` 走这条。

两条都没有就**明确失败**，而不是回落到某个默认 SQLite 文件：静默地在一个"没人打算用"
的库上跑迁移，会把一次配置遗漏变成一次"迁移跑了但服务还是空的"。

仓库根为什么要自己塞进 ``sys.path``
-----------------------------------
迁移可能被任意工作目录下的进程调用（部署脚本、测试、CI）。要求"必须先 cd 到仓库根"
会让失败形态变成 ``ModuleNotFoundError: server``——一个与迁移本身无关的报错。
"""

from __future__ import annotations

import os
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

# ``<repo>/server/storage/migrations/env.py`` → parents[3] = 仓库根。
_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from server.storage.tables import Base  # noqa: E402  （路径必须先就位）

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

#: 与 :data:`server.settings.ENV_PREFIX` 同口径（``DOCFIDELITY_``）。
ENV_KEY = "DOCFIDELITY_DATABASE_URL"


def _database_url() -> str:
    """取连接串；两条来源都没有时报错（理由见模块文档）。"""
    configured = config.get_main_option("sqlalchemy.url")
    if configured:
        return configured
    from_env = os.environ.get(ENV_KEY)
    if not from_env:
        raise RuntimeError(
            f"未提供数据库连接串：设置 {ENV_KEY}，或用 "
            "server.storage.engine.migration_config(url) 构造迁移配置。"
        )
    return from_env


def run_migrations_offline() -> None:
    """离线模式（``--sql``）：只生成 SQL 文本，不连库。"""
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """在线模式：连库执行。"""
    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = _database_url()
    connectable = engine_from_config(section, prefix="sqlalchemy.", poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
