"""FastAPI app implementing docs/openapi/vector-store.yaml / plan §4.2."""

import asyncio
import logging

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse

from app.chunking import chunk_pages, extract_pages
from app.embeddings import embed_texts
from app.hybrid_search import search as hybrid_search
from app.schemas import DocumentStatus, IngestResult, SearchHit, SearchRequest
from app.store import document_status, soft_delete_document, upsert_chunks

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="vector-store", version="0.1.0")

# Loading BGE-M3 is NOT instant (found for real: a first /v1/ingest call took long enough
# to load the model from disk + do its first CPU inference that api-gateway's HTTP client
# timed out at 60s even though this service eventually returned 200). Rather than pay
# that cost on whichever request happens to arrive first, warm it up as a background task
# at process start and make /v1/healthz report 503 "loading" until it's done -- so
# scripts/setup.sh's health-poll loop (and docker-compose healthchecks) correctly wait for
# a service that can actually serve a request quickly, not just one that's listening.
_ready = False


@app.on_event("startup")
async def _warm_up_embedder() -> None:
    async def _warm_up() -> None:
        global _ready
        logger.info("pre-warming embedding model (BGE-M3)...")
        await asyncio.to_thread(embed_texts, ["warmup"])
        _ready = True
        logger.info("embedding model ready")

    asyncio.create_task(_warm_up())


@app.get("/v1/healthz")
def healthz():
    if not _ready:
        return JSONResponse(status_code=503, content={"status": "loading"})
    return {"status": "ok"}


def _ingest_sync(document_id: str, version: int, category: str, file_bytes: bytes) -> int:
    pages = extract_pages(file_bytes)
    chunks = chunk_pages(pages)
    # TODO(workstream B): detect language per document instead of hardcoding "es" --
    # dataset/textos/ mixes Spanish and English sources (see ParticipantArtifacts
    # README). Matters for retrieval quality if we ever want to filter/boost by lang.
    return upsert_chunks(document_id, version, category, lang="es", chunks=chunks)


@app.post("/v1/ingest", response_model=IngestResult)
async def ingest(
    document_id: str = Form(...),
    version: int = Form(...),
    category: str = Form(...),
    file: UploadFile = File(...),
):
    file_bytes = await file.read()

    try:
        # OCR + BGE-M3 embedding are CPU-bound and, per document, genuinely slow (see
        # scripts/bulk_ingest_corpus.py's docstring) -- this used to run inline on the
        # event loop despite the function being `async def`, meaning this single-worker
        # uvicorn process could only ever process one ingest at a time no matter how many
        # concurrent requests arrived, AND any concurrent /v1/search call would queue up
        # behind whichever ingest was in flight. That's the opposite of what background
        # bulk-loading (scripts/setup.sh §7) needs: search has to keep working WHILE
        # ingestion runs in the background. `asyncio.to_thread` (the same pattern already
        # used for the startup warmup call above) offloads this to the default executor so
        # the event loop -- and therefore /v1/search, already a plain `def` FastAPI
        # auto-threads -- stays responsive during ingestion.
        chunk_count = await asyncio.to_thread(_ingest_sync, document_id, version, category, file_bytes)
    except Exception:
        logger.exception("ingest failed for document_id=%s version=%s", document_id, version)
        return JSONResponse(status_code=502, content={"chunk_count": 0, "status": "failed"})

    return IngestResult(chunk_count=chunk_count, status="ready")


@app.delete("/v1/documents/{document_id}", status_code=204)
def delete_document(document_id: str):
    soft_delete_document(document_id)
    return None


@app.get("/v1/documents/{document_id}/status", response_model=DocumentStatus)
def get_document_status(document_id: str):
    status, chunk_count = document_status(document_id)
    return DocumentStatus(status=status, chunk_count=chunk_count)


@app.post("/v1/search", response_model=list[SearchHit])
def search(req: SearchRequest):
    if not req.query.strip():
        raise HTTPException(status_code=400, detail="query must not be empty")

    hits = hybrid_search(req.query, top_k=req.top_k, category_hint=req.category_hint)
    return [
        SearchHit(
            chunk_id=h.chunk_id,
            document_id=h.document_id,
            version=h.version,
            text=h.text,
            page=h.page,
            score=h.score,
            source=h.source,
        )
        for h in hits
    ]
