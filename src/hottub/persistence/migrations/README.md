# Database migrations

Each file in `versions/` is one change to the database schema. The
application applies any it has not yet run at every start-up
(`hottub.persistence.migrate`), so a deploy never needs a manual step.

## Changing the schema

1. Change the table definitions in `../schema.py`.
2. Add a file to `versions/`, numbered one higher than the last, whose
   `down_revision` is the previous file's `revision`. Copy an existing file
   for the shape. Write both `upgrade()` and `downgrade()`.
3. Run `uv run pytest tests/integration/test_database.py`. It builds a
   database from the migrations and fails if the result differs from
   `schema.py`.

Never edit a migration that has been deployed: the Pi's database has already
run it and will not run it again. Add a new one instead.

There is no `alembic.ini`, and the `alembic` command line is not used; the
configuration is built in `../database.py`.
