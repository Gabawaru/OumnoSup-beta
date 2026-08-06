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

# PORT is honoured because managed hosts (Render, Railway, Fly, Heroku) assign it
# and expect the process to bind there; 8000 is the local default. Both the
# healthcheck and the command read it, so they can never disagree about the port.
ENV PORT=8000

HEALTHCHECK --interval=15s --timeout=5s --start-period=40s --retries=5 \
  CMD python -c "import os,sys,urllib.request; sys.exit(0 if urllib.request.urlopen(f\"http://localhost:{os.environ.get('PORT','8000')}/health\", timeout=4).status==200 else 1)"

# Shell form on purpose: exec form cannot expand $PORT. The leading `exec`
# replaces the shell with uvicorn so it becomes PID 1 and receives SIGTERM
# directly -- without it the container would be killed rather than shut down.
CMD exec uvicorn src.api.main:app --host 0.0.0.0 --port "${PORT:-8000}"
