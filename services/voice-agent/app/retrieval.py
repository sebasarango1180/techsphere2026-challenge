"""HTTP client for vector-store's hybrid search (docs/openapi/vector-store.yaml,
specs/implementation-plan.md §4.2). This is the "retrieval" step in the pipeline
pseudocode at §4.3.
"""

from dataclasses import dataclass

import httpx

from app.config import settings


@dataclass
class RetrievedChunk:
    chunk_id: str
    document_id: str
    version: int
    text: str
    page: int | None
    score: float
    source: str


async def search(query: str, top_k: int = 8, category_hint: str | None = None) -> list[RetrievedChunk]:
    """category_hint is a soft ranking nudge, not a hard filter -- see
    vector-store's app/hybrid_search.py docstring / docs/dataset-eda.md §5,§7 for why:
    patients describe symptoms in lay language with no way to self-classify, and the
    corpus's own category labels aren't fully trustworthy (breast_cancer folder is
    verified to be all cervical-cancer content). There is no per-patient knowledge base
    "assignment" in this design -- every call searches the same current, versioned
    corpus.
    """
    # 10s wasn't enough headroom under real load -- found live, a search call timed out
    # mid-call while vector-store's BGE-M3 and this process's own Kokoro TTS pipeline
    # were both competing for the same Metal GPU (see app/main.py's
    # on_user_turn_completed for the graceful-degradation side of this same finding).
    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.post(
            f"{settings.vector_store_url}/v1/search",
            json={"query": query, "top_k": top_k, "category_hint": category_hint},
        )
        resp.raise_for_status()
        return [RetrievedChunk(**hit) for hit in resp.json()]


def format_for_prompt(chunks: list[RetrievedChunk]) -> str:
    """Renders retrieved chunks as a citation-labeled context block for the system/user
    prompt -- the [chunk_id] tags are what let a later step map a claim in the model's
    reply back to a specific document/page for the traceability rubric criterion
    (plan §2.3, §0)."""
    if not chunks:
        return "(no se encontro informacion relevante en la base de conocimiento)"
    lines = []
    for c in chunks:
        lines.append(f"[{c.chunk_id}] (pagina {c.page}): {c.text}")
    return "\n\n".join(lines)
