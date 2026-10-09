"""模板库：已登记模板包版本的登记表

Revision ID: 0002_templates
Revises: 0001_initial
Create Date: 2026-09-24

表结构与 ``server/storage/tables.py`` 必须一一对应（同 ``0001_initial`` 的说明）。

**只加一张表**：模板包本体（清单 / 样式 / 骨架 / 块索引 / 覆盖）留在磁盘上的模板包
目录里，库里存登记元数据。把契约 JSON 也影射进库等于维护第二份 schema，而它必然与
包目录里的那份漂移——而"生成期只查契约"这条决策要的正是**一份**真相。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_templates"
down_revision: str | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: 时刻列的落库类型（与 ``tables.UtcDateTime.impl`` 同宽）。
_MOMENT = sa.String(40)


def upgrade() -> None:
    op.create_table(
        "templates",
        sa.Column("template_id", sa.String(128), primary_key=True),
        sa.Column("version", sa.String(64), primary_key=True),
        sa.Column("doc_type", sa.String(128), nullable=True),
        sa.Column("registered_at", _MOMENT, nullable=False),
        sa.Column("deprecated", sa.Boolean(), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
    )
    op.create_index("ix_templates_doc_type", "templates", ["doc_type"])
    op.create_index("ix_templates_registered_at", "templates", ["registered_at"])
    op.create_index("ix_templates_deprecated", "templates", ["deprecated"])


def downgrade() -> None:
    op.drop_index("ix_templates_deprecated", table_name="templates")
    op.drop_index("ix_templates_registered_at", table_name="templates")
    op.drop_index("ix_templates_doc_type", table_name="templates")
    op.drop_table("templates")
