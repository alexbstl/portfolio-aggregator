FROM python:3.13-slim

WORKDIR /app

# curl is only here for the healthcheck; everything else is pure Python
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy app code. Note: register.py, connect.py, fetch.py are dev tools
# and don't need to be in the image, but they're harmless if included.
# backfill_history.py is included so it can be run via `docker exec`.
COPY db.py sync_once.py backfill_history.py ./
COPY app/ ./app/

# Data dir is a mounted volume; sqlite db lives here
RUN mkdir -p /data
ENV DATABASE_PATH=/data/portfolio.db

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD curl -fsS http://localhost:8000/health || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
