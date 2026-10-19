FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PORT=8080
WORKDIR /srv/backend
# ffmpeg: voice & video forensics and call-recording transcoding (app/media.py)
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*
COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY backend/ /srv/backend/
COPY demo/ /srv/demo/
RUN useradd -r -u 10001 app && mkdir -p /srv/data && chown -R app /srv   # /srv/data: volumes inherit this owner
USER app
# Web process (also runs the background worker unless WORKER_ENABLED=false).
# Dedicated workers:   docker run <image> python -m app.worker
# Schema migrations:   docker run <image> alembic upgrade head   (run once per release, before the app)
CMD exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT} --workers 1 --proxy-headers --timeout-keep-alive 75
