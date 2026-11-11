"""Create the settings and command_log tables.

Revision: 0001
Follows: nothing; this is the first migration.
Created: 2026-11-11
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Apply the change."""
    op.create_table(
        "settings",
        sa.Column("key", sa.String(), primary_key=True),
        sa.Column("value", sa.String(), nullable=False),
    )
    op.create_table(
        "command_log",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        # Naive UTC; see UtcDateTime in hottub/persistence/schema.py.
        sa.Column("issued_at", sa.DateTime(), nullable=False),
        sa.Column("actor", sa.String(), nullable=False),
        sa.Column("command", sa.String(), nullable=False),
        sa.Column("argument", sa.String(), nullable=False),
        sa.Column("succeeded", sa.Boolean(), nullable=False),
        sa.Column("result", sa.String(), nullable=False),
    )
    op.create_index("ix_command_log_issued_at", "command_log", ["issued_at"])


def downgrade() -> None:
    """Undo the change."""
    op.drop_index("ix_command_log_issued_at", table_name="command_log")
    op.drop_table("command_log")
    op.drop_table("settings")
