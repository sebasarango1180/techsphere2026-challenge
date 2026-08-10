"""voice-agent entrypoint: joins a LiveKit room as a participant and runs the pipeline
described in specs/implementation-plan.md §4.3:

    VAD end-of-speech
      -> STT(audio) -> text                                   [Track: STT]
      -> POST vector-store/search(text)                       [retrieval]
      -> build prompt (system + citations + history window + running clinical snapshot)
      -> Ollama generate, streamed                             [Track A: spoken reply -> TTS per-sentence]
      -> Ollama classify, JSON-mode                             [Track B: triage + clinical signals]
      -> deterministic red-flag check(text)                     [rule layer]
      -> final_triage = max(Track B, rule layer)
      -> merge signals into running snapshot, persist turn + snapshot + escalation (if any)
      -> on call end: summarize -> finalize call_summaries

Track A (the spoken reply) is handled automatically by AgentSession once wired with our
STT/LLM/TTS/VAD providers -- that's the whole point of building on livekit-agents (plan
§1). Everything specific to this agent lives in `PostSurgicalAgent.on_user_turn_completed`
(retrieval + injecting the running clinical snapshot + kicking off Track B) and the
session event handlers below (persistence).

The agent's job, stated plainly (this is what on_user_turn_completed/_track_b/decision.py
are actually for): lead a routine post-op check-in conversation, and PRIVATELY infer
whether medical staff should be notified -- the triage classification is never spoken to
the patient (see app/prompts.py's docstring), it drives app/decision.py's escalation
logic and the call summary instead.

TODO(workstream C): this file is unverified against a live LiveKit room + running Ollama
-- it's built from livekit-agents' actual installed API (verified via introspection, not
guessed), but integration bugs are still likely on first real run. Prioritize testing:
1) does on_user_turn_completed's injected context actually reach the LLM call, 2) do the
ChatMessage.metrics fields populate as documented, 3) the room-metadata parsing in
app/call_context.py against a real api-gateway-created room.
"""

import asyncio
import json
import logging

import httpx
from livekit import agents
from livekit.agents import (
    Agent,
    AgentSession,
    JobContext,
    JobProcess,
    ModelSettings,
    WorkerOptions,
    cli,
)
from livekit.agents.llm import ChatContext, ChatMessage
from livekit.plugins import silero
from livekit.plugins.turn_detector.multilingual import MultilingualModel

from app import call_context, db, decision, retrieval
from app.clinical_snapshot import ClinicalSnapshot
from app.config import settings
from app.prompts import CLASSIFICATION_PROMPT_ES, build_context_prompt, build_instructions
from app.providers.llm import get_llm
from app.providers.stt import get_stt
from app.providers.stt import warm_up as stt_warm_up
from app.providers.tts import TTS
from app.providers.tts import warm_up as tts_warm_up
from app.reasoning import strip_reasoning

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class PostSurgicalAgent(Agent):
    def __init__(self, *, call_ctx: call_context.CallContext, instructions: str) -> None:
        super().__init__(instructions=instructions)
        self.call_ctx = call_ctx
        self.snapshot = ClinicalSnapshot()
        self._background_tasks: set[asyncio.Task] = set()

    async def on_user_turn_completed(self, turn_ctx: ChatContext, new_message: ChatMessage) -> None:
        """Called after STT finalizes the patient's turn, before the LLM generates a
        reply -- this is where retrieval happens and gets injected into context (plan
        §4.3's "retrieval" + "build prompt" steps)."""
        patient_text = new_message.text_content or ""
        if not patient_text.strip():
            return

        chunks = await retrieval.search(patient_text, category_hint=self.call_ctx.category)
        context_block = build_context_prompt(retrieval.format_for_prompt(chunks), category=self.call_ctx.category)
        turn_ctx.add_message(role="system", content=context_block)

        # "Context at any point in time": the agent's own running read of this call so
        # far, so it doesn't re-ask something already established earlier in the SAME
        # call (plan §2.10). Only added once there's something to say.
        if self.snapshot.has_any():
            turn_ctx.add_message(
                role="system",
                content=f"Lo que ya sabes de esta llamada: {self.snapshot.render_es()}.",
            )

        # Track B (classification) + rule layer + fusion + persistence run in the
        # background so they never add latency to Track A's spoken reply (plan §2.3).
        task = asyncio.create_task(self._track_b(patient_text, chunks))
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def llm_node(self, chat_ctx: ChatContext, tools: list, model_settings: ModelSettings):
        """Overrides the default llm_node purely to pipe its output through
        strip_reasoning (app/reasoning.py) -- the <think> block our prompt asks for must
        never reach TTS. See that module's docstring for the buffering/latency tradeoff.
        """
        default_stream = Agent.default.llm_node(self, chat_ctx, tools, model_settings)
        async for piece in strip_reasoning(default_stream):
            yield piece

    async def _track_b(self, patient_text: str, chunks: list[retrieval.RetrievedChunk]) -> None:
        try:
            classification = await classify_triage(patient_text, retrieval.format_for_prompt(chunks))
        except Exception:
            logger.exception("Track B classification failed for call_id=%s", self.call_ctx.call_id)
            classification = {"triage": "verde", "confidence": 0.0}

        # Merge this turn's signals into the running picture and persist it -- every
        # turn, not just escalating ones, so the snapshot is complete "at any point in
        # time" (durable half in app/db.py, in-memory half is self.snapshot itself).
        self.snapshot.merge(classification)
        await db.upsert_clinical_snapshot(self.call_ctx.call_id, self.snapshot)

        rule_match = decision.rule_based_triage(patient_text, self.call_ctx.category)
        fused = decision.fuse(
            classification.get("triage", "verde"),
            f"clasificacion del modelo (confianza={classification.get('confidence', 0)})",
            rule_match,
        )

        if fused.level != "verde":
            await db.insert_escalation(
                call_id=self.call_ctx.call_id,
                level=fused.level,
                rationale=fused.rationale,
                triggered_by=fused.triggered_by,
                cited_documents=[
                    {"chunk_id": c.chunk_id, "document_id": c.document_id, "page": c.page}
                    for c in chunks
                ],
            )
            logger.info(
                "escalation call_id=%s level=%s triggered_by=%s",
                self.call_ctx.call_id, fused.level, fused.triggered_by,
            )


