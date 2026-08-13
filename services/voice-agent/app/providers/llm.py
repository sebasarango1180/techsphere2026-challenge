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
        # Constructed directly rather than via with_ollama() so extra_body can carry a
        # hard token cap -- found live: a small model can, under the right (or wrong)
        # combination of context and prompt confusion, produce a multi-minute, multi-
        # paragraph monologue (observed for real: an off-script hip-replacement exercise
        # glossary and questions about sexual activity, spoken to a patient with no
        # known category at all), directly violating SYSTEM_PROMPT_ES's very first rule
        # ("respuestas de 1-2 frases: esto es una llamada, no un chat"). Prompt rules
        # alone aren't a reliable enough backstop against that -- this is a mechanical
        # limit that makes it structurally impossible regardless of cause. Verified
        # live against this exact Ollama instance that "max_tokens" (not the OpenAI v1
        # SDK's own "max_completion_tokens", which Ollama's compat layer silently
        # ignores) is the field that actually truncates generation.
        #
        # 250, not tighter: SYSTEM_PROMPT_ES's own <think> convention (reasoning briefly
        # before answering, see that file's module docstring) needs real room to use the
        # patient's name/age/comorbidities and conversation-so-far before producing the
        # 1-2 sentence spoken reply -- a cap so tight it starves that reasoning step
        # would trade one failure mode (runaway monologues) for another (worse clinical
        # judgment). 250 is still far below what the original incident produced (a full
        # glossary + multiple paragraphs), just not so tight it fights the model's own
        # deliberate reasoning step.
        return openai.LLM(
            model=settings.ollama_model,
            api_key="ollama",
            base_url=f"{settings.ollama_host}/v1",
            extra_body={"max_tokens": 250},
        )

    if settings.llm_provider == "gemini":
        # TODO(workstream C): wire up livekit.plugins.google.LLM against Google AI
        # Studio's free tier if a cloud fallback is ever needed (stack-tecnico.md §2).
        raise NotImplementedError("gemini provider not wired up yet")

    if settings.llm_provider == "groq":
        # TODO(workstream C): livekit.plugins.groq.LLM against Groq's free Llama tier.
        raise NotImplementedError("groq provider not wired up yet")

    raise ValueError(f"unknown LLM_PROVIDER: {settings.llm_provider!r}")
