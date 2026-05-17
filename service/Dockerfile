FROM python:3.11-slim

# uv: single dependency manager across host dev and both container builds.
# Copying the static binary from the official image keeps the layer tiny.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

# Lockfile-driven install. Build context is the repo root (see
# docker-compose.yml) so pyproject.toml + uv.lock — the single source of
# truth — are reachable. --frozen fails if the lock is stale; --no-dev
# excludes the dev group, so the service image gets only its runtime deps.
# uv installs prebuilt wheels for scipy/statsmodels, so no C toolchain is
# needed (the old pip build dropped gcc/g++).
COPY pyproject.toml uv.lock /app/
RUN uv sync --frozen --no-dev

COPY service /app/service

# uv installs into /app/.venv; put it on PATH so `uvicorn` resolves.
ENV PATH="/app/.venv/bin:$PATH"

EXPOSE 8000
CMD ["uvicorn", "service.main:app", "--host", "0.0.0.0", "--port", "8000"]