async def classify_triage(patient_text: str, retrieved_context: str) -> dict:
    """Track B: a concise structured pass over the latest turn via Ollama's JSON mode.
    Deliberately a separate, minimal HTTP call rather than reusing the AgentSession's LLM
    plugin -- we just need one JSON completion, not the conversational streaming machinery.
    """
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(
            f"{settings.ollama_host}/api/chat",
            json={
                "model": settings.ollama_model,
                "messages": [
                    {"role": "system", "content": CLASSIFICATION_PROMPT_ES},
                    {
                        "role": "user",
                        "content": f"Contexto clinico:\n{retrieved_context}\n\nLo que dijo el paciente:\n{patient_text}",
                    },
                ],
                "format": "json",
                "stream": False,
            },
        )
        resp.raise_for_status()
        content = resp.json()["message"]["content"]
        return json.loads(content)


async def summarize_call(session: AgentSession, agent: PostSurgicalAgent) -> None:
    """On call end: generate the narrative fields the required call summary needs
    (symptoms_reported, decision, next_steps) -- the six structured signals are already
    persisted incrementally via upsert_clinical_snapshot, this only fills in the rest
    (plan §4.3's last step)."""
    call_id = agent.call_ctx.call_id
    transcript = "\n".join(f"{m.role}: {m.text_content}" for m in session.history.messages)
    if not transcript.strip():
        return

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(
                f"{settings.ollama_host}/api/chat",
                json={
                    "model": settings.ollama_model,
                    "messages": [
                        {
                            "role": "system",
                            "content": (
                                "Resume esta llamada de seguimiento post-quirurgico en JSON con "
                                'las claves: "symptoms_reported", "decision", "next_steps" '
                                "(todo en espanol, breve)."
                            ),
                        },
                        {"role": "user", "content": transcript},
                    ],
                    "format": "json",
                    "stream": False,
                },
            )
            resp.raise_for_status()
            summary = json.loads(resp.json()["message"]["content"])
    except Exception:
        logger.exception("call summary generation failed for call_id=%s", call_id)
        summary = {}

    await db.finalize_call_summary(
        call_id=call_id,
        procedure=agent.call_ctx.procedure or agent.call_ctx.category,
        symptoms_reported=summary.get("symptoms_reported"),
        decision=summary.get("decision"),
        references=None,  # TODO(workstream C): aggregate cited_documents across the call's escalations
        next_steps=summary.get("next_steps"),
    )
    await db.mark_call_completed(call_id)


def prewarm(proc: JobProcess) -> None:
    """Runs ONCE per worker process, before this process is handed any job (livekit-agents'
    official hook -- `WorkerOptions.prewarm_fnc`, confirmed via the installed package's
    ipc/job_proc_lazy_main.py: `_JobProc.initialize()` calls this synchronously, and it
    completes before the process's asyncio event loop is even created, let alone before
    any call is routed to it).

    VAD is genuinely prewarmable this way -- `silero.VAD.load()` has no dependency on an
    active job, confirmed by reading livekit-plugins-silero's source (no
    `get_job_context()` call anywhere in it). Stored in `proc.userdata` and picked up in
    `entrypoint()` via `ctx.proc.userdata`, since it's a per-call AgentSession constructor
    arg, not something a module-level cache can hold on its own.

    The turn-detector (`MultilingualModel`) is deliberately NOT constructed here, even
    though it looks like the same kind of asset -- verified live that it raises
    `RuntimeError: no job context found` when built outside a job entrypoint:
    `EOUModelBase.__init__` calls `get_job_context().inference_executor` when no explicit
    executor is passed, and that context/executor only exists once `entrypoint()` is
    running (its actual ONNX inference happens out-of-process via that executor, managed
    by livekit-agents itself). What CAN be prewarmed for it is the weight file download --
    handled by `python -m app.main download-files`, baked into the Docker image at build
    time (see Dockerfile) and run once by scripts/setup.sh in native mode -- so by the
    time `entrypoint()` constructs `MultilingualModel()` per call, that construction is
    just wiring against an already-local file, not a network fetch.

    Kokoro/faster-whisper/Ollama warm themselves via a module-level cache instead (see
    their respective `warm_up()`s and `app/providers/tts.py`'s `_get_pipeline`
    docstring) -- calling them here just moves *when* that one-time load happens, same as
    VAD, and Ollama's own first-inference load (the same class of bug already found and
    fixed for BGE-M3 in vector-store -- see that service's README) is handled the same way.
    """
    proc.userdata["vad"] = silero.VAD.load()
    if settings.stt_mode == "local":
        stt_warm_up(settings.local_whisper_model)
    tts_warm_up()
    _warm_up_ollama()


