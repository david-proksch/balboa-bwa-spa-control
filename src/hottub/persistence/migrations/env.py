"""Alembic's entry point: tells it how to reach the database.

Alembic runs this file each time a migration command is executed. The
connection comes from :func:`hottub.persistence.database.migrate` (or from
the developer commands in ``README.md`` next to this file); this script
never opens one itself and never reads a configuration file.
"""

from __future__ import annotations

from alembic import context

from hottub.persistence.schema import metadata

connection = context.config.attributes.get("connection")
if connection is None:
    raise RuntimeError(
        "No database connection was supplied. Run migrations through "
        "hottub.persistence.migrate(), not the alembic command line."
    )

context.configure(
    connection=connection,
    target_metadata=metadata,
    # SQLite cannot alter most things in place; batch mode makes Alembic
    # rebuild the table instead, so ordinary migrations work.
    render_as_batch=True,
)
with context.begin_transaction():
    context.run_migrations()
