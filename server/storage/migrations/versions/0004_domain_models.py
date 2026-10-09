"""领域模型库：已登记领域模型声明的登记表

Revision ID: 0004_domain_models
Revises: 0003_profiles_sources
Create Date: 2026-09-29

表结构与 ``server/storage/tables.py`` 必须一一对应（同 ``0001_initial`` 的说明）。

**主键只有 ``kind``，没有 ``version``**——与 ``templates`` / ``doc_type_profiles``
刻意不同。理由写在 ``protocols.RegisteredDomainModel`` 的类文档里：引用领域模型的
地方（档案的 ``for_each``、追溯边的 ``<kind>:<key>``）写的是裸 ``kind``，
**任何调用点都表达不出"哪一版"**，所以库里就不该存在多版。

只建两个索引（``registered_at`` 排序、``deprecated`` 默认过滤）：本表没有
"按某个业务字段筛"的需求（``title`` 只用于显示），故不抽 ``title`` / ``fingerprint``
列——它们留在 ``payload`` 里，避免"列与载荷谁是真相"多出两个要维持一致的字段。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_domain_models"
down_revision: str | None = "0003_profiles_sources"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: 时刻列的落库类型（与 ``tables.UtcDateTime.impl`` 同宽）。
_MOMENT = sa.String(40)


def upgrade() -> None:
    op.create_table(
        "domain_models",
        sa.Column("kind", sa.String(128), primary_key=True),
        sa.Column("registered_at", _MOMENT, nullable=False),
        sa.Column("deprecated", sa.Boolean(), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
    )
    op.create_index("ix_domain_models_registered_at", "domain_models", ["registered_at"])
    op.create_index("ix_domain_models_deprecated", "domain_models", ["deprecated"])


def downgrade() -> None:
    op.drop_index("ix_domain_models_deprecated", table_name="domain_models")
    op.drop_index("ix_domain_models_registered_at", table_name="domain_models")
    op.drop_table("domain_models")
