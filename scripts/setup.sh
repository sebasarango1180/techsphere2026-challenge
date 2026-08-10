#!/usr/bin/env bash
# One-shot bootstrap: clone dataset if needed, fetch models, build and start every
# service, wait for health, print the URLs the README should point graders at.
#
# This is what G2's 15-minute clock measures end to end (see specs/implementation-plan.md
# §0, §8, §9) -- keep every slow step (image builds, model downloads, dataset clone)
# running in parallel, not sequential, and keep this script the *only* documented path
# so the README and the timed reality never drift apart.
#
# Usage:
#   ./scripts/setup.sh                 # auto-detect OS/hardware and pick a mode
#   ./scripts/setup.sh --native-agent   # force macOS-style native ollama + voice-agent
#                                        # (Docker Desktop cannot pass through Metal --
#                                        # see plan §2.5). Auto-selected on macOS already.
#   ./scripts/setup.sh --docker-only    # force everything in Docker even on macOS
#                                        # (CPU only, no Metal -- useful for CI/testing)
#
# TODO(workstream E): this is the v1 happy-path skeleton. Still needed before relying on
# it for the timed run: precise per-service readiness probes (curl retry loops below are
# placeholders), a matching scripts/teardown.sh, and a `--dataset-path` flag to skip the
# clone when the grader already has ParticipantArtifacts checked out.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

START_TS=$(date +%s)
MODE="auto"
for arg in "$@"; do
  case "$arg" in
    --native-agent) MODE="native" ;;
    --docker-only)  MODE="docker" ;;
    *) echo "Unknown flag: $arg" >&2; exit 1 ;;
  esac
done

log() { printf '\n\033[1;36m==> %s\033[0m\n' "$1"; }
warn() { printf '\033[1;33m!! %s\033[0m\n' "$1" >&2; }
die() { printf '\033[1;31mFATAL: %s\033[0m\n' "$1" >&2; exit 1; }

# ---------------------------------------------------------------------------
# 1. Preconditions
# ---------------------------------------------------------------------------
log "Checking required tools"
command -v docker >/dev/null 2>&1 || die "docker is required: https://docs.docker.com/get-docker/"
docker compose version >/dev/null 2>&1 || die "docker compose v2 is required (bundled with recent Docker Desktop/Engine)"

OS="$(uname -s)"
if [ "$MODE" = "auto" ]; then
  if [ "$OS" = "Darwin" ]; then
    MODE="native"
    log "macOS detected -> native mode (Ollama + voice-agent run on host for Metal acceleration; see plan §2.5)"
  else
    MODE="docker"
    log "$OS detected -> full-Docker mode"
  fi
fi

HAS_NVIDIA=false
if command -v nvidia-smi >/dev/null 2>&1; then
  HAS_NVIDIA=true
  log "NVIDIA GPU detected -> will apply docker-compose.gpu.yml"
fi

if [ "$MODE" = "native" ]; then
  command -v uv >/dev/null 2>&1 || die "uv is required for native mode: https://docs.astral.sh/uv/getting-started/installation/"
  command -v ollama >/dev/null 2>&1 || die "ollama is required for native mode: https://ollama.com/download"
fi

# ---------------------------------------------------------------------------
# 2. Environment
# ---------------------------------------------------------------------------
if [ ! -f .env ]; then
  log "Creating .env from .env.example"
  cp .env.example .env
  warn "Using default/placeholder credentials in .env -- fine for local grading, rotate before any real deployment."
fi
# shellcheck disable=SC1091
set -a; source .env; set +a

# ---------------------------------------------------------------------------
# 3. Dataset (PDF corpus lives in the separate ParticipantArtifacts repo, not vendored
#    here -- see plan §5). Clone happens in parallel with image builds/model pulls below.
# ---------------------------------------------------------------------------
DATASET_DIR="${DATASET_PATH:-../ParticipantArtifacts/dataset}"
clone_dataset() {
  if [ -d "$DATASET_DIR" ]; then
    log "Dataset already present at $DATASET_DIR, skipping clone"
    return
  fi
  log "Cloning ParticipantArtifacts (dataset corpus) into ../ParticipantArtifacts"
  git clone --depth 1 https://github.com/TechSphere2026/ParticipantArtifacts.git ../ParticipantArtifacts
}

