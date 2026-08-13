"""Hybrid retrieval: dense (BGE-M3 via ChromaDB) + sparse (BM25) fused with Reciprocal
Rank Fusion. Plan §2.4: RRF is a tuning-free, well-understood default for a two-source
fusion -- no need for a learned re-ranker at this scale.

`category_hint` is a SOFT boost on the fused score, not a hard Chroma `where` filter --
deliberately, after docs/dataset-eda.md surfaced two reasons a hard filter is actively
dangerous here: (1) the challenge's own framing is that patients describe symptoms in
"lenguaje cotidiano, ambiguo y regional" with no medical vocabulary, so there's no
reliable way for a patient to self-classify into the "right" category during a call, and
(2) the corpus's own category labels aren't fully trustworthy -- the `breast_cancer`
folder is verified (dataset-eda.md §5) to contain zero mastectomy-relevant material, all
cervical-cancer content instead. A hard filter on an untrustworthy label, for a patient
who can't verify it either, is a way to confidently retrieve the wrong document while
looking correctly scoped. There is no per-patient knowledge base "assignment" in this
design at all -- every call draws from the same current, versioned corpus; category is
only ever a tie-breaking nudge toward it.

TODO(workstream B): the BM25 index is rebuilt from scratch on every search() call by
pulling the full active corpus out of Chroma. Fine for the size of the given corpus
(~100 PDFs); revisit (cache + incremental update on ingest/delete) if that becomes a
measurable chunk of retrieval latency once real timing data exists.
"""

import logging
from dataclasses import dataclass

from rank_bm25 import BM25Okapi

from app.config import settings
from app.embeddings import embed_texts
from app.store import combine_where, get_collection

logger = logging.getLogger(__name__)

RRF_K = 60  # standard RRF smoothing constant
CATEGORY_BOOST = 1.15  # +15% score nudge for a category_hint match -- tie-breaker
# strength, not a dominant factor; a document that's a much better semantic/lexical
# match still wins even without a matching category.


@dataclass
class Hit:
    chunk_id: str
    document_id: str
    version: int
    text: str
    page: int | None
    score: float
    source: str  # "dense" | "bm25" | "both"


def _tokenize(text: str) -> list[str]:
    # Naive whitespace/lowercase tokenization. TODO(workstream B): consider a
    # Spanish-aware tokenizer/stemmer (the corpus and patient speech are Spanish with
    # regional variation) if BM25 recall looks weak in practice.
    return text.lower().split()


def search(query: str, top_k: int = 8, category_hint: str | None = None) -> list[Hit]:
    collection = get_collection()
    # status=active is the only hard filter -- see module docstring for why category is
    # NOT in here.
    where = combine_where({"status": "active"})

    dense_ids, dense_meta, dense_docs = _dense_candidates(collection, query, where)
    bm25_ids, bm25_meta, bm25_docs = _bm25_candidates(collection, query, where)

    meta_by_id = {**dict(zip(bm25_ids, bm25_meta)), **dict(zip(dense_ids, dense_meta))}
    doc_by_id = {**dict(zip(bm25_ids, bm25_docs)), **dict(zip(dense_ids, dense_docs))}

    rrf_scores: dict[str, float] = {}
    sources: dict[str, set[str]] = {}
    for rank, chunk_id in enumerate(dense_ids, start=1):
        rrf_scores[chunk_id] = rrf_scores.get(chunk_id, 0.0) + 1.0 / (RRF_K + rank)
        sources.setdefault(chunk_id, set()).add("dense")
    for rank, chunk_id in enumerate(bm25_ids, start=1):
        rrf_scores[chunk_id] = rrf_scores.get(chunk_id, 0.0) + 1.0 / (RRF_K + rank)
        sources.setdefault(chunk_id, set()).add("bm25")

    if category_hint:
        for chunk_id in list(rrf_scores):
            if meta_by_id[chunk_id].get("category") == category_hint:
                rrf_scores[chunk_id] *= CATEGORY_BOOST

    ranked = sorted(rrf_scores.items(), key=lambda kv: kv[1], reverse=True)[:top_k]

    hits = []
    for chunk_id, score in ranked:
        meta = meta_by_id[chunk_id]
        src = sources[chunk_id]
        source = "both" if len(src) == 2 else next(iter(src))
        hits.append(
            Hit(
                chunk_id=chunk_id,
                document_id=meta["document_id"],
                version=meta["version"],
                text=doc_by_id[chunk_id],
                page=meta.get("page"),
                score=score,
                source=source,
            )
        )
    return hits


def _dense_candidates(collection, query: str, where: dict):
    query_embedding = embed_texts([query])[0]
    result = collection.query(
        query_embeddings=[query_embedding],
        n_results=settings.candidate_pool_size,
        where=where,
        include=["documents", "metadatas"],
    )
    return result["ids"][0], result["metadatas"][0], result["documents"][0]


def _bm25_candidates(collection, query: str, where: dict):
    corpus = collection.get(where=where, include=["documents", "metadatas"])
    ids, docs, metas = corpus["ids"], corpus["documents"], corpus["metadatas"]
    if not ids:
        return [], [], []

    bm25 = BM25Okapi([_tokenize(d) for d in docs])
    scores = bm25.get_scores(_tokenize(query))
    ranked_idx = sorted(range(len(ids)), key=lambda i: scores[i], reverse=True)[: settings.candidate_pool_size]
    return [ids[i] for i in ranked_idx], [metas[i] for i in ranked_idx], [docs[i] for i in ranked_idx]
