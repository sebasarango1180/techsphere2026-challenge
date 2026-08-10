# vector-store

Ingestion + hybrid search engine. Contract: [`../../docs/openapi/vector-store.yaml`](../../docs/openapi/vector-store.yaml),
rationale: [`../../specs/implementation-plan.md`](../../specs/implementation-plan.md) §2.4, §4.2.

```
app/main.py            FastAPI routes: /v1/healthz, /v1/ingest, /v1/documents/{id}(/status), /v1/search
app/chunking.py         PDF text extraction (PyMuPDF) + OCR fallback (pytesseract) + sliding-window chunker
app/embeddings.py       BGE-M3 dense embeddings, pre-warmed at startup (see docstring for why)
app/store.py            ChromaDB CRUD: versioning, soft delete, the `combine_where` gotcha (see below)
app/hybrid_search.py    BM25 + dense candidates, fused with Reciprocal Rank Fusion
app/config.py           env-driven settings (pydantic-settings)
```

## Status

Ingestion, versioning, soft-delete, and hybrid search all have real, tested logic behind
them -- this is not a stub service. Several pieces are now verified against a real,
running stack (Postgres + this service + api-gateway + a real LiveKit server), not just
unit-tested in isolation: document create → search → re-index (new version) → search
again confirms only the new version's chunks are returned, with correct
`document_id`/`page` citations throughout. See
[`../../docs/dataset-eda.md`](../../docs/dataset-eda.md) for what the actual corpus looks
like. What's still open:

- [ ] Language detection per document (`app/main.py` hardcodes `lang="es"`)
- [x] ~~OCR fallback for scanned PDFs~~ -- switched from `pypdf` to PyMuPDF (`pymupdf`),
      which can both extract text AND render a page to an image for `pytesseract` OCR in
      one library. Verified against the actual scanned PDF in the corpus (dataset-eda.md
      §6): real, usable Spanish medical text comes out, not garbage. Needs
      `tesseract-ocr`/`tesseract-ocr-spa` on the host for local (non-Docker) runs --
      already in the Dockerfile.
- [x] ~~Encrypted PDF support~~ -- PyMuPDF opens the AES-encrypted corpus PDF directly, no
      extra dependency needed (this actually made `pypdf` + the `cryptography` package
      it needed for this one file both removable -- see `app/chunking.py`'s docstring)
- [x] ~~Retrieval quality risk: hard category filter on an untrustworthy label~~ --
      `category` is now a soft `category_hint` ranking boost (`app/hybrid_search.py`),
      never a hard `where` exclusion; verified both categories still return with a
      matching one ranked first. See dataset-eda.md §5,§7 for the reasoning.
- [x] ~~Cold-start race: first request pays BGE-M3's load cost~~ -- found for real (an
      api-gateway ingest call timed out at 60s waiting for this service's *first* request
      to finish loading the model, even though it eventually succeeded). Now pre-warmed
      as a background task at process startup; `/v1/healthz` returns `503 {"status":
      "loading"}` until warmup finishes, `200` after -- verified against a real container
      build, not just reviewed.
- [x] ~~`/v1/ingest` blocked the event loop~~ -- it was `async def` but ran OCR + BGE-M3
      embedding (both CPU-bound, genuinely slow) directly on the event loop instead of
      via `asyncio.to_thread` (every other endpoint here is a plain `def`, which FastAPI
      auto-threads; `ingest` was the one inconsistent case). Found live, not by
      inspection: this single-worker uvicorn process could only process one ingest at a
      time no matter the client's concurrency setting, AND its own `/v1/healthz`
      healthcheck was timing out during an in-flight ingest (confirmed via `docker
      inspect`'s health log: `Health check exceeded timeout (3s)`, not a clean 503).
      Worse than a throughput problem: a `/v1/search` call arriving during a background
      bulk-load (`scripts/bulk_ingest_corpus.py`, which this service's own design
      assumes runs concurrently with live traffic) would have queued behind whichever
      document was mid-OCR. Fixed by moving the CPU-bound path into a plain sync helper
      called via `asyncio.to_thread`, verified live: the container's Docker healthcheck
      went from `unhealthy` (repeated timeouts) to `healthy` under load after the fix.
- [ ] BM25 index caching (`app/hybrid_search.py` rebuilds it from Chroma on every search
      call -- fine at this corpus size, revisit if latency data says otherwise)
- [ ] GPU/CUDA torch wheel for the `docker-compose.gpu.yml` profile (Dockerfile TODO)

## A ChromaDB gotcha worth knowing before touching `where` clauses

The pinned chromadb version rejects a bare multi-key `where` dict (`{"a": 1, "b": 2}`)
with `ValueError: Expected where to have exactly one operator` -- it wants `{"$and": [...]}`
for anything beyond a single condition. `app/store.py`'s `combine_where()` handles this;
route every new multi-condition filter through it rather than hand-building a dict.

## Bulk-loading the given corpus

Not this service's job directly -- see `../../scripts/bulk_ingest_corpus.py`, which walks
`dataset/textos/*/*.pdf` and POSTs each one through **api-gateway's** `/documents`
endpoint (identity ownership stays with api-gateway, plan §2.4). Real measured cost:
~23s/document (OCR + embedding dominate) -- see that script's docstring and plan §8 for
why `scripts/setup.sh` runs it in the background rather than blocking on it.

## Run locally (outside Docker)

```sh
export CHROMA_PERSIST_DIR=/tmp/chroma-dev   # /data/chroma is the container-only default
uv run uvicorn app.main:app --port 8001 --reload
```

Note: startup now pre-warms BGE-M3 (~2GB, downloaded from Hugging Face on first use) as a
background task -- `/v1/healthz` reports `503 "loading"` until it's ready, `200` after.
Don't be alarmed if that takes a while on a cold cache; it's the same cost the 15-minute
cold-start budget (plan §8) has to account for, just no longer hidden inside whichever
request happens to arrive first.