# ---------------------------------------------------------------------------
# 4. Kick off every slow step in parallel: dataset clone, image builds, and the Ollama
#    model pull. This is the single highest-leverage thing for staying under the
#    15-minute G2 budget -- see plan §8.
#
#    The model pull used to only happen in native mode -- in docker mode the `ollama`
#    container came up with NO model in it at all (a Docker Desktop volume mount can't
#    reuse a native `ollama pull`'s cache across the container boundary), meaning the
#    very first LLM call in a fresh docker-mode run would fail outright, not just be
#    slow. Fixed by starting just the `ollama` container early (its image is a stock
#    pull, not a build, so this overlaps with `build --parallel` below for free) and
#    driving the pull over its HTTP API rather than the CLI, since the CLI isn't
#    guaranteed to exist on the host in docker mode.
# ---------------------------------------------------------------------------
log "Starting dataset clone, image builds, and model pulls in parallel"
clone_dataset &
PID_DATASET=$!

COMPOSE_FILES=(-f docker-compose.yml)
# "tqida" is every service in this project (see docker-compose.yml's top comment);
# ollama/voice-agent additionally require docker-models/docker-agent, which we only add
# in docker mode -- that's what keeps them out of both the build and the run in native
# mode, not just the run.
COMPOSE_PROFILES=(--profile tqida)
if [ "$MODE" = "docker" ]; then
  COMPOSE_PROFILES+=(--profile docker-models --profile docker-agent)
  if $HAS_NVIDIA; then COMPOSE_FILES+=(-f docker-compose.gpu.yml); fi
fi

COMPOSE_BAKE=true docker compose "${COMPOSE_FILES[@]}" "${COMPOSE_PROFILES[@]}" build --parallel &
PID_BUILD=$!

# postgres:16-alpine (~411MB) and livekit/livekit-server (~116MB) are stock images, no
# `build:` key -- `build --parallel` above doesn't touch them at all, so without this
# they'd only get pulled at `up -d` in step 5, serially after everything else here is
# already done. Same gap as ollama/vector-store, smaller in absolute size but the same
# "every heavy pull must be early and parallel, not an afterthought" principle.
docker compose "${COMPOSE_FILES[@]}" "${COMPOSE_PROFILES[@]}" pull postgres livekit &
PID_PULL_STOCK_IMAGES=$!

OLLAMA_MODEL="${OLLAMA_MODEL:-phi3.5:3.8b}"
OLLAMA_API=""
PID_OLLAMA_PULL=""
PID_AGENT_DOWNLOAD_FILES=""
if [ "$MODE" = "native" ]; then
  # Assumes `ollama serve` is already running (Ollama.app on macOS starts it automatically;
  # otherwise `ollama serve &` first). TODO(workstream E): detect and start it if not up.
  OLLAMA_API="http://localhost:11434"
  # .env's OLLAMA_HOST is the in-Docker-network value (http://ollama:11434, for services
  # running INSIDE the compose network) and was already sourced into this shell's
  # environment in step 2 -- the `ollama` CLI itself also reads OLLAMA_HOST, so without
  # this override it inherits that value and fails to resolve "ollama" from the host
  # (`dial tcp: lookup ollama: no such host`). Override it here to the host-reachable URL.
  OLLAMA_HOST="$OLLAMA_API" ollama pull "$OLLAMA_MODEL" &
  PID_OLLAMA_PULL=$!

  # Docker mode gets this for free at image-build time (see voice-agent/Dockerfile) --
  # native mode runs voice-agent straight from this checkout, so without this step the
  # turn-detector plugin's ONNX weights (livekit-agents' `download-files` mechanism,
  # backed by a HuggingFace Hub fetch) would download on whichever call is first instead.
  (cd services/voice-agent && uv run python -m app.main download-files >/dev/null 2>&1) &
  PID_AGENT_DOWNLOAD_FILES=$!
else
  # Bring up just the ollama container now (stock image, no build dependency) so its
  # pull overlaps with the other services' `build --parallel` above instead of waiting
  # behind it.
  COMPOSE_BAKE=true docker compose "${COMPOSE_FILES[@]}" "${COMPOSE_PROFILES[@]}" up -d ollama
  OLLAMA_API="http://localhost:11434"  # docker-compose.yml maps this port statically
  (
    tries=30
    until curl -sf "$OLLAMA_API/api/tags" >/dev/null 2>&1; do
      tries=$((tries - 1))
      [ "$tries" -le 0 ] && { echo "ollama container did not become reachable" >&2; exit 1; }
      sleep 2
    done
    curl -sf "$OLLAMA_API/api/pull" -d "{\"model\":\"$OLLAMA_MODEL\"}" >/dev/null
  ) &
  PID_OLLAMA_PULL=$!
