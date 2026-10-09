"""档案库与事实源：已登记档案版本的登记表 + 每项目的事实源声明表

Revision ID: 0003_profiles_sources
Revises: 0002_templates
Create Date: 2026-09-24

表结构与 ``server/storage/tables.py`` 必须一一对应（同 ``0001_initial`` 的说明）。

**两张表，两种性质**——放在同一个迁移里是因为它们是同一次"资产管理"螺旋的两半，
不是为了省一个 revision：

* ``doc_type_profiles`` 与 ``templates`` 同形：带版本的**资产**，本体在磁盘目录里
  （档案要能整包导出给第三方，FR-M2-25），库里存登记元数据 + 指纹摘要。
* ``fact_sources`` 是**配置**，没有磁盘本体：一行就是一条外部数据源的声明，
  连同"上次探测的结果"这份留痕。``last_probed_at`` 抽成列是因为它可排序、可筛
  （"哪些来源从没探过"），而 ``item_count`` / ``options`` / ``bindings`` 留在
  ``payload`` 里——它们不承担筛选职责。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_profiles_sources"
down_revision: str | None = "0002_templates"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: 时刻列的落库类型（与 ``tables.UtcDateTime.impl`` 同宽）。
_MOMENT = sa.String(40)


def upgrade() -> None:
    op.create_table(
        "doc_type_profiles",
        sa.Column("profile_id", sa.String(128), primary_key=True),
        sa.Column("version", sa.String(64), primary_key=True),
        sa.Column("doc_type", sa.String(128), nullable=False),
        sa.Column("registered_at", _MOMENT, nullable=False),
        sa.Column("deprecated", sa.Boolean(), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
    )
    op.create_index("ix_doc_type_profiles_doc_type", "doc_type_profiles", ["doc_type"])
    op.create_index("ix_doc_type_profiles_registered_at", "doc_type_profiles", ["registered_at"])
    op.create_index("ix_doc_type_profiles_deprecated", "doc_type_profiles", ["deprecated"])

    op.create_table(
        "fact_sources",
        sa.Column("project_id", sa.String(64), primary_key=True),
        sa.Column("source_id", sa.String(128), primary_key=True),
        sa.Column("connector", sa.String(32), nullable=False),
        sa.Column("created_at", _MOMENT, nullable=False),
        sa.Column("last_probed_at", _MOMENT, nullable=True),
        sa.Column("payload", sa.Text(), nullable=False),
    )
    op.create_index("ix_fact_sources_connector", "fact_sources", ["connector"])
    op.create_index("ix_fact_sources_created_at", "fact_sources", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_fact_sources_created_at", table_name="fact_sources")
    op.drop_index("ix_fact_sources_connector", table_name="fact_sources")
    op.drop_table("fact_sources")

    op.drop_index("ix_doc_type_profiles_deprecated", table_name="doc_type_profiles")
    op.drop_index("ix_doc_type_profiles_registered_at", table_name="doc_type_profiles")
    op.drop_index("ix_doc_type_profiles_doc_type", table_name="doc_type_profiles")
    op.drop_table("doc_type_profiles")
