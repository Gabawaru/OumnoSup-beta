# OumnoSup application image, shared by the api and scheduler services.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Build tooling for asyncpg's C extension, plus curl for the compose healthcheck.
# Removed in the same layer so it never reaches the final image.
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential curl \
 && rm -rf /var/lib/apt/lists/*

# Dependencies first: this layer is cached until requirements.txt itself changes,
# so ordinary code edits do not trigger a reinstall.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
 && apt-get purge -y --auto-remove build-essential

COPY alembic.ini ./
COPY alembic/ ./alembic/
COPY src/ ./src/
COPY data/ ./data/

# Run as a non-root user. A scraper reaches the network and parses untrusted
# input, so it should not be root if it is ever compromised.
RUN useradd --create-home --uid 1000 oumno \
 && chown -R oumno:oumno /app
USER oumno

EXPOSE 8000

CMD ["uvicorn", "src.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
