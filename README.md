# Transcription Pipeline

Welcome! This is a small audio transcription service I put together as a AI Backend engineering assessment. You throw a WAV or MP3 at it, it turns the speech into text in the background, and then it hands you back a structured transcript with timestamps and confidence scores. I tried to make it something you'd actually enjoy running, not just a box-ticking exercise.

## What's under the hood

I kept the stack deliberately boring and dependable. The API is FastAPI running on Uvicorn, which gives us async request handling and a free interactive docs page at /docs. Background work goes through Celery with Redis as the broker, so an upload returns immediately while the heavy lifting happens out of band. Transcription itself uses faster-whisper, which wraps OpenAI's Whisper models in a much faster CTranslate2 runtime. For audio I rely on pydub and FFmpeg to normalize everything down to 16 kHz mono PCM WAV. Job status and metadata live in SQLite through SQLAlchemy, standing in for PostgreSQL in this demo so you never have to spin up a separate database server. And the whole thing is wrapped up in Docker Compose so it comes up with a single command.

## How it flows

When you post a file to /upload, the server checks the MIME type, enforces a 100 MB limit, normalizes the audio, and saves it under a fresh UUID in storage/audio. It hands back that task id straight away and passes the real work to a Celery worker.

The worker preprocesses the audio idempotently, just to be safe, then checks the duration. A clip of thirty seconds or less gets transcribed in one shot. Anything longer gets split into thirty second sliding windows with a five second overlap between neighbours, and those chunks are transcribed one after another, sequentially. I made that choice on purpose rather than reaching for a process pool, because Celery workers run as daemon processes and Python simply refuses to let a daemon spawn children. The overlap means a sentence that straddles a chunk boundary gets transcribed twice, and the merge step keeps whichever copy scored higher confidence. The final result is written to storage/results/{task_id}.json and the database row flips to COMPLETED.

## Running it with Docker

The quickest way in is docker compose up --build. That starts three services, a web service serving the API on http://localhost:8000, a worker service running the Celery consumer, and Redis sitting between them as the broker and result backend.

One small heads up. The first transcription will download the Whisper model, roughly 150 MB for the default base model, into the container. If you'd like that to survive rebuilds, mount a volume for the Hugging Face cache directory.

## Running it locally instead

If you'd rather skip Docker, you'll need Python 3.10 or newer, a Redis instance on localhost, and FFmpeg on your PATH. Create and activate a virtual environment, install the requirements, and open three terminals.

```bash
python -m venv .venv
# activate it: .venv\Scripts\activate on Windows, source .venv/bin/activate on macOS or Linux
pip install -r requirements.txt

# terminal 1, Redis
docker run -p 6379:6379 redis:7-alpine

# terminal 2, the API
uvicorn app.main:app --reload

# terminal 3, the worker
celery -A app.tasks worker --loglevel=info
```

When you run the worker locally, point it at your local Redis first by setting CELERY_BROKER_URL to redis://localhost:6379/0. That's `set CELERY_BROKER_URL=...` on Windows and `export CELERY_BROKER_URL=...` on macOS or Linux.

## Things you can tweak

Everything worth adjusting is an environment variable, and each one has a sensible default so you can happily ignore them. WHISPER_MODEL picks the Whisper size and defaults to base, a nice middle ground between speed and accuracy for a demo. CHUNK_DURATION and CHUNK_OVERLAP govern how longer files get split, defaulting to thirty seconds with a five second overlap. CELERY_BROKER_URL and CELERY_RESULT_BACKEND point at Redis and default to redis://redis:6379/0, which is exactly what Docker Compose expects. STORAGE_DIR is where the audio, the results, and the SQLite database all live, and it defaults to ./storage.

## The API

There are only three endpoints, and they're meant to feel obvious.

POST /upload takes a multipart form field named file and expects a WAV or MP3. It validates the MIME type, rejects anything over 100 MB, normalizes the audio, and returns the task id immediately.

```bash
curl -F "file=@sample.mp3" http://localhost:8000/upload
```

That comes back as something like {"task_id": "c5a1f8d0-..."}. Keep that id handy, you'll need it for the next two calls.

GET /status/{task_id} tells you how the job is going. The status field is one of PENDING, PROCESSING, COMPLETED, or FAILED, and progress runs from zero to a hundred.

```bash
curl http://localhost:8000/status/c5a1f8d0-...
```

GET /result/{task_id} returns the full transcript once the job finishes. You get the detected language, the overall duration and confidence, the complete text, and a list of segments each with its own start time, end time, and confidence.

```json
{
  "task_id": "c5a1f8d0-...",
  "status": "COMPLETED",
  "language": "en",
  "duration": 42.5,
  "confidence": 0.9214,
  "full_text": "The full transcript text ...",
  "segments": [
    { "start": 0.0, "end": 2.3, "text": "Hello world", "confidence": 0.98 }
  ]
}
```

## Where everything lives

The code is organised into a compact app package. main.py holds the FastAPI app and the three endpoints, tasks.py carries the Celery app and the transcription logic, and models.py with database.py define the SQLAlchemy model and the connection. utils.py has the helpers for normalizing audio, building chunks, and merging results. The storage folder keeps two subfolders, one for normalized uploads and one for JSON transcripts, with the SQLite file sitting right alongside them. Dockerfile, docker-compose.yml, and requirements.txt round out the repo.

## A few notes from the trenches

numpy is pinned to 1.26.4 because the 2.x line has had compatibility friction across the faster-whisper, onnxruntime, and ctranslate2 stack. Confidence comes from Whisper's per segment avg_logprob, which I map through exp so it lands in a friendly zero to one range where higher means better. And as I mentioned earlier, the chunk loop is sequential on purpose, since Celery's daemon workers can't spawn child processes, which is exactly the kind of constraint you only discover the first time you actually run it.