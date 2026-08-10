# CPU-only base (torch, ctranslate2 for faster-whisper, onnxruntime for VAD/turn-detector).
# See specs/implementation-plan.md §2.5: on macOS this whole service is meant to run
# natively via `uv run` instead (Metal isn't available in Docker Desktop) -- this image is
# for Linux hosts and the CI/CPU-only path. The docker-compose.gpu.yml override's TODO in
# services/vector-store/Dockerfile applies here too for a CUDA build.
FROM python:3.11-slim AS base

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY app ./app

# Pre-download the small plugin-side models (Silero VAD, multilingual turn detector) at
# build time so they're not part of the timed cold-start clock (plan §8's 15-minute
# budget). This does NOT cover Ollama's model pull or BGE-M3/Kokoro -- those are handled
# by scripts/setup.sh's parallel-download step and vector-store's own lazy load
# respectively.
RUN uv run python -m app.main download-files

CMD ["uv", "run", "python", "-m", "app.main", "start"]
