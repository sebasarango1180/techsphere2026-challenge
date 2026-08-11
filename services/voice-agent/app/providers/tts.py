"""Kokoro-82M TTS wrapper, in-process (plan §1/§2.5 -- TTS is latency-critical and runs
in the same process as the rest of the pipeline rather than as a separate service hop).

There's no official livekit-plugins-kokoro package (checked PyPI directly -- doesn't
exist as of writing), so this implements livekit-agents' `tts.TTS` /
`tts.ChunkedStream` interface by hand, modeled on livekit-plugins-openai's tts.py (see
that file for the reference shape this follows: `_run(output_emitter)` pushing raw audio
bytes as they become available).

Kokoro's `KPipeline.__call__` is a *synchronous, blocking* generator (one `Result` per
sentence/segment, each with `.output.audio` as a torch.FloatTensor at 24kHz) -- it's run
on a worker thread and bridged back into the async `_run` loop via a queue so audio starts
reaching the LiveKit track as each sentence finishes synthesizing, not after the whole
reply is done (this is what makes Track A's "start talking before the full response is
generated" design in plan §2.3 actually pay off end to end).

TODO(workstream C): `settings.tts_voice` ("ef_dora") is not yet verified against
hexgrad/Kokoro-82M's actual voice list -- confirm once the weights are downloaded and fix
if wrong; lang_code "e" (Spanish) per misaki's language table is correct, the specific
voice id within that language is the unverified part.
"""

import asyncio
import logging
import queue
from functools import lru_cache

import torch
from livekit.agents import APIConnectOptions, tts
from livekit.agents.types import DEFAULT_API_CONNECT_OPTIONS

from app.config import settings

logger = logging.getLogger(__name__)

SAMPLE_RATE = 24000
NUM_CHANNELS = 1

_SENTINEL = object()


def _pick_device() -> str:
    """Kokoro's own KPipeline.__init__ auto-selects a device with `'cuda' if
    torch.cuda.is_available() else 'cpu'` -- it has no concept of MPS at all, so on
    macOS (this service's native-mode host, specifically so Metal is available -- plan
    §2.5) it silently ran on CPU even when running natively for exactly that reason.
    Found by reading KPipeline's source, not assumed; verified `torch.backends.mps.
    is_available()` is True on this host. Override its default explicitly rather than
    relying on device=None.
    """
    if torch.cuda.is_available():
        return "cuda"
    if torch.backends.mps.is_available():
        return "mps"
    return "cpu"


@lru_cache(maxsize=1)
def _get_pipeline():
    from kokoro import KPipeline

    device = _pick_device()
    logger.info("Kokoro TTS pipeline using device=%s", device)
    return KPipeline(lang_code=settings.tts_lang_code, device=device)


def warm_up() -> None:
    """Force Kokoro's pipeline (weights + misaki g2p) into memory ahead of the first
    real call -- called from app/main.py's prewarm_fnc. `_get_pipeline`'s own
    `lru_cache` already means the model loads once per worker process rather than once
    per call; this just moves *when* that one load happens from "whichever call is
    first" to "before any call is dispatched to this process"."""
    _get_pipeline()


class TTS(tts.TTS):
    def __init__(self) -> None:
        super().__init__(
            capabilities=tts.TTSCapabilities(streaming=False),
            sample_rate=SAMPLE_RATE,
            num_channels=NUM_CHANNELS,
        )

    def synthesize(
        self, text: str, *, conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS
    ) -> "KokoroChunkedStream":
        return KokoroChunkedStream(tts=self, input_text=text, conn_options=conn_options)


class KokoroChunkedStream(tts.ChunkedStream):
    def __init__(self, *, tts: TTS, input_text: str, conn_options: APIConnectOptions) -> None:
        super().__init__(tts=tts, input_text=input_text, conn_options=conn_options)

    async def _run(self, output_emitter: tts.AudioEmitter) -> None:
        output_emitter.initialize(
            request_id=self.input_text[:16],
            sample_rate=SAMPLE_RATE,
            num_channels=NUM_CHANNELS,
            mime_type="audio/pcm",
        )

        loop = asyncio.get_running_loop()
        q: queue.Queue = queue.Queue()

        def _produce() -> None:
            try:
                pipeline = _get_pipeline()
                for result in pipeline(self.input_text, voice=settings.tts_voice):
                    pcm_bytes = _to_pcm16(result.output.audio)
                    q.put(pcm_bytes)
            finally:
                q.put(_SENTINEL)

        loop.run_in_executor(None, _produce)

        while True:
            chunk = await loop.run_in_executor(None, q.get)
            if chunk is _SENTINEL:
                break
            output_emitter.push(chunk)

        output_emitter.flush()


def _to_pcm16(audio_tensor) -> bytes:
    """torch.FloatTensor in [-1, 1] -> little-endian int16 PCM bytes."""
    clamped = audio_tensor.clamp(-1.0, 1.0)
    int16 = (clamped * 32767.0).to("cpu").to(dtype=torch.int16)
    return int16.numpy().tobytes()
