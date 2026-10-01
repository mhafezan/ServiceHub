"""Run ServiceHub migrations using the configured database and complete model metadata."""

from alembic import context
from servicehub import models
from servicehub.db import Base, engine

with engine.connect() as connection:
    context.configure(connection=connection, target_metadata=Base.metadata)
    with context.begin_transaction():
        context.run_migrations()