fi

# vector-store's BGE-M3 embedding model (~2.2GB) used to only start downloading once
# `docker compose up -d` ran in step 5 -- i.e. AFTER waiting for every other service's
# build to finish too, not overlapped with anything. Measured live on a cold HuggingFace
# cache: ~9.5 minutes just for that download, serially eating into the 15-minute G2
# budget for no reason, the same class of gap as Ollama's pull above. Fixed the same
# way: build vector-store's image on its own first (it's the dependency, unlike ollama
# which needs no build at all), start its container immediately once that's done, and
# let its own startup-time background warmup (`app/main.py`'s `_warm_up_embedder`,
# already pre-existing) begin overlapping with the OTHER services' still-in-progress
# `build --parallel` above rather than waiting for them.
(
  COMPOSE_BAKE=true docker compose "${COMPOSE_FILES[@]}" "${COMPOSE_PROFILES[@]}" build vector-store &&
  COMPOSE_BAKE=true docker compose "${COMPOSE_FILES[@]}" "${COMPOSE_PROFILES[@]}" up -d vector-store
) &
PID_VECTOR_STORE_EARLY=$!

wait "$PID_DATASET"  || die "Dataset clone failed"
wait "$PID_BUILD"    || die "Image build failed"
wait "$PID_PULL_STOCK_IMAGES" || die "postgres/livekit image pull failed"
if [ -n "$PID_OLLAMA_PULL" ]; then
  wait "$PID_OLLAMA_PULL" || die "Ollama model pull failed"
fi
if [ -n "$PID_AGENT_DOWNLOAD_FILES" ]; then
  wait "$PID_AGENT_DOWNLOAD_FILES" || warn "voice-agent download-files failed -- turn-detector weights will download on first real call instead"
fi
wait "$PID_VECTOR_STORE_EARLY" || die "vector-store build/start failed"

# Force the model into memory now rather than letting the first real voice-agent call
# pay Ollama's own load cost -- the same class of bug already found and fixed for
# BGE-M3 in vector-store (see its README): an empty-prompt /api/generate call is
# Ollama's documented way to load a model without generating anything, verified live
# against a real local Ollama instance during development.
log "Warming up Ollama model $OLLAMA_MODEL"
curl -sf "$OLLAMA_API/api/generate" -d "{\"model\":\"$OLLAMA_MODEL\",\"prompt\":\"\"}" >/dev/null \
  || warn "Ollama warmup call failed -- the first real call in voice-agent will pay the load cost instead"

# vector-store's own /v1/healthz gates on BGE-M3 actually being warm (not just the
# container being up) -- wait for it here, now that its download/load has had the whole
# build phase above to run in the background, instead of the short, easy-to-blow budget
# step 6 used to give it after everything else was already done.
log "Waiting for vector-store's embedding model to finish warming up"
tries=180
until curl -sf http://localhost:8001/v1/healthz >/dev/null 2>&1; do
  tries=$((tries - 1))
  [ "$tries" -le 0 ] && { warn "vector-store did not finish warming up in time -- check docker compose logs vector-store"; break; }
  sleep 2
done

# ---------------------------------------------------------------------------
# 5. Start the stack
# ---------------------------------------------------------------------------
log "Starting services (mode=$MODE)"
COMPOSE_BAKE=true docker compose "${COMPOSE_FILES[@]}" "${COMPOSE_PROFILES[@]}" up -d

if [ "$MODE" = "native" ]; then
  log "Starting voice-agent natively via uv"
  (
    cd services/voice-agent
    OLLAMA_HOST="http://localhost:11434" \
    VECTOR_STORE_URL="http://localhost:8001" \
    LIVEKIT_URL="ws://localhost:7880" \
    DATABASE_URL="postgres://${POSTGRES_USER:-techsphere}:${POSTGRES_PASSWORD:-changeme}@localhost:5432/${POSTGRES_DB:-techsphere}?sslmode=disable" \
    uv run python -m app.main >"$REPO_ROOT/voice-agent.native.log" 2>&1 &
    echo $! > "$REPO_ROOT/voice-agent.native.pid"
  )
  log "voice-agent running natively (PID $(cat "$REPO_ROOT/voice-agent.native.pid")); logs at voice-agent.native.log"
  warn "To stop it: kill \$(cat voice-agent.native.pid)"
