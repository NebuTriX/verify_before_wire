FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PORT=8080
WORKDIR /srv/backend
COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY backend/ /srv/backend/
COPY demo/ /srv/demo/
RUN useradd -r -u 10001 app && chown -R app /srv
USER app
# One worker: the stream handler owns the investigation it runs. Scale with Cloud Run instances.
CMD exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT} --workers 1 --proxy-headers --timeout-keep-alive 75
