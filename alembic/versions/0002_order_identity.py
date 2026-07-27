"""Use customer and order number as the business identity.

Revision ID: 0002_order_identity
Revises: 0001_server_foundation
"""

from alembic import op
from sqlalchemy import inspect


revision = "0002_order_identity"
down_revision = "0001_server_foundation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    indexes = {
        item["name"] for item in inspect(op.get_bind()).get_indexes("order_results")
    }
    if "ix_order_results_run_order" in indexes:
        op.drop_index("ix_order_results_run_order", table_name="order_results")
    if "ix_order_results_run_customer_order" not in indexes:
        op.create_index(
            "ix_order_results_run_customer_order",
            "order_results",
            ["run_id", "customer_id", "order_id"],
            unique=True,
        )


def downgrade() -> None:
    indexes = {
        item["name"] for item in inspect(op.get_bind()).get_indexes("order_results")
    }
    if "ix_order_results_run_customer_order" in indexes:
        op.drop_index(
            "ix_order_results_run_customer_order", table_name="order_results"
        )
    if "ix_order_results_run_order" not in indexes:
        op.create_index(
            "ix_order_results_run_order",
            "order_results",
            ["run_id", "order_id"],
            unique=True,
        )
