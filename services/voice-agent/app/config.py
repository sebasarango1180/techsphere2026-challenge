"""Env config for voice-agent. Mirrors the vars in the repo root .env.example."""

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # LiveKit
    livekit_url: str = "ws://livekit:7880"
    livekit_api_key: str = "devkey"
    livekit_api_secret: str = "changeme_min_32_chars_______________"

    # LLM (plan §2.1 -- Ollama by default, behind a swappable provider)
    llm_provider: str = "ollama"
    ollama_host: str = "http://ollama:11434"
    ollama_model: str = "phi3.5:3.8b"

    # STT (plan §2.2 -- dual mode)
    stt_mode: str = "groq"  # "groq" | "local"
    groq_api_key: str = ""
    local_whisper_model: str = "small"  # faster-whisper model size

    # TTS
    tts_model: str = "hexgrad/Kokoro-82M"
    # Verified against the real repo (huggingface.co/hexgrad/Kokoro-82M/tree/main/voices):
    # the only Spanish ("e" lang code) voices are ef_dora (female), em_alex/em_santa
    # (male). This default was always correct -- .env had TTS_VOICE=es (a plausible
    # looking but wrong guess), which 404s fetching voices/es.pt and silently kills
    # every TTS call. Found live: a call with no greeting and no replies at all.
    tts_voice: str = "ef_dora"
    tts_lang_code: str = "e"

    # Knowledge
    vector_store_url: str = "http://vector-store:8001"

    # Persistence
    database_url: str = ""

    class Config:
        env_file = ".env"


settings = Settings()
