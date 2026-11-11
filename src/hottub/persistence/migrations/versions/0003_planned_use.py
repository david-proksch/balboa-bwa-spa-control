"""Create the planned_use table.

The weekend keep-ready switch needs no table: it is a row in ``settings``.

Revision: 0003
Follows: 0002
Created: 2026-11-11
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Apply the change."""
    op.create_table(
        "planned_use",
        # The owner's wall clock, not UTC.
        sa.Column("day", sa.Date(), primary_key=True),
        sa.Column("use_time", sa.Time(), nullable=False),
        # Naive UTC; see UtcDateTime in hottub/persistence/schema.py.
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("heating_started_at", sa.DateTime(), nullable=True),
    )


def downgrade() -> None:
    """Undo the change."""
    op.drop_table("planned_use")