def _warm_up_ollama() -> None:
    """Ollama's documented load-only request shape: POST /api/generate with an empty
    prompt loads the model into memory and returns `done_reason: "load"` without
    generating anything -- verified live against a real local Ollama instance (pulled a
    small test model, timed the empty-prompt call, confirmed a second real-prompt call
    afterward reported near-zero `load_duration`). Synchronous on purpose: this runs
    inside prewarm_fnc, before this worker process has an event loop at all (see this
    function's caller's docstring), so an async client isn't an option here anyway.
    Non-fatal -- if Ollama isn't reachable yet, the first real call just pays the load
    cost itself, same as before this existed.
    """
    try:
        with httpx.Client(timeout=180.0) as client:
            resp = client.post(
                f"{settings.ollama_host}/api/generate",
                json={"model": settings.ollama_model, "prompt": ""},
            )
            resp.raise_for_status()
        logger.info("Ollama model %s warmed up", settings.ollama_model)
    except Exception:
        logger.exception(
            "Ollama warmup failed for model %s -- first real call will pay the load cost",
            settings.ollama_model,
        )


async def entrypoint(ctx: JobContext) -> None:
    await ctx.connect()

    call_ctx = call_context.from_room(ctx.room.name, ctx.room.metadata)

    prior_snapshot_es = None
    if call_ctx.patient_id:
        prior = await db.fetch_latest_snapshot_for_patient(call_ctx.patient_id, call_ctx.call_id)
        if prior:
            prior_snapshot_es = ClinicalSnapshot.from_db_row(prior).render_es()

    vad = ctx.proc.userdata.get("vad")
    if vad is None:
        # Should only happen if job_executor_type isn't PROCESS (prewarm's guarantees
        # don't hold under THREAD/inline execution) -- fall back rather than crash, but
        # loudly, since this call just paid a load cost prewarm exists to avoid.
        logger.warning("proc.userdata missing vad (prewarm_fnc may not have run) -- loading fresh")
        vad = silero.VAD.load()

    # NOT prewarmed, unlike vad above -- MultilingualModel() requires an active job
    # context (it wires itself to ctx's inference executor internally), so this must be
    # constructed here rather than in prewarm_fnc. See prewarm()'s docstring for what IS
    # done ahead of time for it (the weight file download).
    session = AgentSession(
        stt=get_stt(),
        llm=get_llm(),
        tts=TTS(),
        vad=vad,
        turn_detection=MultilingualModel(),
    )

    @session.on("conversation_item_added")
    def _on_item(event: agents.ConversationItemAddedEvent) -> None:
        item = event.item
        if not isinstance(item, ChatMessage):
            return
        role = "patient" if item.role == "user" else "agent"
        metrics = item.metrics or {}
        asyncio.create_task(
            db.insert_turn(
                call_id=call_ctx.call_id,
                role=role,
                text=item.text_content or "",
                stt_ms=_ms(metrics.get("transcription_delay")),
                retrieval_ms=_ms(metrics.get("on_user_turn_completed_delay")),
                llm_ms=_ms(metrics.get("llm_node_ttft")),
                tts_ms=_ms(metrics.get("tts_node_ttfb")),
            )
        )

    agent = PostSurgicalAgent(
        call_ctx=call_ctx,
        instructions=build_instructions(call_ctx, prior_snapshot_es),
    )

    await session.start(agent=agent, room=ctx.room)

    async def _on_shutdown() -> None:
        await summarize_call(session, agent)

    ctx.add_shutdown_callback(_on_shutdown)


def _ms(seconds: float | None) -> int | None:
    return int(seconds * 1000) if seconds is not None else None


if __name__ == "__main__":
    cli.run_app(
        WorkerOptions(
            entrypoint_fnc=entrypoint,
            prewarm_fnc=prewarm,
            ws_url=settings.livekit_url,
            api_key=settings.livekit_api_key,
            api_secret=settings.livekit_api_secret,
        )
    )
