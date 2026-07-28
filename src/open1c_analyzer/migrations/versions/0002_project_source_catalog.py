"""Add project source paths and the source-file catalog.

Revision ID: 0002_project_source_catalog
Revises: 0001_initial
Create Date: 2026-07-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_project_source_catalog"
down_revision: str | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("projects") as batch_op:
        batch_op.add_column(sa.Column("source_path", sa.String(length=2048), nullable=True))
        batch_op.add_column(sa.Column("last_scanned_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.create_unique_constraint("uq_projects_source_path", ["source_path"])

    op.create_table(
        "source_files",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("relative_path", sa.String(length=2048), nullable=False),
        sa.Column("checksum", sa.String(length=64), nullable=False),
        sa.Column("language", sa.String(length=32), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("modified_ns", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "project_id",
            "relative_path",
            name="uq_source_files_project_path",
        ),
    )
    op.create_index("ix_source_files_project_id", "source_files", ["project_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_source_files_project_id", table_name="source_files")
    op.drop_table("source_files")

    with op.batch_alter_table("projects") as batch_op:
        batch_op.drop_constraint("uq_projects_source_path", type_="unique")
        batch_op.drop_column("last_scanned_at")
        batch_op.drop_column("source_path")
