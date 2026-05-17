FROM python:3.11-slim

# uv: same dependency manager as the service build (see service/Dockerfile).
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

# Build context is the repo root (see docker-compose.yml). --only-group ingest
# installs just that group (psycopg + pandas) from the shared uv.lock, omitting
# the project's own deps — so this image stays lean (no litellm/fastapi/etc.).
COPY pyproject.toml uv.lock /app/
RUN uv sync --frozen --only-group ingest

COPY ingest/ingest.py /app/

# uv installs into /app/.venv; put it on PATH so `python` resolves the deps.
ENV PATH="/app/.venv/bin:$PATH"

CMD ["python", "-u", "ingest.py"]
