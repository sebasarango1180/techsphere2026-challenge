# Base image bundles CPU PyTorch; see §2.5 in specs/implementation-plan.md for why GPU/
# Metal acceleration for this service is a native-host concern on macOS (Docker Desktop
# has no Metal passthrough), not something this Dockerfile can provide. The
# docker-compose.gpu.yml override adds CUDA passthrough for Linux+NVIDIA hosts; this image
# works there unmodified as long as a CUDA-enabled torch wheel is installed (TODO below).
FROM python:3.11-slim AS base

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates \
    tesseract-ocr tesseract-ocr-spa tesseract-ocr-eng \
    && rm -rf /var/lib/apt/lists/*
# tesseract-ocr-{spa,eng}: OCR fallback for scanned PDF pages (app/chunking.py) -- the
# corpus is Spanish/English mixed (docs/dataset-eda.md §1) and at least one given PDF has
# no text layer at all, confirmed for real, not a theoretical case.

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

WORKDIR /app
COPY pyproject.toml uv.lock ./
# TODO(workstream E/B): for the docker-compose.gpu.yml profile, swap this for a
# CUDA-enabled torch install (`uv pip install torch --index-url
# https://download.pytorch.org/whl/cu121` or similar) -- as written this pulls the
# CPU-only wheel, which works everywhere but won't use a Linux NVIDIA GPU even when one's
# available and requested via the gpu override.
RUN uv sync --frozen --no-dev

COPY app ./app

ENV CHROMA_PERSIST_DIR=/data/chroma
VOLUME ["/data/chroma"]

EXPOSE 8001
# start-period=20s + retries=20*interval=5s = ~120s of margin for the embedding-model
# warmup (app/main.py's startup task) to finish before this is considered unhealthy --
# matches scripts/setup.sh's wait_http retry budget. /v1/healthz returns 503 "loading"
# (not just slow to answer) while warmup is in progress, verified against a real build.
HEALTHCHECK --interval=5s --timeout=3s --start-period=20s --retries=20 \
    CMD curl -f http://localhost:8001/v1/healthz || exit 1

CMD ["uv", "run", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8001"]
