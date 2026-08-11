"""Env config for vector-store. Mirrors the vars in the repo root .env.example."""

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    port: int = 8001
    embedding_model: str = "BAAI/bge-m3"
    # Chroma runs as its own server (official chromadb/chroma image), not embedded as a
    # local file-backed library -- it does no GPU/Metal work itself, so there's no
    # reason for it to follow vector-store natively (see docker-compose.yml's top
    # comment). "localhost" is native mode's default (Docker Desktop publishes the
    # container's port there); docker-compose.yml overrides both for Docker mode.
    chroma_host: str = "localhost"
    chroma_port: int = 8000
    chroma_collection: str = "clinical_kb"
    # How many candidates each of dense/BM25 contributes before RRF fusion trims to top_k.
    # See app/hybrid_search.py.
    candidate_pool_size: int = 25


settings = Settings()
