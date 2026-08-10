"""BGE-M3 dense embeddings -- the hard requirement from scaffolding.md/stack-tecnico.md.

The heavy import (FlagEmbedding -> transformers -> torch) is deferred into
`get_embedder()` rather than sitting at module load time, so importing this module (e.g.
for a quick health check or a unit test of chunking.py) doesn't force a multi-hundred-MB
download / GPU probe as a side effect. The model itself is lazily loaded on first use and
cached as a process-wide singleton -- loading BGE-M3 per request would blow any latency
budget.
"""

import logging
import threading
from functools import lru_cache

from app.config import settings

logger = logging.getLogger(__name__)

# Verified live, not assumed: running embed_texts() from multiple threads at once (which
# /v1/ingest's asyncio.to_thread now makes possible) doesn't parallelize -- it makes EVERY
# call dramatically slower. Measured on the actual container: 4 concurrent encode() calls
# each took ~5.05s (5.06s total wall time); the same 4 calls run one after another took
# 1.49s total (~0.37s each) -- concurrency here was ~13x slower per call, not faster, almost
# certainly CPU oversubscription (torch's intra-op threading already uses every core for a
# single encode() call, so N concurrent calls just contend for the same cores instead of
# adding throughput). This lock serializes just the embedding step; OCR/PDF extraction
# (tesseract subprocesses, genuinely OS-parallel) still overlaps across concurrent requests.
_encode_lock = threading.Lock()


@lru_cache(maxsize=1)
def get_embedder():
    """Returns a loaded BGEM3FlagModel, hardware-aware (CUDA/MPS/CPU -- plan §2.5).

    TODO(workstream B): confirm memory behavior under concurrent requests -- FlagEmbedding's
    encode() is not obviously thread-safe; a request queue / lock may be needed once this
    is under real concurrent load (plan's "low latency under multi-user concurrency"
    requirement).
    """
    from FlagEmbedding import BGEM3FlagModel  # noqa: PLC0415 -- deferred, see module docstring

    use_fp16 = _has_cuda()
    logger.info("loading embedding model %s (fp16=%s)", settings.embedding_model, use_fp16)
    return BGEM3FlagModel(settings.embedding_model, use_fp16=use_fp16)


def _has_cuda() -> bool:
    try:
        import torch

        return torch.cuda.is_available()
    except ImportError:
        return False


def embed_texts(texts: list[str]) -> list[list[float]]:
    """Dense embeddings only (return_dense=True) -- BGE-M3 can also produce sparse/ColBERT
    vectors, but the sparse side of our hybrid search is a separate plain-BM25 index
    (app/hybrid_search.py) per plan §2.4, not BGE-M3's own sparse output. Revisit if BM25
    turns out to underperform BGE-M3's learned sparse vectors on this corpus.
    """
    model = get_embedder()
    with _encode_lock:
        output = model.encode(texts, return_dense=True, return_sparse=False, return_colbert_vecs=False)
    return output["dense_vecs"].tolist()
