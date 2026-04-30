"""Initial scheduled stories schema."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "scheduled_stories",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("photo_path", sa.Text(), nullable=False),
        sa.Column("media_path", sa.Text(), nullable=False),
        sa.Column("prepared_media_path", sa.Text(), nullable=True),
        sa.Column("caption", sa.Text(), nullable=True),
        sa.Column("scheduled_time", sa.Time(), nullable=False),
        sa.Column("scheduled_date", sa.Date(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column(
            "status",
            sa.Enum("pending", "processing", "sent", "failed", "cancelled", name="storyjobstatus", native_enum=False),
            nullable=False,
        ),
        sa.Column(
            "schedule_type",
            sa.Enum("once", "weekly", name="scheduletype", native_enum=False),
            nullable=False,
        ),
        sa.Column("days", sa.String(length=64), nullable=True),
        sa.Column("timezone", sa.String(length=64), nullable=False),
        sa.Column("last_sent_at", sa.DateTime(), nullable=True),
        sa.Column("last_sent_date", sa.Date(), nullable=True),
        sa.Column(
            "media_type",
            sa.Enum("photo", "video", name="mediatype", native_enum=False),
            nullable=False,
        ),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("next_run_at", sa.DateTime(), nullable=True),
        sa.Column("locked_at", sa.DateTime(), nullable=True),
        sa.Column("lock_token", sa.String(length=64), nullable=True),
    )
    op.create_index(
        "ix_scheduled_stories_status_next_run_at",
        "scheduled_stories",
        ["status", "next_run_at"],
        unique=False,
    )
    op.create_index(
        "ix_scheduled_stories_schedule_type_status",
        "scheduled_stories",
        ["schedule_type", "status"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_scheduled_stories_schedule_type_status", table_name="scheduled_stories")
    op.drop_index("ix_scheduled_stories_status_next_run_at", table_name="scheduled_stories")
    op.drop_table("scheduled_stories")
