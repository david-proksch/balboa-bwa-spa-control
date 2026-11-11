"""Create the alert table.

What the watchdog remembers besides its alerts (the last fault-log entry
it has seen) needs no table: it is a row in ``settings``.

Revision: 0004
Follows: 0003
Created: 2026-11-11
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Apply the change."""
    op.create_table(
        "alert",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("severity", sa.String(), nullable=False),
        sa.Column("message", sa.String(), nullable=False),
        # Naive UTC; see UtcDateTime in hottub/persistence/schema.py.
        sa.Column("raised_at", sa.DateTime(), nullable=False),
        sa.Column("cleared_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_alert_raised_at", "alert", ["raised_at"])


def downgrade() -> None:
    """Undo the change."""
    op.drop_index("ix_alert_raised_at", table_name="alert")
    op.drop_table("alert")
