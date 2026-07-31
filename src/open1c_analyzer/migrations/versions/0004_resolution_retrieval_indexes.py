"""Add indexes for focused graph retrieval.

Revision ID: 0004_resolution_retrieval
Revises: 0003_analyzer_core
Create Date: 2026-07-31
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0004_resolution_retrieval"
down_revision: str | None = "0003_analyzer_core"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_calls_project_caller",
        "call_sites",
        ["project_id", "caller_symbol_id"],
        unique=False,
    )
    op.create_index(
        "ix_calls_project_resolved",
        "call_sites",
        ["project_id", "resolved_symbol_id"],
        unique=False,
    )
    op.create_index(
        "ix_calls_project_resolution",
        "call_sites",
        ["project_id", "resolution"],
        unique=False,
    )
    op.create_index(
        "ix_queries_project_symbol",
        "queries",
        ["project_id", "symbol_id"],
        unique=False,
    )
    op.create_index(
        "ix_dependencies_project_source_identity",
        "dependencies",
        ["project_id", "source_kind", "source_id"],
        unique=False,
    )
    op.create_index(
        "ix_dependencies_project_target_identity",
        "dependencies",
        ["project_id", "target_kind", "target_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_dependencies_project_target_identity", table_name="dependencies")
    op.drop_index("ix_dependencies_project_source_identity", table_name="dependencies")
    op.drop_index("ix_queries_project_symbol", table_name="queries")
    op.drop_index("ix_calls_project_resolution", table_name="call_sites")
    op.drop_index("ix_calls_project_resolved", table_name="call_sites")
    op.drop_index("ix_calls_project_caller", table_name="call_sites")
