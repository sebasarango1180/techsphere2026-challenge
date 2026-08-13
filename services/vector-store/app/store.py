"""ChromaDB wrapper: identity (document_id) comes from api-gateway/Postgres, this module
only owns chunk storage/versioning/soft-delete inside the `clinical_kb` collection (plan
§2.4, §3.2). Embeddings are computed by app/embeddings.py and passed in explicitly --
Chroma's own embedding-function hook is intentionally unused so BGE-M3 stays the single
source of vectors.
"""

import logging
import threading
from functools import lru_cache

import chromadb

from app.chunking import Chunk
from app.config import settings
from app.embeddings import embed_texts

logger = logging.getLogger(__name__)

# Found live, the hard way, running a real bulk-load under concurrency: ChromaDB's
# PersistentClient (Rust-backed, embedded in-process) was not safe to call from multiple
# threads at once -- `/v1/ingest`'s asyncio.to_thread lets several requests reach this
# module concurrently, and that produced real, silent-looking failures --
# `AttributeError: 'RustBindingsAPI' object has no attribute 'bindings'` deep inside
# chromadb's own client code, and `ValueError: Could not connect to tenant
# default_tenant` -- both symptoms of the same underlying client object being torn by
# concurrent access, not a data problem with any particular document. `get_collection`'s
# `lru_cache` only makes the *cached object* thread-safe to store/retrieve; it does
# nothing to make concurrent `.get()`/`.add()`/`.update()` calls on that object safe.
# Since then, Chroma moved from an embedded library to its own server (HttpClient, see
# get_collection below and docker-compose.yml's top comment) -- this lock is kept as the
# safe default since HttpClient's own thread-safety under this exact concurrency
# pattern hasn't been separately re-verified, not because the original Rust-binding
# failure is known to still apply over HTTP. OCR/PDF extraction (the genuinely parallel,
# CPU-bound part) still overlaps across threads either way; only the Chroma read/write
# tail of each request is forced one-at-a-time.
_chroma_lock = threading.Lock()


def combine_where(conditions: dict) -> dict:
    """Chroma's `where` requires exactly one top-level operator -- a bare multi-key dict
    like {"a": 1, "b": 2} raises ValueError as of the chromadb version pinned here. Wrap
    multi-condition filters in $and; pass single-condition ones through unchanged."""
    if not conditions:
        return {}
    if len(conditions) == 1:
        return dict(conditions)
    return {"$and": [{k: v} for k, v in conditions.items()]}


@lru_cache(maxsize=1)
def get_collection():
    client = chromadb.HttpClient(host=settings.chroma_host, port=settings.chroma_port)
    return client.get_or_create_collection(name=settings.chroma_collection)


def upsert_chunks(document_id: str, version: int, category: str, lang: str, chunks: list[Chunk]) -> int:
    """Embeds and stores `chunks` as the new active version, superseding any prior active
    version's chunks for this document_id (plan §2.4 versioning rule)."""
    with _chroma_lock:
        collection = get_collection()
        _supersede_older_versions(collection, document_id, keep_version=version)

    if not chunks:
        return 0

    ids = [f"{document_id}:v{version}:{c.chunk_index}" for c in chunks]
    texts = [c.text for c in chunks]
    # Outside _chroma_lock on purpose -- embed_texts() holds its own lock
    # (app/embeddings.py's _encode_lock) and doesn't touch Chroma, so serializing it
    # behind THIS lock too would just block other requests' Chroma access for no reason
    # while this one is busy embedding.
    embeddings = embed_texts(texts)
    metadatas = [
        {
            "document_id": document_id,
            "version": version,
            "chunk_index": c.chunk_index,
            "category": category,
            "lang": lang,
            "page": c.page,
            "status": "active",
        }
        for c in chunks
    ]

    with _chroma_lock:
        collection.add(ids=ids, embeddings=embeddings, documents=texts, metadatas=metadatas)
    return len(chunks)


def _supersede_older_versions(collection, document_id: str, keep_version: int) -> None:
    existing = collection.get(where={"document_id": document_id}, include=["metadatas"])
    stale_ids = []
    stale_metadatas = []
    for chunk_id, metadata in zip(existing["ids"], existing["metadatas"]):
        if metadata.get("version") != keep_version and metadata.get("status") == "active":
            stale_ids.append(chunk_id)
            stale_metadatas.append({**metadata, "status": "superseded"})
    if stale_ids:
        collection.update(ids=stale_ids, metadatas=stale_metadatas)


def soft_delete_document(document_id: str) -> int:
    """Flips every chunk of every version to status=deleted, synchronously -- G5 is
    tested live, so a subsequent search() call must not see this document anymore."""
    with _chroma_lock:
        collection = get_collection()
        existing = collection.get(where={"document_id": document_id}, include=["metadatas"])
        ids = existing["ids"]
        if not ids:
            return 0
        metadatas = [{**m, "status": "deleted"} for m in existing["metadatas"]]
        collection.update(ids=ids, metadatas=metadatas)
        return len(ids)


def document_status(document_id: str) -> tuple[str, int]:
    """Best-effort status from Chroma's own state. api-gateway/Postgres is the real
    source of truth for the console's polled status (plan §4.1) -- this endpoint exists
    for debugging vector-store in isolation."""
    with _chroma_lock:
        collection = get_collection()
        active = collection.get(
            where=combine_where({"document_id": document_id, "status": "active"}), include=[]
        )
        if active["ids"]:
            return "ready", len(active["ids"])

        any_chunks = collection.get(where={"document_id": document_id}, include=["metadatas"])
    if not any_chunks["ids"]:
        return "processing", 0
    statuses = {m.get("status") for m in any_chunks["metadatas"]}
    if "deleted" in statuses:
        return "deleted", 0
    return "superseded", 0
