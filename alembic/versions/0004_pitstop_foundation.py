"""Add versioned PDF and PitStop persistence.

Revision ID: 0004_pitstop_foundation
Revises: 0003_active_order_action
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0004_pitstop_foundation"
down_revision = "0003_active_order_action"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ``Database.create_schema()`` is used by tests and some early local
    # installations. Such a database may later be stamped at an older
    # revision; make the migration safe when the complete target schema is
    # already present.
    inspector = sa.inspect(op.get_bind())
    tables = set(inspector.get_table_names())
    order_columns = {
        item["name"] for item in inspector.get_columns("order_results")
    }
    if {
        "pdf_revisions",
        "pitstop_checks",
        "pitstop_issues",
    }.issubset(tables) and {
        "source_status",
        "pitstop_status",
        "workflow_status",
    }.issubset(order_columns):
        return

    op.add_column(
        "order_results",
        sa.Column(
            "source_status",
            sa.String(32),
            nullable=False,
            server_default="pending",
        ),
    )
    op.add_column(
        "order_results",
        sa.Column(
            "pitstop_status",
            sa.String(32),
            nullable=False,
            server_default="not_checked",
        ),
    )
    op.add_column(
        "order_results",
        sa.Column(
            "workflow_status",
            sa.String(32),
            nullable=False,
            server_default="active",
        ),
    )
    op.execute("UPDATE order_results SET source_status = status")
    op.create_index("ix_order_results_source_status", "order_results", ["source_status"])
    op.create_index("ix_order_results_pitstop_status", "order_results", ["pitstop_status"])
    op.create_index("ix_order_results_workflow_status", "order_results", ["workflow_status"])

    op.create_table(
        "pdf_revisions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "order_result_id",
            sa.Integer(),
            sa.ForeignKey("order_results.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("revision_number", sa.Integer(), nullable=False),
        sa.Column("pdf_path", sa.Text(), nullable=False),
        sa.Column("sha256", sa.String(64)),
        sa.Column("size_bytes", sa.BigInteger()),
        sa.Column("is_current", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_by_action", sa.String(64)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_pdf_revisions_order_result_id", "pdf_revisions", ["order_result_id"]
    )
    op.create_index(
        "uq_pdf_revisions_order_revision",
        "pdf_revisions",
        ["order_result_id", "revision_number"],
        unique=True,
    )
    op.create_index(
        "uq_pdf_revisions_one_current_per_order",
        "pdf_revisions",
        ["order_result_id"],
        unique=True,
        sqlite_where=sa.text("is_current = 1"),
        postgresql_where=sa.text("is_current"),
    )

    # Existing PDFs become revision 1. Their checksum is intentionally unknown;
    # the first PitStop run will calculate and persist it.
    op.execute(
        sa.text(
            "INSERT INTO pdf_revisions "
            "(order_result_id, revision_number, pdf_path, is_current, created_at) "
            "SELECT id, 1, pdf_path, :current, created_at FROM order_results "
            "WHERE pdf_path IS NOT NULL AND pdf_path <> ''"
        ).bindparams(current=True)
    )

    op.create_table(
        "pitstop_checks",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "pdf_revision_id",
            sa.Integer(),
            sa.ForeignKey("pdf_revisions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("execution_status", sa.String(32), nullable=False),
        sa.Column("verdict", sa.String(32), nullable=False),
        sa.Column("checked_at", sa.DateTime(timezone=True)),
        sa.Column("profile_key", sa.String(128)),
        sa.Column("profile_name", sa.String(256)),
        sa.Column("profile_version", sa.String(64)),
        sa.Column("page_count", sa.Integer()),
        sa.Column("errors_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("warnings_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("fixes_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("critical_failures_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("noncritical_failures_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("informations_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("report_json_path", sa.Text()),
        sa.Column("report_xml_path", sa.Text()),
        sa.Column("technical_error", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_pitstop_checks_pdf_revision_id", "pitstop_checks", ["pdf_revision_id"]
    )
    op.create_index("ix_pitstop_checks_execution_status", "pitstop_checks", ["execution_status"])
    op.create_index("ix_pitstop_checks_verdict", "pitstop_checks", ["verdict"])

    op.create_table(
        "pitstop_issues",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("issue_id", sa.String(64), nullable=False),
        sa.Column(
            "pitstop_check_id",
            sa.String(64),
            sa.ForeignKey("pitstop_checks.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("fingerprint", sa.String(128)),
        sa.Column("severity", sa.String(32), nullable=False),
        sa.Column("action_id", sa.String(128)),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("occurrences", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("locations_json", sa.Text(), nullable=False, server_default="[]"),
    )
    op.create_index(
        "ix_pitstop_issues_pitstop_check_id", "pitstop_issues", ["pitstop_check_id"]
    )
    op.create_index("ix_pitstop_issues_severity", "pitstop_issues", ["severity"])


def downgrade() -> None:
    op.drop_table("pitstop_issues")
    op.drop_table("pitstop_checks")
    op.drop_table("pdf_revisions")
    op.drop_index("ix_order_results_workflow_status", table_name="order_results")
    op.drop_index("ix_order_results_pitstop_status", table_name="order_results")
    op.drop_index("ix_order_results_source_status", table_name="order_results")
    op.drop_column("order_results", "workflow_status")
    op.drop_column("order_results", "pitstop_status")
    op.drop_column("order_results", "source_status")
