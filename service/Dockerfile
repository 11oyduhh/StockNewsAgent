FROM python:3.11-slim

WORKDIR /app

# Build deps for statsmodels (drags in scipy, which compiles on slim).
RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc g++ \
    && rm -rf /var/lib/apt/lists/*

# Build context is the repo root (see docker-compose.yml); requirements.txt
# lives at the root and is shared by the service container.
COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt

COPY service /app/service

EXPOSE 8000
CMD ["uvicorn", "service.main:app", "--host", "0.0.0.0", "--port", "8000"]
