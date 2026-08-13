#!/usr/bin/env bash
# Restores a knowledge-base seed built by scripts/export_kb_seed.sh -- Postgres
# documents/document_versions rows + a snapshot of the chroma_data Docker volume -- so
# setup.sh can skip re-running OCR + BGE-M3 embedding over the full given corpus on
# every fresh boot.
#
# MUST run before the chroma container starts (see setup.sh's call site) -- it writes
# directly into the chroma_data named volume via a throwaway container, and doing that
# while the real chroma server already has that sqlite file open is not safe -- and
# after Postgres is up and migrated.
#
# Silent no-op (exit 0, nothing restored) if the seed archive isn't present, or if
# Postgres's documents table is already non-empty (avoids double-applying INSERTs that
# have no ON CONFLICT clause) -- both are meant to be safe to call unconditionally from
# setup.sh and fall through to the normal live-ingest path.
#
# Usage:
#   ./scripts/import_kb_seed.sh
#   ./scripts/import_kb_seed.sh --seed-file /path/to/kb-seed.tar.gz

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

log() { printf '\n\033[1;36m==> %s\033[0m\n' "$1"; }

SEED_FILE="$REPO_ROOT/kb-seed.tar.gz"
for arg in "$@"; do
  case "$arg" in
    --seed-file=*) SEED_FILE="${arg#--seed-file=}" ;;
    --seed-file) shift; SEED_FILE="$1" ;;
  esac
done

if [ ! -f "$SEED_FILE" ]; then
  log "No KB seed found at $SEED_FILE -- skipping, full live ingest will run instead"
  exit 0
fi

if [ -f .env ]; then set -a; source .env; set +a; fi

# Runs psql INSIDE the postgres container rather than requiring it installed on the
# host (found live: it wasn't) -- see export_kb_seed.sh's matching note.
EXISTING=$(docker compose exec -T postgres psql -tA \
  --username="${POSTGRES_USER:-techsphere}" --dbname="${POSTGRES_DB:-techsphere}" \
  -c "SELECT count(*) FROM documents" 2>/dev/null || echo "-1")

if [ "$EXISTING" != "0" ]; then
  log "documents table is not empty ($EXISTING rows, or Postgres not reachable yet) -- skipping KB seed restore, letting the normal ingest path reconcile instead"
  exit 0
fi

log "Restoring KB seed from $SEED_FILE"
RESTORE_DIR="$REPO_ROOT/.kb-seed-restore"
rm -rf "$RESTORE_DIR"
mkdir -p "$RESTORE_DIR"
tar -xzf "$SEED_FILE" -C "$RESTORE_DIR"

log "Copying Chroma data into the chroma_data volume via a throwaway container"
docker volume create techsphere2026_chroma_data >/dev/null
docker run --rm \
  -v techsphere2026_chroma_data:/data \
  -v "$RESTORE_DIR/chroma":/seed:ro \
  alpine sh -c "rm -rf /data/* /data/.[!.]* 2>/dev/null; cp -a /seed/. /data/"
log "Chroma data restored into the chroma_data volume"

docker compose exec -T postgres psql \
  --username="${POSTGRES_USER:-techsphere}" --dbname="${POSTGRES_DB:-techsphere}" \
  -v ON_ERROR_STOP=1 \
  < "$RESTORE_DIR/postgres-seed.sql" >/dev/null
COUNT=$(docker compose exec -T postgres psql -tA \
  --username="${POSTGRES_USER:-techsphere}" --dbname="${POSTGRES_DB:-techsphere}" \
  -c "SELECT count(*) FROM documents")
log "Postgres restored: $COUNT documents rows"

rm -rf "$RESTORE_DIR"
