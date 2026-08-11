#!/usr/bin/env bash
# Snapshots the CURRENT, already-ingested knowledge base (Postgres documents/
# document_versions rows + the chroma container's `chroma_data` Docker volume) into one
# local archive, so a later `scripts/setup.sh` run can restore it instead of re-running
# OCR + BGE-M3 embedding over the full given corpus from scratch every single boot.
#
# Why both halves, not just Chroma: Postgres's `documents`/`document_versions` rows are
# the identity source of truth (plan §2.4) -- api-gateway's admin console, GET
# /documents, and GET /documents/{id}/status all read Postgres, not Chroma. A
# Chroma-only snapshot would make the corpus searchable but invisible/unmanageable
# through the documented API. Both halves share the same document_id values (Chroma's
# chunk metadata references them), so they're restored together or not at all.
#
# Chroma now runs as its own server (chromadb/chroma image, docker-compose.yml) in BOTH
# modes -- see that file's top comment for why it never needed to leave Docker the way
# ollama/voice-agent/vector-store did (it does no GPU/Metal work). Its data lives in the
# `chroma_data` named volume regardless of native/Docker mode, so exporting it means
# copying out of that volume via a throwaway container, not reading a bare host directory.
#
# Does NOT weaken G5 (the live knowledge-base-update gate): that's tested with a
# document OUTSIDE this seed, so the real live ingestion pipeline (OCR -> embed -> store)
# still has to work for real regardless of whether this seed exists.
#
# Run this ONCE, by hand, after a known-good full-corpus ingest (see root README's KB
# section for how to get there) -- not part of the normal setup.sh flow.
#
# Usage:
#   ./scripts/export_kb_seed.sh

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

log() { printf '\n\033[1;36m==> %s\033[0m\n' "$1"; }
die() { printf '\033[1;31mFATAL: %s\033[0m\n' "$1" >&2; exit 1; }

if [ -f .env ]; then set -a; source .env; set +a; fi

SEED_DIR="$REPO_ROOT/.kb-seed-build"
rm -rf "$SEED_DIR"
mkdir -p "$SEED_DIR/chroma"

log "Dumping documents + document_versions from Postgres"
# Runs pg_dump INSIDE the postgres container rather than requiring it installed on the
# host (found live: neither pg_dump nor psql were actually present here) -- more
# portable, and the container always has a matching server version by construction.
docker compose exec -T postgres pg_dump \
  --username="${POSTGRES_USER:-techsphere}" --dbname="${POSTGRES_DB:-techsphere}" \
  --data-only --table=documents --table=document_versions \
  > "$SEED_DIR/postgres-seed.sql"

# pg_dump's plain-text default is COPY ... FROM stdin; blocks, not individual INSERTs
# (an earlier version of this check assumed INSERT and silently always failed --
# caught live, not by inspection). Count data rows between the COPY marker and its `\.`
# terminator.
COUNT=$(awk '/^COPY public\.documents /{flag=1; next} /^\\\.$/{flag=0} flag' "$SEED_DIR/postgres-seed.sql" | wc -l | tr -d ' ')
[ "$COUNT" -gt 0 ] || die "postgres-seed.sql has zero documents rows -- did you mean to run this against an empty DB?"
log "Dumped $COUNT documents rows"

log "Stopping chroma container before copying its volume"
# Chroma's backing store is a SQLite file -- copying it out while the server still holds
# it open for writes risks a torn/inconsistent snapshot (same reasoning already applied
# on the restore side: scripts/setup.sh doesn't start chroma until AFTER
# import_kb_seed.sh runs, specifically to avoid writing into a live SQLite file). Stop it
# for the duration of the copy, then bring it back regardless of how the copy goes.
CHROMA_WAS_RUNNING=false
if docker compose ps --status running --services 2>/dev/null | grep -qx chroma; then
  CHROMA_WAS_RUNNING=true
  docker compose stop chroma
fi
restart_chroma() {
  if [ "$CHROMA_WAS_RUNNING" = true ]; then
    log "Restarting chroma"
    docker compose start chroma
  fi
}
trap restart_chroma EXIT

log "Copying chroma_data volume contents out via a throwaway container"
docker run --rm \
  -v techsphere2026_chroma_data:/data:ro \
  -v "$SEED_DIR/chroma":/backup \
  alpine cp -a /data/. /backup/
[ -f "$SEED_DIR/chroma/chroma.sqlite3" ] || die "chroma.sqlite3 not found in the copied volume -- is the chroma container actually running with real data?"

OUT="$REPO_ROOT/kb-seed.tar.gz"
log "Building $OUT"
tar -czf "$OUT" -C "$SEED_DIR" postgres-seed.sql chroma
rm -rf "$SEED_DIR"

SIZE=$(du -h "$OUT" | cut -f1)
log "Done: $OUT ($SIZE)"
cat <<EOF

This file is gitignored (too large for normal git history) -- you decide how to publish
it (GitHub Release, Git LFS, object storage, etc.) and where scripts/setup.sh should
fetch it from; see scripts/import_kb_seed.sh for the restore side, which currently only
looks for it at this same local path (kb-seed.tar.gz at repo root).
EOF
