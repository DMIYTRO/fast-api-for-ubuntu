"""Store the PitStop comparison with the original PDF after a Callas fix.

All columns are nullable: checks and issues stored before this revision (and
any check that was not compared) keep NULL and load exactly as before.

Revision ID: 0006_pitstop_fix_baseline
Revises: 0005_archive_run_events
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0006_pitstop_fix_baseline"
down_revision = "0005_archive_run_events"
branch_labels = None
depends_on = None


_CHECK_COLUMNS = (
    ("verdict_raw", sa.String(32)),
    ("baseline_status", sa.String(32)),
    ("counts_raw_json", sa.Text()),
)
_ISSUE_COLUMNS = (
    ("baseline_occurrences", sa.Integer()),
    ("introduced_by_fix", sa.Integer()),
    ("hidden_by_baseline", sa.Boolean()),
    ("original_severity", sa.String(32)),
)


def _existing_columns(table: str) -> set[str]:
    return {item["name"] for item in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    # ``Database.create_schema()`` databases may already have the columns.
    for table, columns in (
        ("pitstop_checks", _CHECK_COLUMNS),
        ("pitstop_issues", _ISSUE_COLUMNS),
    ):
        existing = _existing_columns(table)
        for name, kind in columns:
            if name not in existing:
                # Nullable ADD COLUMN: no table rebuild, old rows stay NULL.
                op.add_column(table, sa.Column(name, kind, nullable=True))


def downgrade() -> None:
    for table, columns in (
        ("pitstop_issues", _ISSUE_COLUMNS),
        ("pitstop_checks", _CHECK_COLUMNS),
    ):
        existing = _existing_columns(table)
        present = [name for name, _kind in columns if name in existing]
        if not present:
            continue
        with op.batch_alter_table(table) as batch:
            for name in present:
                batch.drop_column(name)
