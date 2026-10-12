# Collector image for the cloud content-sync job.
# Build context is the repo root (config.yaml must be included):
#   docker build -f ingest_utils/confluence_processor/Dockerfile .
FROM python:3.12-slim

# libreoffice: docx/pptx -> pdf (video chunking happens in the ingest state machine)
RUN apt-get update && apt-get install -y --no-install-recommends \
        libreoffice-writer \
        libreoffice-impress \
        fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY ingest_utils/confluence_processor/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Scripts resolve config.yaml two directories above their own location, and
# pipeline_config.py resolves src/ingest/shared the same way
COPY config.yaml /app/config.yaml
COPY src/ingest/shared/processing_config.py /app/src/ingest/shared/processing_config.py
COPY ingest_utils/confluence_processor/*.py /app/ingest_utils/confluence_processor/

WORKDIR /app/ingest_utils/confluence_processor
CMD ["python", "run_collector.py"]
