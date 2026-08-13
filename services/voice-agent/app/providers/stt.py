"""STT provider selection (plan §2.2: dual mode, config toggle).

"groq" uses livekit-plugins-groq directly (Whisper Large V3 over Groq's OpenAI-compatible
API -- no custom code needed, the plugin already implements livekit-agents' `stt.STT`
interface). "local" has no off-the-shelf livekit plugin for faster-whisper, so
LocalWhisperSTT below implements the same interface by hand, following the same
`_recognize_impl` shape used by livekit-plugins-openai/groq (see that plugin's
services.py for the reference pattern this was modeled on).
"""

import asyncio
import io
from functools import lru_cache

from livekit import rtc
from livekit.agents import APIConnectOptions, stt
from livekit.agents.types import NOT_GIVEN, NotGivenOr
from livekit.agents.utils.audio import AudioBuffer
from livekit.plugins import groq

from app.config import settings


def get_stt() -> stt.STT:
    if settings.stt_mode == "groq":
        return groq.STT(api_key=settings.groq_api_key, language="es")
    if settings.stt_mode == "local":
        return LocalWhisperSTT(model_size=settings.local_whisper_model)
    raise ValueError(f"unknown STT_MODE: {settings.stt_mode!r}")


def warm_up(model_size: str) -> None:
    """Force faster-whisper's model into memory ahead of the first real call. Only
    meaningful when STT_MODE=local -- called from app/main.py's prewarm_fnc."""
    _load_whisper_model(model_size)


@lru_cache(maxsize=None)
def _load_whisper_model(model_size: str):
    """Module-level cache, deliberately NOT an instance attribute on LocalWhisperSTT:
    app/main.py's entrypoint() previously constructed a fresh LocalWhisperSTT (and thus a
    fresh WhisperModel, the actual multi-hundred-MB asset) on every single call, since
    get_stt() runs inside entrypoint(), not prewarm_fnc. Caching by model_size here means
    every LocalWhisperSTT instance in this worker process, across every call it handles,
    shares the same loaded model."""
    from faster_whisper import WhisperModel

    device, compute_type = _pick_device()
    return WhisperModel(model_size, device=device, compute_type=compute_type)


def _pick_device() -> tuple[str, str]:
    try:
        import torch

        if torch.cuda.is_available():
            return "cuda", "float16"
    except ImportError:
        pass
    return "cpu", "int8"


class LocalWhisperSTT(stt.STT):
    """faster-whisper (CTranslate2), in-process -- see plan §2.2/§2.5 for why this runs
    in-process rather than as a separate service (latency) and how it picks CPU/CUDA
    (Metal is not exposed to CTranslate2; on macOS this always runs on CPU regardless of
    the --native-agent hardware mode -- call this out explicitly in the README rather than
    silently eating the latency hit).
    """

    def __init__(self, *, model_size: str = "small"):
        super().__init__(capabilities=stt.STTCapabilities(streaming=False, interim_results=False))
        self._model_size = model_size

    def _get_model(self):
        return _load_whisper_model(self._model_size)

    async def _recognize_impl(
        self,
        buffer: AudioBuffer,
        *,
        language: NotGivenOr[str] = NOT_GIVEN,
        conn_options: APIConnectOptions,
    ) -> stt.SpeechEvent:
        wav_bytes = rtc.combine_audio_frames(buffer).to_wav_bytes()

        def _transcribe() -> str:
            model = self._get_model()
            segments, _info = model.transcribe(io.BytesIO(wav_bytes), language="es")
            return " ".join(seg.text.strip() for seg in segments)

        text = await asyncio.to_thread(_transcribe)

        return stt.SpeechEvent(
            type=stt.SpeechEventType.FINAL_TRANSCRIPT,
            alternatives=[stt.SpeechData(language="es", text=text)],
        )
