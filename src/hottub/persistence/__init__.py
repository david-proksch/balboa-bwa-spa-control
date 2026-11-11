"""Persistence: the SQLite database behind the repositories.

Everything stored on disk goes through this package: the schema, its
migrations, and the classes that implement the repository interfaces
declared in ``hottub.domain``. It is the only package that imports
SQLAlchemy or Alembic.
"""

from hottub.persistence.database import migrate, open_database
from hottub.persistence.repositories import (
    SqlAlertRepository,
    SqlCommandLog,
    SqlPlanRepository,
    SqlRunIntervalRepository,
    SqlSettingsRepository,
    SqlTemperatureSampleRepository,
)

__all__ = [
    "SqlAlertRepository",
    "SqlCommandLog",
    "SqlPlanRepository",
    "SqlRunIntervalRepository",
    "SqlSettingsRepository",
    "SqlTemperatureSampleRepository",
    "migrate",
    "open_database",
]
