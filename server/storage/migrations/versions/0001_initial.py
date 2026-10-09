"""初始 schema：项目 / 配置版本 / 作业 / 阶段 / 产物 / 报告 / 上传件

Revision ID: 0001_initial
Revises:
Create Date: 2026-09-23

表结构与 ``server/storage/tables.py`` 必须一一对应。**这不是"两处真相"**：
迁移是历史（一经发布不可改），ORM 是当下；两者是否一致由
``tests/api/test_server_sqlalchemy.py`` 的「迁移结果 == ORM 元数据」用例盯住。

时刻列用 ``String(40)`` 存 ISO-8601（对应 ``tables.UtcDateTime``）——SQLite 不保存
``tzinfo``，用 ``DateTime(timezone=True)`` 会让读回来的是 naive datetime，而内存版给的是
aware，``expires_at <= now()`` 随即抛异常（详见 ``tables.UtcDateTime`` 的说明）。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: 时刻列的落库类型（与 ``tables.UtcDateTime.impl`` 同宽）。
_MOMENT = sa.String(40)


def upgrade() -> None:
    op.create_table(
        "projects",
        sa.Column("project_id", sa.String(64), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("created_at", _MOMENT, nullable=False),
        sa.Column("updated_at", _MOMENT, nullable=False),
        sa.Column("archived", sa.Boolean(), nullable=False),
        sa.Column("demo", sa.Boolean(), nullable=False),
    )
    op.create_index("ix_projects_name", "projects", ["name"])

    op.create_table(
        "config_versions",
        sa.Column("project_id", sa.String(64), primary_key=True),
        sa.Column("version", sa.Integer(), primary_key=True),
        sa.Column("author", sa.String(200), nullable=True),
        sa.Column("created_at", _MOMENT, nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
    )

    op.create_table(
        "jobs",
        sa.Column("job_id", sa.String(128), primary_key=True),
        sa.Column("project_id", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("created_at", _MOMENT, nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
    )
    op.create_index("ix_jobs_project_id", "jobs", ["project_id"])
    op.create_index("ix_jobs_status", "jobs", ["status"])
    op.create_index("ix_jobs_created_at", "jobs", ["created_at"])
    op.create_index("ix_jobs_seq", "jobs", ["seq"])

    op.create_table(
        "stages",
        sa.Column("job_id", sa.String(128), primary_key=True),
        sa.Column("ordinal", sa.Integer(), primary_key=True),
        sa.Column("payload", sa.Text(), nullable=False),
    )

    op.create_table(
        "artifacts",
        sa.Column("artifact_id", sa.String(128), primary_key=True),
        sa.Column("job_id", sa.String(128), nullable=False),
        sa.Column("created_at", _MOMENT, nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
    )
    op.create_index("ix_artifacts_job_id", "artifacts", ["job_id"])

    op.create_table(
        "reports",
        sa.Column("job_id", sa.String(128), primary_key=True),
        sa.Column("payload", sa.Text(), nullable=False),
    )

    op.create_table(
        "uploads",
        sa.Column("upload_id", sa.String(64), primary_key=True),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("purpose", sa.String(32), nullable=False),
        sa.Column("created_at", _MOMENT, nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
    )
    op.create_index("ix_uploads_purpose", "uploads", ["purpose"])
    op.create_index("ix_uploads_created_at", "uploads", ["created_at"])


def downgrade() -> None:
    # 逆序删：先删有外键引用方向的"子表"。本项目没有真正的外键约束（跨表一致性由
    # 应用层保证），故顺序只为可读性，但保持逆序才不会有"删了父表才发现子表还在"。
    op.drop_index("ix_uploads_created_at", table_name="uploads")
    op.drop_index("ix_uploads_purpose", table_name="uploads")
    op.drop_table("uploads")
    op.drop_table("reports")
    op.drop_index("ix_artifacts_job_id", table_name="artifacts")
    op.drop_table("artifacts")
    op.drop_table("stages")
    op.drop_index("ix_jobs_seq", table_name="jobs")
    op.drop_index("ix_jobs_created_at", table_name="jobs")
    op.drop_index("ix_jobs_status", table_name="jobs")
    op.drop_index("ix_jobs_project_id", table_name="jobs")
    op.drop_table("jobs")
    op.drop_table("config_versions")
    op.drop_index("ix_projects_name", table_name="projects")
    op.drop_table("projects")
