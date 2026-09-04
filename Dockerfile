# ---------------------------------------------------------------------------
# Transcription Pipeline image
# ---------------------------------------------------------------------------
FROM python:3.10-slim

# Don't write bytecode and don't buffer stdout (helps with Docker logs).
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# FFmpeg is required by pydub for audio decoding/encoding.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies first to leverage Docker layer caching.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the application code.
COPY app ./app

# Ensure storage directories exist (also provided by a volume in compose).
RUN mkdir -p storage/audio storage/results

EXPOSE 8000

# The ``web`` service runs this; the ``worker`` service overrides the command.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
