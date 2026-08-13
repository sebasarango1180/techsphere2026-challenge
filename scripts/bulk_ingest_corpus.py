# /// script
# requires-python = ">=3.11"
# dependencies = ["httpx"]
# ///
"""Bulk-loads the given challenge corpus (dataset/textos/*/*.pdf) into the knowledge
base via api-gateway's real ingest path -- POST /documents for each PDF, same endpoint
the admin console uses, so identity ownership stays with api-gateway (plan §2.4) rather
than this script talking to vector-store directly.

This is the "at init, all PDFs must be OCR'd and vectorized" requirement: without this
script, the ONLY way documents get into the knowledge base is one-at-a-time through the
admin console. `scripts/setup.sh` now runs this BLOCKING, not backgrounded -- a system
that can't answer from the knowledge base isn't "corriendo y accesible" yet, so this
counts against the timed G2 boot like everything else. Idempotent by design (safe to
re-run): skips any document whose *actual ingest status* is "ready" (checked live via
`GET /documents/{id}/status`, not just title presence in Postgres -- an earlier version
used title presence, and a run that got interrupted mid-flight left ~107 documents with a
Postgres row but no real embeddings, which the title-only check then skipped forever on
retry, silently).

concurrency=3 default: NOT the original 3 for the original reason. vector-store's
/v1/ingest used to block its single event loop entirely (fixed), and separately, BOTH
BGE-M3 embedding AND ChromaDB's client turned out to be unsafe under concurrent calls --
the embedding one just contends for CPU (4 concurrent encode() calls took ~5.05s EACH vs
1.49s total for 4 sequential), but the ChromaDB one actively corrupted requests
(`AttributeError: 'RustBindingsAPI' object has no attribute 'bindings'`), found live
running a real bulk-load under concurrency=8. Both are now serialized behind locks in
vector-store (see app/embeddings.py, app/store.py) -- correct, but it means the
embed+store tail of every document's processing is fully one-at-a-time regardless of
how many requests arrive concurrently. Pushed concurrency to 8 anyway once for a live
stress test: 5 of 8 concurrent uploads hit api-gateway's 5-minute ingest timeout waiting
in that queue (`context deadline exceeded`) even though nothing was actually broken --
they just never got their turn at the lock in time. concurrency=3 keeps enough overlap
to pipeline OCR (genuinely parallel, tesseract subprocesses) against the next document
while one is in the locked embed+store phase, without queueing deep enough to risk that
timeout. Raise it only if a future fix removes the ChromaDB/embedding serialization.

Standalone `uv run` script (PEP 723 inline metadata above) rather than living inside any
one service's dependency tree -- it only needs an HTTP client and the filesystem, not
FastAPI/Gin/livekit-agents.

Usage:
    uv run scripts/bulk_ingest_corpus.py
    uv run scripts/bulk_ingest_corpus.py --api-url http://localhost:8080 --concurrency 3
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from pathlib import Path

import httpx

# Generous: OCR (a scanned page) + BGE-M3 embedding a full document can genuinely take a
# while (see services/api-gateway/internal/vectorstore/client.go's ingestTimeout, sized
# from the same real observation).
INGEST_TIMEOUT = httpx.Timeout(300.0)


def category_for_folder(folder_name: str) -> str:
    """dataset/textos/'s five folder names map to the clean category key
    (docs/dataset-eda.md §4's `modulo_synthea` taxonomy) via a plain lowercase +
    space-to-underscore transform -- verified this covers all five real folder names
    correctly (Appendicitis, breast_cancer, cholecystitis, "colorectal cancer",
    "total joint replacement") rather than assumed."""
    return folder_name.strip().lower().replace(" ", "_")


async def ready_titles(client: httpx.AsyncClient, api_url: str, sem: asyncio.Semaphore) -> set[str]:
    """Which titles are safe to skip -- deliberately NOT "which titles exist in Postgres".
    Found live, the hard way: `documents.status` (active/deleted) is a document
    lifecycle flag set at creation time, not an ingest-success signal -- a document whose
    vector-store ingest failed (or never finished) still shows up as "active" in
    `GET /documents`. An earlier version of this function used title-presence there as
    the skip check, and after a run got interrupted mid-flight (a vector-store restart
    killed in-flight/queued requests), a re-run silently skipped every one of those
    broken documents forever, since their titles already "existed" -- exactly the kind
    of failure this idempotency check exists to avoid. The real signal is each
    document's *version* status, from `GET /documents/{id}/status`
    (`document_versions.status`, set to "ready" only after vector-store's ingest
    actually succeeds -- see services/api-gateway's documents.go `ingestVersion`)."""
    resp = await client.get(f"{api_url}/api/v1/documents")
    resp.raise_for_status()
    docs = resp.json()

    async def _check(doc: dict) -> str | None:
        if doc.get("status") != "active":  # e.g. "deleted" -- not part of the live corpus
            return None
        async with sem:
            try:
                r = await client.get(f"{api_url}/api/v1/documents/{doc['id']}/status")
                r.raise_for_status()
                if r.json().get("status") == "ready":
                    return doc["title"]
            except Exception:
                pass
        return None

    results = await asyncio.gather(*(_check(d) for d in docs))
    return {title for title in results if title is not None}


async def ingest_one(client: httpx.AsyncClient, api_url: str, pdf_path: Path, category: str, sem: asyncio.Semaphore) -> tuple[Path, bool, str]:
    title = pdf_path.stem
    async with sem:
        try:
            with pdf_path.open("rb") as f:
                resp = await client.post(
                    f"{api_url}/api/v1/documents",
                    data={"title": title, "category": category},
                    files={"file": (pdf_path.name, f, "application/pdf")},
                    timeout=INGEST_TIMEOUT,
                )
            if resp.status_code >= 300:
                return pdf_path, False, f"HTTP {resp.status_code}: {resp.text[:200]}"
            return pdf_path, True, resp.json().get("status", "?")
        except Exception as e:  # noqa: BLE001 -- one bad file must not abort the whole corpus
            return pdf_path, False, str(e)


async def main() -> int:
    # scripts/setup.sh backgrounds this with stdout redirected to a file, not a TTY --
    # Python fully buffers stdout in that case (vs. line-buffering for a TTY), so without
    # this, every print() below sits invisible in an internal buffer for the entire ~2h+
    # run instead of showing up in bulk_ingest.log as it happens. Found live: the process
    # was genuinely running and making progress, but the log stayed empty the whole time.
    sys.stdout.reconfigure(line_buffering=True)

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-url", default=os.environ.get("API_GATEWAY_URL", "http://localhost:8080"))
    parser.add_argument(
        "--dataset-path",
        default=os.environ.get("DATASET_PATH", "../ParticipantArtifacts/dataset"),
        help="Path to the dataset/ directory (containing textos/) -- default matches scripts/setup.sh's convention",
    )
    parser.add_argument("--concurrency", type=int, default=3)
    args = parser.parse_args()

    textos_dir = Path(args.dataset_path) / "textos"
    if not textos_dir.is_dir():
        print(f"ERROR: {textos_dir} not found -- run scripts/setup.sh first (it clones ParticipantArtifacts)", file=sys.stderr)
        return 1

    pdfs: list[tuple[Path, str]] = []
    for folder in sorted(p for p in textos_dir.iterdir() if p.is_dir()):
        category = category_for_folder(folder.name)
        for pdf_path in sorted(folder.glob("*.pdf")):
            pdfs.append((pdf_path, category))

    if not pdfs:
        print(f"No PDFs found under {textos_dir}")
        return 0

    print(f"Found {len(pdfs)} PDFs across {len(set(c for _, c in pdfs))} categories in {textos_dir}")

    start = time.monotonic()
    sem = asyncio.Semaphore(args.concurrency)
    async with httpx.AsyncClient() as client:
        try:
            already = await ready_titles(client, args.api_url, sem)
        except Exception as e:
            print(f"ERROR: could not reach api-gateway at {args.api_url}: {e}", file=sys.stderr)
            return 1

        todo = [(p, c) for p, c in pdfs if p.stem not in already]
        skipped = len(pdfs) - len(todo)
        print(f"{skipped} already ingested (skipping), {len(todo)} to process (concurrency={args.concurrency})")

        results = await asyncio.gather(*(ingest_one(client, args.api_url, p, c, sem) for p, c in todo))

    ok = [r for r in results if r[1]]
    failed = [r for r in results if not r[1]]

    for path, _, detail in failed:
        print(f"FAILED: {path.name}: {detail}", file=sys.stderr)

    elapsed = time.monotonic() - start
    print(
        f"\nDone in {elapsed:.1f}s -- {len(ok)} ingested, {len(failed)} failed, {skipped} already present "
        f"(out of {len(pdfs)} total PDFs in the corpus)."
    )
    # Non-fatal on individual failures (a handful of unreadable PDFs shouldn't fail the
    # whole bulk load) -- but a wholesale failure (e.g. api-gateway unreachable) already
    # returned 1 above, and a high failure rate here is worth a human looking at.
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
