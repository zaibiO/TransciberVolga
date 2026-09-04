"""FastAPI application exposing the transcription REST API."""

import json
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, File, HTTPException, UploadFile
from sqlalchemy.orm import Session

from app import utils
from app.database import STORAGE_DIR, get_db, init_db
from app.models import TranscriptionTask
from app.tasks import transcribe_audio

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
AUDIO_DIR = Path(STORAGE_DIR) / "audio"
RESULTS_DIR = Path(STORAGE_DIR) / "results"
MAX_UPLOAD_SIZE = 100 * 1024 * 1024  # 100 MB

# Accepted audio MIME types (WAV and MP3 variants).
ALLOWED_MIME_TYPES = {
    "audio/wav",
    "audio/x-wav",
    "audio/wave",
    "audio/mpeg",
    "audio/mp3",
    "audio/mpeg3",
    "audio/x-mpeg-3",
}


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Ensure tables and storage directories exist on startup."""
    init_db()
    AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    yield


app = FastAPI(title="Transcription Pipeline", version="1.0.0", lifespan=lifespan)


@app.get("/")
def root():
    return {"service": "Transcription Pipeline", "docs": "/docs"}


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/upload")
async def upload_audio(file: UploadFile = File(...), db: Session = Depends(get_db)):
    """Accept a WAV/MP3 upload, normalize it, and start transcription.

    Returns the task id immediately.
    """
    # 1. Validate the MIME type.
    content_type = (file.content_type or "").lower()
    if content_type not in ALLOWED_MIME_TYPES:
        raise HTTPException(
            status_code=415,
            detail=f"Unsupported file type '{content_type}'. Allowed: WAV or MP3.",
        )

    task_id = str(uuid.uuid4())
    AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    audio_path = AUDIO_DIR / f"{task_id}.wav"

    # 2. Stream to disk while enforcing the 100MB limit (avoids buffering the
    #    entire file in memory).
    size = 0
    try:
        with open(audio_path, "wb") as out:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_UPLOAD_SIZE:
                    out.close()
                    audio_path.unlink(missing_ok=True)
                    raise HTTPException(
                        status_code=413, detail="File exceeds the 100MB size limit"
                    )
                out.write(chunk)
    except HTTPException:
        raise
    except Exception as exc:
        audio_path.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail=f"Failed to save upload: {exc}")

    if size == 0:
        audio_path.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail="Uploaded file is empty")

    # 3. Normalize to 16kHz mono PCM16 WAV.
    try:
        duration = utils.normalize_audio(str(audio_path))
    except Exception as exc:
        audio_path.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail=f"Could not decode audio file: {exc}")

    # 4. Persist metadata and dispatch the Celery task.
    record = TranscriptionTask(
        id=task_id,
        filename=file.filename,
        status="PENDING",
        progress=0,
        duration=duration,
    )
    db.add(record)
    db.commit()

    transcribe_audio.delay(task_id=task_id, audio_path=str(audio_path))

    return {"task_id": task_id}


@app.get("/status/{task_id}")
def get_status(task_id: str, db: Session = Depends(get_db)):
    """Return the current status (and progress) of a transcription task."""
    task = db.query(TranscriptionTask).filter(TranscriptionTask.id == task_id).first()
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return task.to_status_dict()


@app.get("/result/{task_id}")
def get_result(task_id: str, db: Session = Depends(get_db)):
    """Return the full JSON transcript for a completed task."""
    task = db.query(TranscriptionTask).filter(TranscriptionTask.id == task_id).first()
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")

    if task.status in ("PENDING", "PROCESSING"):
        return {
            "status": task.status,
            "progress": task.progress,
            "message": "Transcription is not complete yet",
        }

    if task.status == "FAILED":
        raise HTTPException(status_code=500, detail=task.error_message or "Transcription failed")

    result_path = RESULTS_DIR / f"{task_id}.json"
    if not result_path.exists():
        raise HTTPException(status_code=404, detail="Result file not found")

    with open(result_path, "r", encoding="utf-8") as fh:
        return json.load(fh)
