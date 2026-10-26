FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Yekaterinburg

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && useradd -r -u 10001 watcher \
    && mkdir -p /app/data && chown watcher /app/data

COPY app ./app
USER watcher

HEALTHCHECK --interval=60s --timeout=10s --start-period=60s --retries=3 \
    CMD python -m app.healthcheck

CMD ["python", "-m", "app.main"]
