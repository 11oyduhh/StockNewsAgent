FROM python:3.11-slim

WORKDIR /app

# Minimal deps — no LiteLLM or FastAPI here. psycopg for COPY, pandas
# for the small wide-CSV transforms (securities, fundamentals).
RUN pip install --no-cache-dir \
    'psycopg[binary]>=3.2.0' \
    'pandas>=2.2.0'

COPY ingest.py /app/

CMD ["python", "-u", "ingest.py"]
