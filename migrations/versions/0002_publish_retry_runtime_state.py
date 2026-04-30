"""Add persisted retry metadata for publish reliability."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0002_publish_retry_runtime_state"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "scheduled_stories",
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "scheduled_stories",
        sa.Column("retry_window_started_at", sa.DateTime(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("scheduled_stories", "retry_window_started_at")
    op.drop_column("scheduled_stories", "retry_count")
