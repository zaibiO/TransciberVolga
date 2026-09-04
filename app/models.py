"""SQLAlchemy ORM models for the transcription pipeline."""

from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, Float, Integer, String

from app.database import Base


def _utcnow():
    """Return the current UTC datetime (timezone-aware)."""
    return datetime.now(timezone.utc)


class TranscriptionTask(Base):
    """Metadata and status for a single transcription job."""

    __tablename__ = "transcription_tasks"

    # The UUID doubles as the primary key, the public ``task_id`` and the
    # Celery task id.
    id = Column(String(36), primary_key=True, index=True)

    filename = Column(String(255), nullable=True)

    # One of: PENDING, PROCESSING, COMPLETED, FAILED.
    status = Column(String(20), nullable=False, default="PENDING")
    progress = Column(Integer, nullable=False, default=0)  # 0..100

    language = Column(String(20), nullable=True)
    duration = Column(Float, nullable=True)  # audio duration in seconds
    error_message = Column(String(2000), nullable=True)

    created_at = Column(DateTime, nullable=False, default=_utcnow)
    updated_at = Column(DateTime, nullable=False, default=_utcnow, onupdate=_utcnow)

    def to_status_dict(self):
        """Serialize the fields exposed by the ``/status`` endpoint."""
        return {"status": self.status, "progress": self.progress}
