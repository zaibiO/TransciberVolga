"""Database connection and session management.

Uses SQLAlchemy with SQLite as a lightweight stand-in for PostgreSQL in this
demo. The database file lives under ``STORAGE_DIR`` so the ``web`` and
``worker`` services can share it through the mounted volume.
"""

import os

from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

# Directory that holds audio files, result JSON files and the SQLite DB.
# Overridable via environment variable (set to ``/app/storage`` inside Docker).
STORAGE_DIR = os.environ.get("STORAGE_DIR", "./storage")

# SQLite URL; the folder must exist before the engine opens the file.
os.makedirs(STORAGE_DIR, exist_ok=True)
DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    f"sqlite:///{os.path.join(STORAGE_DIR, 'transcriptions.db')}",
)

# ``check_same_thread=False`` is required because SQLAlchemy connections are
# used from FastAPI's threadpool *and* from Celery worker processes/threads,
# which a default SQLite connection would reject.
engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False},
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()


def get_db():
    """FastAPI dependency that yields a session and always closes it."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    """Create all tables if they do not already exist."""
    # Import models so they register against ``Base.metadata``.
    from app import models  # noqa: F401

    Base.metadata.create_all(bind=engine)
