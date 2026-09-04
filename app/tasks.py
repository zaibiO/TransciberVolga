"""Celery application and asynchronous transcription task.

Pipeline:
    1. Preprocess the uploaded audio to 16kHz mono PCM16 WAV (idempotent; the
       upload endpoint already normalizes, but this guarantees correctness).
    2. Transcribe directly if the audio is <= 30 seconds.
    3. Otherwise split it into 30-second sliding windows with 5 seconds of
       overlap and transcribe each chunk sequentially.
    4. Merge the overlapping chunks (keeping the higher-confidence segment) and
       persist the final JSON result.
"""

import json
import logging
import math
import os
import tempfile
import traceback
from pathlib import Path

from celery import Celery

from app import utils
from app.database import STORAGE_DIR, SessionLocal
from app.models import TranscriptionTask

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Celery application
# ---------------------------------------------------------------------------
# Named ``celery`` so ``celery -A app.tasks worker`` resolves the app directly.
celery = Celery(
    "transcription",
    broker=os.environ.get("CELERY_BROKER_URL", "redis://redis:6379/0"),
    backend=os.environ.get("CELERY_RESULT_BACKEND", "redis://redis:6379/0"),
)

celery.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    task_track_started=True,
)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "base")
CHUNK_DURATION = float(os.environ.get("CHUNK_DURATION", "30"))
CHUNK_OVERLAP = float(os.environ.get("CHUNK_OVERLAP", "5"))

AUDIO_DIR = Path(STORAGE_DIR) / "audio"
RESULTS_DIR = Path(STORAGE_DIR) / "results"

# ---------------------------------------------------------------------------
# Model loading (lazy)
# ---------------------------------------------------------------------------
# The WhisperModel is loaded lazily, once per Celery worker process, and reused
# across tasks. Because chunks are processed sequentially, no multiprocessing
# is required and the model is never shared across processes.
_MODEL = None


def _get_model():
    """Return the process-local WhisperModel, loading it on first use."""
    global _MODEL
    if _MODEL is None:
        from faster_whisper import WhisperModel

        logger.info("Loading Whisper model '%s' (cpu/int8)...", WHISPER_MODEL)
        _MODEL = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
    return _MODEL


def _segments_to_dicts(segments):
    """Convert faster-whisper segments into plain dicts (chunk-local time)."""
    return [
        {
            "start": round(float(segment.start), 3),
            "end": round(float(segment.end), 3),
            "text": segment.text.strip(),
            # ``avg_logprob`` is negative; exp() maps it to (0, 1] where a
            # higher value means higher confidence.
            "confidence": round(math.exp(segment.avg_logprob), 6),
        }
        for segment in segments
    ]


def _overall_confidence(segments):
    """Average segment confidence for the whole transcript."""
    if not segments:
        return 0.0
    return sum(s["confidence"] for s in segments) / len(segments)


@celery.task(bind=True, name="app.tasks.transcribe_audio")
def transcribe_audio(self, task_id, audio_path):
    """Transcribe audio, processing chunks sequentially (no multiprocessing).

    Celery workers run as daemon processes, which cannot spawn children, so we
    avoid multiprocessing.Pool entirely and transcribe chunks one at a time.
    """
    db = SessionLocal()
    try:
        task = db.query(TranscriptionTask).filter(TranscriptionTask.id == task_id).first()
        if task is None:
            return

        # 1. Preprocess to 16kHz mono PCM16 WAV (idempotent; returns duration).
        task.status = "PROCESSING"
        task.progress = 10
        db.commit()

        duration = utils.normalize_audio(audio_path)
        task.duration = duration
        db.commit()

        # 2. Load the Whisper model lazily (once per worker process).
        model = _get_model()

        # 3. Transcribe: direct (<= 30s) or sequential sliding-window chunking.
        if duration <= CHUNK_DURATION:
            segments, info = model.transcribe(audio_path, beam_size=5)
            merged_segments = _segments_to_dicts(segments)
            language = info.language
            task.progress = 80
            db.commit()
        else:
            chunks = utils.build_chunks(duration, CHUNK_DURATION, CHUNK_OVERLAP)
            total_chunks = len(chunks)
            chunk_results = []
            language = None

            from pydub import AudioSegment
            audio = AudioSegment.from_file(audio_path)

            for idx, window in enumerate(chunks):
                task.progress = 20 + int((idx / total_chunks) * 60)
                db.commit()

                start, end = window["start"], window["end"]
                chunk_audio = audio[int(start * 1000):int(end * 1000)]

                with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
                    temp_path = tmp.name
                try:
                    chunk_audio.export(temp_path, format="wav")
                    segments, info = model.transcribe(temp_path, beam_size=5)
                    chunk_segments = _segments_to_dicts(segments)  # chunk-local time
                    language = language or info.language
                    chunk_results.append({"chunk_index": idx, "segments": chunk_segments})
                finally:
                    os.remove(temp_path)

            merged_segments = utils.merge_segments(
                chunk_results, CHUNK_DURATION, CHUNK_OVERLAP
            )
            task.progress = 80
            db.commit()

        # 4. Build the final payload and persist it to disk.
        full_text = " ".join(seg["text"] for seg in merged_segments).strip()
        payload = {
            "task_id": task_id,
            "status": "COMPLETED",
            "language": language,
            "duration": round(duration, 3),
            "confidence": round(_overall_confidence(merged_segments), 6),
            "full_text": full_text,
            "segments": merged_segments,
        }

        RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        result_path = RESULTS_DIR / f"{task_id}.json"
        with open(result_path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)

        task.status = "COMPLETED"
        task.progress = 100
        task.language = language
        db.commit()

        logger.info("Task %s completed successfully", task_id)
        return {"status": "COMPLETED", "task_id": task_id}

    except Exception as exc:
        error_message = f"{exc.__class__.__name__}: {exc}"
        logger.error(
            "Task %s failed: %s\n%s", task_id, error_message, traceback.format_exc()
        )
        # Best-effort: mark the task FAILED (the row may not exist yet).
        try:
            task = db.query(TranscriptionTask).filter(TranscriptionTask.id == task_id).first()
            if task is not None:
                task.status = "FAILED"
                task.error_message = error_message
                db.commit()
        except Exception:
            db.rollback()
        # Re-raise so Celery records the failure for its own accounting/retry.
        raise
    finally:
        db.close()
