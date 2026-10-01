"""Run ServiceHub migrations using the configured database and complete model metadata."""

from alembic import context

import servicehub.database.tables  # noqa: F401  # Register table metadata before migrations run.
from servicehub.database.session import Base, engine

with engine.connect() as connection:
    context.configure(connection=connection, target_metadata=Base.metadata)
    with context.begin_transaction():
        context.run_migrations()
