FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    STORAGE_DIR=/data/storage APP_ENV=production PORT=8000

RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*
ENV FFMPEG_BINARY=/usr/bin/ffmpeg

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
# Runs as root so platform-mounted volumes (often root-owned) are writable without extra setup.
RUN mkdir -p /data/storage

EXPOSE 8000
# ROLE=web (default) or ROLE=worker
CMD ["sh", "-c", "if [ \"$ROLE\" = worker ]; then exec python worker.py; else exec gunicorn -w ${WEB_CONCURRENCY:-2} -k gthread --threads 4 -t 600 -b 0.0.0.0:${PORT} wsgi:app; fi"]
