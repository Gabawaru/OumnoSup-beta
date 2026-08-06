# OumnoSup application image, shared by the api and scheduler services.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# No apt layer at all. Every pinned dependency publishes a manylinux wheel for
# CPython 3.11 -- asyncpg included -- so no compiler is needed, and the
# healthcheck below uses the Python already in the image rather than curl.
# Skipping apt keeps the image small, the build fast, and the CVE surface low.

# Dependencies first: this layer is cached until requirements.txt itself changes,
# so ordinary code edits do not trigger a reinstall.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

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

HEALTHCHECK --interval=15s --timeout=5s --start-period=40s --retries=5 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/health', timeout=4).status==200 else 1)"

CMD ["uvicorn", "src.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
