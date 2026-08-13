"""Strips <think>...</think> reasoning out of the LLM stream before it reaches TTS.

Phi-3.5-mini isn't a native chain-of-thought model, but prompting it to reason briefly
before answering (see app/prompts.py) measurably helps small-model clinical judgment.
That reasoning must never be spoken to the patient -- this module is the enforcement
point, sitting between the LLM's token stream and TTS via Agent.llm_node (app/main.py).

Latency tradeoff, stated plainly: we can't know whether a response will open with
<think> until we've received enough characters to tell. Once we know it's reasoning, we
must keep buffering (not yielding to TTS) until </think> shows up, so audio for that turn
starts later than it would otherwise. This is why app/prompts.py caps reasoning at "una
frase corta" -- it bounds how long that buffering can last. MAX_BUFFER_CHARS below is the
hard safety net for a malformed generation that opens <think> and never closes it: past
that many characters we give up waiting and flush the raw buffer rather than stay silent
forever (reasoning leaking through in that failure case is the lesser risk).
"""

from collections.abc import AsyncIterable, AsyncIterator

REASONING_OPEN = "<think>"
REASONING_CLOSE = "</think>"
MAX_BUFFER_CHARS = 400


def chunk_text(chunk: object) -> str | None:
    """Extracts text from whatever `Agent.default.llm_node` yields (str | ChatChunk |
    FlushSentinel). Returns None for non-text chunks (e.g. FlushSentinel) so the caller
    can pass those through untouched."""
    if isinstance(chunk, str):
        return chunk
    delta = getattr(chunk, "delta", None)
    content = getattr(delta, "content", None) if delta is not None else None
    return content if isinstance(content, str) else None


async def strip_reasoning(source: AsyncIterable) -> AsyncIterator[str | object]:
    """Consumes the raw LLM chunk stream, yields only the post-</think> spoken text (as
    plain str) plus any non-text chunks (FlushSentinel, tool calls) passed through as-is.
    If the response never opens with <think>, this adds no meaningful delay -- the first
    few characters decide that almost immediately and everything streams through live.
    """
    buffer = ""
    state = "deciding"  # "deciding" -> "reasoning" | "passthrough"

    async for chunk in source:
        text = chunk_text(chunk)
        if text is None:
            yield chunk  # non-text control chunk, e.g. FlushSentinel -- always pass through
            continue

        if state == "passthrough":
            yield text
            continue

        buffer += text

        # Re-evaluate the buffer after every state change (not just after every input
        # chunk) -- open and close tags can both land in the same chunk, e.g. when a
        # provider doesn't stream token-by-token.
        made_progress = True
        while made_progress:
            made_progress = False

            if state == "deciding":
                stripped = buffer.lstrip()
                if len(stripped) < len(REASONING_OPEN) and REASONING_OPEN.startswith(stripped):
                    break  # not enough characters yet to know either way
                if stripped.startswith(REASONING_OPEN):
                    state = "reasoning"
                    buffer = stripped[len(REASONING_OPEN):]
                else:
                    state = "passthrough"
                    out, buffer = buffer, ""
                    yield out
                    break
                made_progress = True

            elif state == "reasoning":
                if REASONING_CLOSE in buffer:
                    _, _, remainder = buffer.partition(REASONING_CLOSE)
                    state = "passthrough"
                    buffer = ""
                    if remainder:
                        yield remainder
                    break
                if len(buffer) > MAX_BUFFER_CHARS:
                    state = "passthrough"
                    out, buffer = buffer, ""
                    yield out
                    break

    if state != "passthrough" and buffer:
        # Stream ended mid-buffer (e.g. cut off by an interruption) -- flush whatever we
        # have rather than silently drop the tail of a reply.
        yield buffer
