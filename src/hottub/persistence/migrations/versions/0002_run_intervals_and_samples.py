"""Create the run_interval and temperature_sample tables.

Revision: 0002
Follows: 0001
Created: 2026-11-11
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Apply the change."""
    op.create_table(
        "run_interval",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("device", sa.String(), nullable=False),
        sa.Column("speed", sa.String(), nullable=True),
        # Naive UTC; see UtcDateTime in hottub/persistence/schema.py.
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("ended_at", sa.DateTime(), nullable=False),
        sa.Column("is_open", sa.Boolean(), nullable=False),
    )
    op.create_index("ix_run_interval_ended_at", "run_interval", ["ended_at"])
    op.create_table(
        "temperature_sample",
        sa.Column("at", sa.DateTime(), primary_key=True),
        sa.Column("water_f", sa.Float(), nullable=True),
        sa.Column("set_f", sa.Float(), nullable=False),
    )


def downgrade() -> None:
    """Undo the change."""
    op.drop_table("temperature_sample")
    op.drop_index("ix_run_interval_ended_at", table_name="run_interval")
    op.drop_table("run_interval")
