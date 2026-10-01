"""Provide bounded SQLAlchemy connections and explicit transaction scopes."""

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from servicehub.config import settings

class Base(DeclarativeBase):
    """Collect the ServiceHub relational schema for migrations."""

def make_engine(url: str):
    """Construct a portable test engine or a bounded MySQL production pool."""
    
    options = {"pool_pre_ping": True}
    if not url.startswith("sqlite"):
        options.update(pool_size=5, max_overflow=2, pool_recycle=1200, isolation_level="READ COMMITTED")
    return create_engine(url, **options)

engine = make_engine(settings().database_url)
Session = sessionmaker(engine, expire_on_commit=False)

