"""Create the initial ServiceHub schema for bookings, agents, tracking, and reliable jobs."""

from alembic import op

from servicehub import models
from servicehub.db import Base

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create the initial schema; later migrations must use explicit incremental operations."""
    Base.metadata.create_all(bind=op.get_bind())


def downgrade() -> None:
    """Remove the initial schema only through an explicit operator migration command."""
    Base.metadata.drop_all(bind=op.get_bind())

