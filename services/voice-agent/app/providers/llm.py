"""LLM provider selection (plan §2.1: "should be able to handle multiple models and
vendors, and switch between them as needed").

We deliberately do NOT wrap livekit-agents' own `llm.LLM` in a second, redundant
interface -- `llm.LLM` (and every plugin that implements it: openai.LLM, google.LLM,
groq.LLM, ...) already *is* the swap point AgentSession expects, so adding our own layer
on top would just be indirection with no new capability. `get_llm()` is the single place
that reads LLM_PROVIDER and returns a concrete, conforming instance; that's the whole
abstraction.

Default: Phi-3.5-mini via Ollama's OpenAI-compatible API (`openai.LLM.with_ollama`) --
this is the model declared for G3 (specs/implementation-plan.md §0, §2.1). The other
branches are stubs, not fully wired -- they exist so "switch providers" is a config change
plus finishing one branch, not a redesign, matching the family list in
ParticipantArtifacts/docs/stack-tecnico.md#1 (Gemini Flash, Llama via Groq are both
allowed alternatives).
"""

from livekit.agents import llm
from livekit.plugins import openai

from app.config import settings


def get_llm() -> llm.LLM:
    if settings.llm_provider == "ollama":
        return openai.LLM.with_ollama(
            model=settings.ollama_model,
            base_url=f"{settings.ollama_host}/v1",
        )

    if settings.llm_provider == "gemini":
        # TODO(workstream C): wire up livekit.plugins.google.LLM against Google AI
        # Studio's free tier if a cloud fallback is ever needed (stack-tecnico.md §2).
        raise NotImplementedError("gemini provider not wired up yet")

    if settings.llm_provider == "groq":
        # TODO(workstream C): livekit.plugins.groq.LLM against Groq's free Llama tier.
        raise NotImplementedError("groq provider not wired up yet")

    raise ValueError(f"unknown LLM_PROVIDER: {settings.llm_provider!r}")
