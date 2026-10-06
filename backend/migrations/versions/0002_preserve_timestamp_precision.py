"""Preserve Unix timestamps and coordinates at MySQL double precision."""

import time

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


DOUBLE_COLUMNS = {
    "rides": {
        "scheduled_at": True,
        "bid_until": True,
        "choose_until": True,
        "pickup_until": True,
        "started_at": True,
        "ended_at": True,
        "created_at": False,
    },
    "commands": {"expires_at": False, "created_at": False},
    "jobs": {"due_at": False, "lease_until": False, "created_at": False},
    "events": {"created_at": False},
    "locations": {
        "latitude": False,
        "longitude": False,
        "accuracy": True,
        "sampled_at": False,
    },
    "ride_training_samples": {"completed_at": False, "coverage": False},
}


def _change_precision(new_type: sa.types.TypeEngine, old_type: sa.types.TypeEngine) -> None:
    """Change numeric precision on MySQL while fresh SQLite schemas use current metadata."""

    if op.get_bind().dialect.name != "mysql":
        return
    for table, columns in DOUBLE_COLUMNS.items():
        for column, nullable in columns.items():
            op.alter_column(
                table,
                column,
                existing_type=old_type,
                type_=new_type,
                existing_nullable=nullable,
            )


def upgrade() -> None:
    """Convert imprecise floats and release immediate jobs rounded into the future."""

    _change_precision(sa.Double(), sa.Float())
    jobs = sa.table(
        "jobs",
        sa.column("status", sa.String()),
        sa.column("due_at", sa.Double()),
        sa.column("created_at", sa.Double()),
        sa.column("lease_until", sa.Double()),
    )
    now = time.time()
    op.execute(
        jobs.update()
        .where(
            jobs.c.status == "pending",
            jobs.c.due_at == jobs.c.created_at,
            jobs.c.due_at > now,
        )
        .values(due_at=now, lease_until=0.0)
    )


def downgrade() -> None:
    """Restore the prior float types when explicitly rolling back the schema."""

    _change_precision(sa.Float(), sa.Double())
