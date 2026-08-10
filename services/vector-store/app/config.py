"""Env config for vector-store. Mirrors the vars in the repo root .env.example."""

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    port: int = 8001
    embedding_model: str = "BAAI/bge-m3"
    chroma_persist_dir: str = "/data/chroma"
    chroma_collection: str = "clinical_kb"
    # How many candidates each of dense/BM25 contributes before RRF fusion trims to top_k.
    # See app/hybrid_search.py.
    candidate_pool_size: int = 25


settings = Settings()
