"""Create server state and session tables.

Revision ID: 0001_server_foundation
Revises:
"""

from alembic import op
import sqlalchemy as sa


revision = "0001_server_foundation"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "sessions",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("password_fingerprint", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_sessions_token_hash", "sessions", ["token_hash"], unique=True)
    op.create_index(
        "ix_sessions_password_fingerprint",
        "sessions",
        ["password_fingerprint"],
    )
    op.create_index("ix_sessions_expires_at", "sessions", ["expires_at"])

    op.create_table(
        "check_runs",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("input_path", sa.Text(), nullable=False),
        sa.Column("direction", sa.String(32), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("stage", sa.String(64)),
        sa.Column("progress", sa.Float(), nullable=False),
        sa.Column("options_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("error", sa.Text()),
    )
    op.create_index("ix_check_runs_status", "check_runs", ["status"])

    op.create_table(
        "order_results",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "run_id",
            sa.String(32),
            sa.ForeignKey("check_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("order_id", sa.String(64), nullable=False),
        sa.Column("customer_id", sa.String(64)),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("passed", sa.Boolean(), nullable=False),
        sa.Column("errors_json", sa.Text(), nullable=False),
        sa.Column("warnings_json", sa.Text(), nullable=False),
        sa.Column("pdf_path", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_order_results_run_id", "order_results", ["run_id"])
    op.create_index("ix_order_results_order_id", "order_results", ["order_id"])
    op.create_index("ix_order_results_status", "order_results", ["status"])
    op.create_index(
        "ix_order_results_run_order",
        "order_results",
        ["run_id", "order_id"],
        unique=True,
    )

    op.create_table(
        "file_results",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "order_result_id",
            sa.Integer(),
            sa.ForeignKey("order_results.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("filename", sa.String(512), nullable=False),
        sa.Column("source_path", sa.Text(), nullable=False),
        sa.Column("side", sa.String(32)),
        sa.Column("format", sa.String(32)),
        sa.Column("colorspace", sa.String(32)),
        sa.Column("width_mm", sa.Float()),
        sa.Column("height_mm", sa.Float()),
        sa.Column("dpi_x", sa.Float()),
        sa.Column("dpi_y", sa.Float()),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("errors_json", sa.Text(), nullable=False),
        sa.Column("warnings_json", sa.Text(), nullable=False),
        sa.Column("preview_path", sa.Text()),
    )
    op.create_index(
        "ix_file_results_order_result_id", "file_results", ["order_result_id"]
    )
    op.create_index("ix_file_results_status", "file_results", ["status"])

    op.create_table(
        "correction_decisions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "file_result_id",
            sa.Integer(),
            sa.ForeignKey("file_results.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("decision", sa.String(32), nullable=False),
        sa.Column("original_parameters_json", sa.Text(), nullable=False),
        sa.Column("proposed_parameters_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_correction_decisions_file_result_id",
        "correction_decisions",
        ["file_result_id"],
    )

    op.create_table(
        "run_events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "run_id",
            sa.String(32),
            sa.ForeignKey("check_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_run_events_run_id", "run_events", ["run_id"])
    op.create_index("ix_run_events_event_type", "run_events", ["event_type"])
    op.create_index("ix_run_events_created_at", "run_events", ["created_at"])

    op.create_table(
        "order_actions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "order_result_id",
            sa.Integer(),
            sa.ForeignKey("order_results.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("action", sa.String(32), nullable=False),
        sa.Column("comment", sa.Text()),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("cms_response_json", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_order_actions_order_result_id", "order_actions", ["order_result_id"]
    )
    op.create_index("ix_order_actions_status", "order_actions", ["status"])


def downgrade() -> None:
    op.drop_table("order_actions")
    op.drop_table("run_events")
    op.drop_table("correction_decisions")
    op.drop_table("file_results")
    op.drop_table("order_results")
    op.drop_table("check_runs")
    op.drop_table("sessions")