fi

# ---------------------------------------------------------------------------
# 6. Wait for health (vector-store was already waited on above, right after its early
# build+start -- this is just api-gateway, which doesn't exist until `up -d` above).
# TODO(workstream E): replace this fixed retry loop with a real readiness endpoint check
# once there's more than one condition to wait on here.
# ---------------------------------------------------------------------------
log "Waiting for api-gateway to answer"
wait_http() {
  local url="$1" name="$2" tries=60
  until curl -sf "$url" >/dev/null 2>&1; do
    tries=$((tries - 1))
    [ "$tries" -le 0 ] && { warn "$name did not become ready in time ($url) -- check docker compose logs"; return 1; }
    sleep 2
  done
  echo "  $name OK"
}
wait_http "http://localhost:8080/healthz" "api-gateway" || true

# ---------------------------------------------------------------------------
# 7. Bulk-load the given knowledge base corpus (dataset/textos/*.pdf) -- BLOCKING.
#
# Previously this ran in the background on the theory that G2 measures "corriendo y
# accesible", not the full corpus being pre-loaded. Overruled: a system that can't
# answer a patient's question from the knowledge base isn't actually usable yet, so
# "accessible" means the KB is loaded too, not just that the process is listening. This
# is now counted in the timed boot, same as everything else in this script.
#
# Made tractable by three real bugs found and fixed this session, in vector-store (see
# its README): (1) /v1/ingest used to block the single event loop on every request,
# making client-side concurrency a no-op -- fixed via asyncio.to_thread; (2) running
# BGE-M3 embedding calls concurrently made each one ~13x SLOWER via CPU contention, not
# faster (measured: 4 concurrent encode() calls took 5.05s EACH vs 1.49s total for 4
# sequential) -- fixed with a lock around just the embedding step; (3) ChromaDB's client
# is not thread-safe under concurrent access -- concurrency=8 produced real corrupted
# requests (`AttributeError: 'RustBindingsAPI' object has no attribute 'bindings'`), not
# just slow ones -- fixed with a second lock around Chroma reads/writes. Net effect:
# embed+store is now fully serial per document (correctness over throughput), so
# --concurrency stays low (3) -- enough to pipeline OCR against the locked phase, not
# enough to queue deep enough to hit api-gateway's 5-minute ingest timeout (measured:
# concurrency=8 timed out 5 of 8 requests waiting for the lock, though nothing was
# actually broken -- they just never got their turn in time).
# ---------------------------------------------------------------------------
log "Loading the knowledge base corpus (blocking -- see this step's comment for why)"
DATASET_PATH="$DATASET_DIR" uv run scripts/bulk_ingest_corpus.py \
  --api-url "http://localhost:${PORT:-8080}" \
  --concurrency "${BULK_INGEST_CONCURRENCY:-3}" \
  2>&1 | tee "$REPO_ROOT/bulk_ingest.log"
BULK_INGEST_STATUS="${PIPESTATUS[0]}"
[ "$BULK_INGEST_STATUS" -eq 0 ] || die "Knowledge base bulk-load failed (see bulk_ingest.log) -- the system is not usable without it, not a soft failure"

# ---------------------------------------------------------------------------
# 8. Done
# ---------------------------------------------------------------------------
ELAPSED=$(( $(date +%s) - START_TS ))
log "Setup complete in ${ELAPSED}s ($(( ELAPSED / 60 ))m $(( ELAPSED % 60 ))s)"
cat <<EOF

  Call interface:   http://localhost:5173
  Admin console:    http://localhost:5174
  API docs:         http://localhost:8080/docs
  LiveKit URL:       ${LIVEKIT_URL:-ws://localhost:7880}

Record the elapsed time above in the README's "levantamiento" section (G2 is timed
against exactly this command). The knowledge base corpus is fully loaded and searchable
-- this includes that time, not just the process being up.
EOF
