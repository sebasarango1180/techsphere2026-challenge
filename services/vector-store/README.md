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
- [x] ~~Two real thread-safety bugs found running a real bulk-load under concurrency~~ --
      (1) running BGE-M3 `encode()` calls concurrently doesn't parallelize, it makes
      EVERY call ~13x slower (measured: 4 concurrent calls took 5.05s each vs 1.49s
      total for 4 sequential -- almost certainly CPU/GPU oversubscription, torch's own
      intra-op threading already saturates the device for one call). (2) ChromaDB's
      client genuinely isn't thread-safe: concurrent access produced real corrupted
      requests (`AttributeError: 'RustBindingsAPI' object has no attribute 'bindings'`,
      `ValueError: Could not connect to tenant default_tenant`), not just contention.
      Both fixed with a `threading.Lock` around just the critical section
      (`_encode_lock` in `app/embeddings.py`, `_chroma_lock` in `app/store.py`) -- OCR/
      PDF extraction (genuinely parallel, tesseract subprocesses) still overlaps across
      concurrent requests, only the embed+store tail is serialized.
- [x] ~~BGE-M3 was CPU-only even on macOS~~ -- not a code bug, an architectural one: this
      service ran in Docker in BOTH modes, and Docker Desktop has no Metal passthrough at
      all, full stop. `FlagEmbedding` already auto-detects MPS correctly when actually
      running on the host (checked its source: falls back through npu/musa/mps/cpu, not
      a CUDA-only check like an unrelated bug found the same way in Kokoro's device
      selection, voice-agent). Moved to native mode alongside ollama/voice-agent (see
      docker-compose.yml, scripts/setup.sh) -- zero code changes needed, just don't run
      it in Docker. Verified live: `get_embedder()`'s `target_devices` reports
      `['mps:0']`, and a real bulk-ingest run went from ~90s/document (Docker/CPU) to
      ~13.5s/document average (native/Metal, full 107-doc corpus, 1449s total, 0
      failures) -- see root README.
- [x] ~~Chroma ran embedded (`PersistentClient`, a local file) instead of as its own
      service~~ -- a real, fair critique of the fix above: moving vector-store natively
      also silently took Chroma out of Docker with it (embedded libraries follow the
      process that opened them), even though Chroma does no GPU/Metal work at all (it's
      disk I/O + a vector index; we don't use its embedding-function hook). Switched to
      `chromadb.HttpClient` against the official `chromadb/chroma` server image
      (docker-compose.yml, pinned to the same version as the `chromadb` client to avoid
      protocol drift) -- Chroma now runs in Docker in BOTH modes, same as
      postgres/livekit, visible in `docker compose ps` again. Verified live: full
      ingest → search round trip against the new architecture, real chunks returned
      with correct citations.
- [ ] BM25 index caching (`app/hybrid_search.py` rebuilds it from Chroma on every search
      call -- fine at this corpus size, revisit if latency data says otherwise)
- [ ] GPU/CUDA torch wheel for the `docker-compose.gpu.yml` profile (Dockerfile TODO) --
      lower priority now that native-mode Metal covers the macOS dev/grading path

## A ChromaDB gotcha worth knowing before touching `where` clauses

The pinned chromadb version rejects a bare multi-key `where` dict (`{"a": 1, "b": 2}`)
with `ValueError: Expected where to have exactly one operator` -- it wants `{"$and": [...]}`
for anything beyond a single condition. `app/store.py`'s `combine_where()` handles this;
route every new multi-condition filter through it rather than hand-building a dict.

## Bulk-loading the given corpus

Not this service's job directly -- see `../../scripts/bulk_ingest_corpus.py`, which walks
`dataset/textos/*/*.pdf` and POSTs each one through **api-gateway's** `/documents`
endpoint (identity ownership stays with api-gateway, plan §2.4). `scripts/setup.sh` now
runs this BLOCKING (a system that can't answer from the knowledge base isn't "corriendo y
accesible" yet) -- see root README for the real measured full-corpus time on native/Metal
vs. the earlier Docker/CPU numbers.

To skip re-running this on every fresh boot, `../../scripts/export_kb_seed.sh` snapshots
an already-ingested corpus (Postgres rows + the `chroma_data` Docker volume) into one
archive; `import_kb_seed.sh` restores it. See both scripts' docstrings -- this doesn't
weaken G5 (tested with a document outside the seed, so the live ingestion pipeline still
has to work for real).

## Run locally (outside Docker)

This is the DEFAULT on macOS now, not just an ad-hoc dev option -- `scripts/setup.sh`
runs this service exactly this way in native-agent mode, to get Metal for BGE-M3 (Docker
Desktop has no Metal passthrough; see docker-compose.yml's top comment). Needs
`tesseract`/`tesseract-lang` on the host (`brew install tesseract tesseract-lang`) for
the OCR fallback -- already baked into the Dockerfile for Docker mode. Chroma itself
still runs in Docker either way (`docker compose up -d chroma`) -- it does no GPU work,
so there's no reason for it to run natively too; this process reaches it over HTTP.

```sh
export CHROMA_HOST=localhost CHROMA_PORT=8000   # Docker Desktop publishes the chroma container's port here
uv run uvicorn app.main:app --port 8001 --reload
```

Note: startup now pre-warms BGE-M3 (~2GB, downloaded from Hugging Face on first use) as a
background task -- `/v1/healthz` reports `503 "loading"` until it's ready, `200` after.
Don't be alarmed if that takes a while on a cold cache; it's the same cost the 15-minute
cold-start budget (plan §8) has to account for, just no longer hidden inside whichever
request happens to arrive first. On macOS this now loads onto `mps:0` automatically
(verified via `get_embedder().target_devices`) -- if you ever see `cpu` there instead on
a Mac, something's wrong (missing MPS build of torch, or accidentally running in Docker).
