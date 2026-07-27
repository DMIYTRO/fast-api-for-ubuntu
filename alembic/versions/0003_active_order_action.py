"""Allow only one pending action for an order.

Revision ID: 0003_active_order_action
Revises: 0002_order_identity
"""

from __future__ import annotations

import json

from alembic import op
import sqlalchemy as sa


revision = "0003_active_order_action"
down_revision = "0002_order_identity"
branch_labels = None
depends_on = None


INDEX_NAME = "uq_order_actions_one_pending_per_order"


def upgrade() -> None:
    connection = op.get_bind()
    indexes = {
        item["name"]
        for item in sa.inspect(connection).get_indexes("order_actions")
    }
    if INDEX_NAME in indexes:
        return
    rows = connection.execute(
        sa.text(
            "SELECT id, order_result_id FROM order_actions "
            "WHERE status = 'pending' ORDER BY order_result_id, id DESC"
        )
    ).mappings()
    active_orders: set[int] = set()
    duplicate_ids: list[int] = []
    for row in rows:
        order_result_id = int(row["order_result_id"])
        if order_result_id in active_orders:
            duplicate_ids.append(int(row["id"]))
        else:
            active_orders.add(order_result_id)

    if duplicate_ids:
        action_table = sa.table(
            "order_actions",
            sa.column("id", sa.Integer),
            sa.column("status", sa.String),
            sa.column("cms_response_json", sa.Text),
        )
        connection.execute(
            action_table.update()
            .where(action_table.c.id.in_(duplicate_ids))
            .values(
                status="failed",
                cms_response_json=json.dumps(
                    {
                        "reason": "duplicate_pending_normalized",
                        "message": (
                            "Устаревшее дублирующее действие снято при обновлении базы."
                        ),
                    },
                    ensure_ascii=False,
                ),
            )
        )

    op.create_index(
        INDEX_NAME,
        "order_actions",
        ["order_result_id"],
        unique=True,
        sqlite_where=sa.text("status = 'pending'"),
        postgresql_where=sa.text("status = 'pending'"),
    )


def downgrade() -> None:
    indexes = {
        item["name"]
        for item in sa.inspect(op.get_bind()).get_indexes("order_actions")
    }
    if INDEX_NAME in indexes:
        op.drop_index(INDEX_NAME, table_name="order_actions")
