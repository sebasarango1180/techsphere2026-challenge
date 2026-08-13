"""Pydantic models mirroring docs/openapi/vector-store.yaml -- keep the two in sync."""

from typing import Literal, Optional

from pydantic import BaseModel, Field


class IngestResult(BaseModel):
    chunk_count: int
    status: Literal["processing", "ready", "failed"]


class DocumentStatus(BaseModel):
    status: Literal["processing", "ready", "failed", "superseded", "deleted"]
    chunk_count: int


class SearchRequest(BaseModel):
    query: str
    top_k: int = 8
    # Soft ranking boost, NOT a hard filter -- see app/hybrid_search.py's module
    # docstring for why (docs/dataset-eda.md found the corpus's own category labels
    # aren't fully trustworthy, and patients can't reliably self-classify anyway).
    category_hint: Optional[str] = Field(default=None, description='e.g. "cholecystitis" -- nudges ranking, does not exclude other categories')


class SearchHit(BaseModel):
    chunk_id: str
    document_id: str
    version: int
    text: str
    page: Optional[int] = None
    score: float
    source: Literal["dense", "bm25", "both"]
