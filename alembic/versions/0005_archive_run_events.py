"""Add replay-preserving cold storage for run events.

Revision ID: 0005_archive_run_events
Revises: 0004_pitstop_foundation
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0005_archive_run_events"
down_revision = "0004_pitstop_foundation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if "archived_run_events" in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        "archived_run_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("run_id", sa.String(32), nullable=False),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_archived_run_events_run_id", "archived_run_events", ["run_id"]
    )
    op.create_index(
        "ix_archived_run_events_created_at", "archived_run_events", ["created_at"]
    )
    op.create_index(
        "ix_archived_run_events_run_id_id",
        "archived_run_events",
        ["run_id", "id"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.scalar(sa.text("SELECT COUNT(*) FROM archived_run_events")):
        raise RuntimeError(
            "Cannot drop archived_run_events while it contains replayable history"
        )
    op.drop_table("archived_run_events")
