"""voice-agent entrypoint: joins a LiveKit room as a participant and runs the pipeline
described in specs/implementation-plan.md §4.3, REWORKED more than once against real
live-call evidence (see docs/decision-flow.md for the full history). Current shape:

    VAD end-of-speech
      -> STT(audio) -> text                                   [Track: STT]
      -> deterministic red-flag check(text)                     [rule layer, per turn, NOT an LLM call]
      -> if rule layer fires: escalate immediately
      -> KB search-decision gate (isolated LLM call, NOT the conversational one) --
           only if it judges the patient's utterance a genuine clinical question:
           vector-store search -> grounded context injected for THIS reply only
      -> build prompt (system + next scripted topic + KB context if the gate fired)
      -> Ollama generate, streamed                             [conversational reply -> TTS per-sentence]
      -> advance to the next scripted topic (turn counter, not LLM-dependent)
      -> on call end: ONE comprehensive LLM pass over the full transcript
           -> six clinical signals + triage classification + narrative summary
      -> THEN, separately: KB retrieval + pathology validation against the
           six-signal snapshot
      -> final_triage = max(end-of-call model classification, worst rule-layer match seen)
      -> persist snapshot + final triage + call summary + pathology validation

**KB retrieval mid-call is gated, not unconditional and not absent.** Two prior designs
both turned out wrong, live: (1) querying vector-store on EVERY patient turn to ground
the conversational reply backfired badly -- for a call with no known procedure category,
a generic scripted question (e.g. "movilidad") could score a confident but entirely
wrong-category match against the corpus (which spans ~5 very different surgical
categories), and the model would free-associate from that mismatched context into a
multi-minute off-script monologue. (2) Removing KB retrieval from the live call
entirely then failed the rubric's own RAG requirement (demonstrable, traceable,
citation-backed clinical answers when the patient asks something, with an honest "I
don't know" when the KB doesn't cover it -- not silence on the KB during the call at
all). The fix is a gate: `_decide_search` (app/prompts.py's SEARCH_DECISION_PROMPT_ES)
is a small, ISOLATED, non-streamed call -- the practical equivalent of tool-calling for
a model with no native tool support (confirmed live: Ollama reports phi3.5:3.8b's
capabilities as `["completion"]` only) -- that judges, per turn, whether the patient's
OWN utterance is a real clinical question. Only then does retrieval run, with the
model's own query, and only that one reply gets grounded context. The six scripted
topics never trigger it. `summarize_call`'s end-of-call pathology validation is a
SEPARATE, additional KB query, grounded against the clean six-signal snapshot.

Track A (the spoken reply) is handled automatically by AgentSession once wired with our
STT/LLM/TTS/VAD providers -- that's the whole point of building on livekit-agents (plan
§1). Everything specific to this agent lives in `PostSurgicalAgent.on_user_turn_completed`
(rule-layer safety net + KB search-decision gate + next-topic steering) and
`summarize_call` (the end-of-call classification + pathology validation pass).

The agent's job, stated plainly: lead a routine post-op check-in conversation, and
PRIVATELY infer whether medical staff should be notified -- the triage classification is
never spoken to the patient (see app/prompts.py's docstring), it drives
app/decision.py's escalation logic and the call summary instead.
"""

import asyncio
import json
import logging
import re

import httpx
from livekit import agents
from livekit.agents import (
    Agent,
    AgentSession,
    JobContext,
    JobProcess,
    ModelSettings,
    StopResponse,
    WorkerOptions,
    cli,
)
from livekit.agents.llm import ChatContext, ChatMessage
from livekit.plugins import silero
from livekit.plugins.turn_detector.multilingual import MultilingualModel

from app import call_context, db, decision, retrieval
from app.clinical_snapshot import QUESTION_ORDER, ClinicalSnapshot, topic_hint
from app.config import settings
from app.prompts import (
    FINAL_CLASSIFICATION_PROMPT_ES,
    GREETING_ES,
    PATHOLOGY_VALIDATION_PROMPT_ES,
    SEARCH_DECISION_PROMPT_ES,
    build_context_prompt,
    build_farewell,
    build_instructions,
)
from app.providers.llm import get_llm
from app.providers.stt import get_stt
from app.providers.stt import warm_up as stt_warm_up
from app.providers.tts import TTS
from app.providers.tts import warm_up as tts_warm_up
from app.reasoning import strip_reasoning

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class PostSurgicalAgent(Agent):
    def __init__(self, *, call_ctx: call_context.CallContext, instructions: str, job_ctx: JobContext) -> None:
        super().__init__(instructions=instructions)
        self.call_ctx = call_ctx
        self._job_ctx = job_ctx
        # Six-signal extraction now happens ONCE at call end (see module docstring) --
        # this snapshot is populated by summarize_call, not incrementally per turn.
        self.snapshot = ClinicalSnapshot()
        # Conversation flow: a plain turn counter through the fixed six-topic order
        # (docs/dataset-eda.md §2), advanced unconditionally after every patient turn or
        # silence timeout -- deliberately NOT dependent on any LLM call succeeding
        # (the old snapshot-driven version would silently stall on the same topic
        # whenever its per-turn classification call failed/timed out -- found live: the
        # agent repeating the same question, directly caused by this dependency).
        self.topic_index = 0
        # Set once every QUESTION_ORDER topic has been asked at least once (topic_index
        # has run past the end) -- does NOT by itself mean the call is closing. See
        # _prompt_next_topic's docstring: once script_done, we check whether the patient
        # actually gave usable answers before deciding to say goodbye, since the plain
        # turn-counter above can't tell "answered" from "STT caught a stray noise/word"
        # or "this turn was a clarification, not a new topic" -- found live, both look
        # identical from turn-counting alone and could silently skip a real topic.
        self.script_done = False
        # Guards the ONE makeup round (see _prompt_next_topic) against repeating --
        # bounded so an unclear/uncooperative patient answer can never keep the call
        # open indefinitely.
        self.makeup_attempted = False
        # Set once the farewell has been triggered -- guards on_user_turn_completed and
        # the user_state_changed handler against firing again afterward. Found live:
        # without this, the session stayed open after the farewell, and a stray
        # post-goodbye utterance (or even just the away-timeout re-firing) could trigger
        # another generate_reply -- and because the base system prompt describes the
        # full six-topic script, the model would sometimes start back through it instead
        # of just repeating the goodbye. See _prompt_next_topic.
        self.closing = False
        # Deterministic rule-layer's worst finding across the WHOLE call (not just the
        # latest turn) -- fed into the end-of-call fusion in summarize_call. A rule
        # match still escalates immediately when found (see on_user_turn_completed);
        # this is what lets the end-of-call classification also reflect it.
        self.worst_rule_match: decision.RuleMatch | None = None
        self._background_tasks: set[asyncio.Task] = set()
        # Chunks cited to ground the CURRENT/next spoken reply, set in
        # on_user_turn_completed only when a search-decision call (see
        # app.prompts.SEARCH_DECISION_PROMPT_ES) actually judged the patient's turn to
        # be a genuine clinical question worth looking up. Read and cleared by
        # entrypoint()'s conversation_item_added handler when it logs the agent's next
        # reply, so that turn's `retrieved_chunk_ids` reflects what actually grounded it
        # -- traceability for the rubric's "cada respuesta clinica puede rastrearse hasta
        # el documento" without going back to retrieving on every single turn.
        self.pending_citation_chunk_ids: list[str] = []

    async def on_enter(self) -> None:
        """Runs once, right when the agent joins the call (livekit-agents' official
        hook) -- this is where the mandatory greeting lives. Spoken verbatim via
        `session.say()`, NOT asked of the LLM as an instruction: a ~3.8B model told to
        "greet the patient like this" will paraphrase it, and this exact wording is a
        hard requirement, not a style guide. Waits for the greeting to finish playing
        (`wait_for_playout`) before triggering the first scripted question, so the two
        don't overlap -- "as soon as the greeting ends, start asking questions".
        """
        handle = self.session.say(GREETING_ES)
        await handle.wait_for_playout()
        await self._prompt_next_topic()

    async def _prompt_next_topic(self) -> None:
        """Triggers the LLM to naturally phrase the CURRENT scripted question
        (self.topic_index into QUESTION_ORDER), or -- once past the last one -- runs the
        one-time missing-topic makeup check before saying goodbye and ending the call.
        Callers are responsible for not invoking this again once self.closing is set
        (see the user_state_changed handler in entrypoint())."""
        if not self.script_done:
            topic = topic_hint(self.topic_index)
            if topic:
                try:
                    self.session.generate_reply(
                        instructions=f"Pregunta de forma natural y breve sobre: {topic}. Solo ese tema, no menciones los demas."
                    )
                except RuntimeError:
                    logger.warning("call_id=%s session already closed -- skipping topic prompt", self.call_ctx.call_id)
                return
            self.script_done = True

        # All six scripted topics have been asked at least once. Before closing, run the
        # SAME end-of-call extraction summarize_call would run anyway, but here, live,
        # purely to check whether the patient's answers actually covered all six signals
        # -- this is what "if the agent considers a question hasn't been answered, it
        # should be able to introduce it again before closing" needs: the turn-counter
        # above can advance without a real answer (STT noise, a clarification exchange
        # eating a slot), so it alone can't be trusted to know what's actually missing.
        # Bounded to ONE makeup round (self.makeup_attempted) so a still-unclear answer
        # can't keep the call open indefinitely -- whatever's left after that is simply
        # recorded as missing (summarize_call's own end-of-call pass runs again anyway,
        # on the complete final transcript, and is the actual source of truth persisted
        # to the DB; this is only for deciding whether to ask again live).
        if not self.makeup_attempted:
            self.makeup_attempted = True
            transcript = "\n".join(f"{m.role}: {m.text_content}" for m in self.session.history.messages())
            # _classify_full_call is a real LLM round-trip (observed live: 18+ seconds
            # under Ollama contention from other concurrent calls) -- the patient's
            # client can legitimately disconnect while this is in flight (they hear
            # nothing for a while and hang up themselves), which tears down the
            # session/job. Every self.session access below can then raise RuntimeError
            # ("no activity context found") -- found live, an unhandled version of
            # exactly this crashed the makeup round and, worse, silently ate the rest of
            # this function (including the eventual farewell/shutdown), which is why a
            # call that hit this race never got marked complete or classified at all.
            classification = await _classify_full_call(transcript, self.call_ctx.call_id)
            self.snapshot.merge(classification)
            missing = self.snapshot.missing_fields()
            if missing:
                logger.info("call_id=%s makeup round for missing: %s", self.call_ctx.call_id, missing)
                try:
                    self.session.generate_reply(
                        instructions=(
                            f"Pregunta de forma breve y natural sobre: {', '.join(missing)} -- un tema a la vez si son "
                            "varios. No expliques por que preguntas esto ni menciones que faltaba, simplemente hazlo "
                            "como continuidad natural de la conversacion."
                        )
                    )
                except RuntimeError:
                    logger.warning("call_id=%s session closed mid-classification (patient likely hung up) -- skipping makeup round, closing instead", self.call_ctx.call_id)
                else:
                    return

        # Ending the call HERE, ourselves, rather than leaving the session open waiting
        # on the patient to hang up, is what actually fixes the restart bug (see module
        # docstring) -- and as a side effect guarantees the end-of-call classification
        # (summarize_call, registered as a shutdown callback in entrypoint()) runs
        # promptly, instead of depending on however/whenever the patient's client
        # disconnects. ctx.shutdown() is safe to call even if the session/job is
        # already tearing down for some other reason (e.g. the patient hung up) --
        # confirmed via the installed package: a second shutdown request is a silently
        # suppressed no-op, not an error.
        self.closing = True
        try:
            handle = self.session.say(build_farewell(self.call_ctx.patient_name))
            await handle.wait_for_playout()
        except RuntimeError:
            logger.warning("call_id=%s session already closed -- skipping farewell", self.call_ctx.call_id)
        self._job_ctx.shutdown(reason="chequeo completo")

    async def on_user_turn_completed(self, turn_ctx: ChatContext, new_message: ChatMessage) -> None:
        """Called after STT finalizes the patient's turn, before the LLM generates a
        reply -- the deterministic real-time safety net, the KB search-decision gate,
        and next-topic steering all happen here (plan §4.3). The KB is queried ONLY when
        a separate, isolated decision call (SEARCH_DECISION_PROMPT_ES) judges the
        patient's own utterance to be a genuine clinical question -- never unconditionally
        on every turn (see module docstring for the incident that established this)."""
        if self.closing:
            # Already wrapping up (farewell in flight or spoken) -- a plain `return`
            # here would still let the framework auto-generate its normal per-turn
            # reply; StopResponse is what actually suppresses that (see
            # _prompt_next_topic's docstring for why letting any further reply happen
            # here was the root cause of the call restarting itself after goodbye).
            raise StopResponse()
        patient_text = new_message.text_content or ""
        if not patient_text.strip():
            return

        # Deterministic rule layer -- NOT an LLM call, cheap, runs every turn for
        # real-time safety regardless of when the model-based classification happens
        # (see module docstring). Escalates immediately on a match; also tracked as the
        # running worst-of-the-call for the end-of-call fusion (summarize_call).
        # cited_documents is empty here (no per-turn retrieval to cite from, by design)
        # -- the rule layer's match itself is deterministic text/threshold matching, not
        # KB-dependent, so this doesn't weaken the escalation's rationale.
        rule_match = decision.rule_based_triage(patient_text, self.call_ctx.category)
        if rule_match is not None:
            if self.worst_rule_match is None or decision.TRIAGE_ORDER[rule_match.level] > decision.TRIAGE_ORDER[self.worst_rule_match.level]:
                self.worst_rule_match = rule_match
            await db.insert_escalation(
                call_id=self.call_ctx.call_id,
                level=rule_match.level,
                rationale=rule_match.rationale,
                triggered_by="rule",
                cited_documents=[],
            )
            logger.info(
                "real-time rule-layer escalation call_id=%s level=%s",
                self.call_ctx.call_id, rule_match.level,
            )

        # KB search-decision gate: an isolated, non-streamed call judges whether the
        # patient's OWN utterance is a genuine clinical question (not just an answer to
        # the routine six-topic script) -- see SEARCH_DECISION_PROMPT_ES's docstring for
        # why this lives here, separate from the conversational reply generation. Only
        # when it decides yes does retrieval run at all; the query is the model's own
        # formulation, not the raw patient text.
        self.pending_citation_chunk_ids = []
        search_query = await _decide_search(patient_text, self.call_ctx.call_id)
        if search_query:
            try:
                chunks = await retrieval.search(search_query, category_hint=self.call_ctx.category)
            except Exception:
                logger.exception("retrieval.search failed for call_id=%s -- answering without KB grounding this turn", self.call_ctx.call_id)
                chunks = []
            context_block = build_context_prompt(retrieval.format_for_prompt(chunks), category=self.call_ctx.category)
            turn_ctx.add_message(role="system", content=context_block)
            # Restates the patient's own question explicitly rather than relying on the
            # model to correctly re-derive "what am I answering" from conversation
            # history -- found live: without this, the model sometimes read the LAST
            # system message (this instruction) as itself being the thing to respond to,
            # producing a confused "no question was provided" reply instead of actually
            # answering what the patient asked.
            turn_ctx.add_message(
                role="system",
                content=(
                    f'El paciente pregunto: "{patient_text}". Esta es una pregunta clinica '
                    "especifica. Responde SOLO usando el contexto de arriba, citando [chunk_id], "
                    "en 1-2 frases. Si el contexto no responde bien esa pregunta, dilo con "
                    "honestidad: 'no tengo esa informacion, lo reporto a tu equipo medico'. Luego "
                    "continua con el chequeo."
                ),
            )
            self.pending_citation_chunk_ids = [c.chunk_id for c in chunks]
            logger.info(
                "call_id=%s KB search triggered, query=%r, %d chunks",
                self.call_ctx.call_id, search_query, len(chunks),
            )

        # Steers the reply toward the fixed six-topic script (docs/dataset-eda.md §2),
        # then advances -- unconditionally, regardless of whether the patient's answer
        # seemed to address the topic (app/prompts.py's SYSTEM_PROMPT_ES asks the model
        # to make one genuine clarifying attempt within its own reply first; this
        # counter guarantees the call moves on either way, never gets stuck repeating).
        # Only runs before script_done -- once true, _prompt_next_topic's own makeup-
        # round / closing logic takes over (see below) and no further "next tema
        # pendiente" hint is needed here.
        #
        # Increment BEFORE computing next_topic -- found live (100% reproducible): the
        # old order computed next_topic from the PRE-increment index, so the hint
        # injected right after the patient answered topic N always described topic N
        # itself (the one just answered), one full step behind the true next topic. The
        # model mostly compensated using its own read of conversation history, EXCEPT at
        # fiebre: its hint is phrased as a yes/no question ("si ha tenido fiebre o
        # escalofrios"), and receiving that as "next topic" immediately after the
        # patient's fever answer read as "this is already answered" -- producing exactly
        # the reported bug (asks, then immediately says thanks/move-on in the same
        # breath, without waiting).
        if not self.script_done:
            self.topic_index += 1
            next_topic = topic_hint(self.topic_index)
            if next_topic:
                # Phrased as a silent background note, not an instruction to react to or
                # echo -- found live (real call transcripts) that a more imperative
                # phrasing here ("siguiente tema pendiente por preguntar") measurably
                # correlated with the model narrating the mechanism out loud ("procedamos
                # al siguiente tema", "entendido, continuemos"), which SYSTEM_PROMPT_ES's
                # rule 8 now also explicitly forbids.
                turn_ctx.add_message(
                    role="system",
                    content=f"(Nota interna, no la menciones: pregunta ahora sobre {next_topic}.)",
                )
                return

        # Either the six scripted topics just finished, or this turn was the patient's
        # answer during the makeup round (script_done already True from a prior turn).
        # Either way, suppress the framework's normal automatic reply for THIS turn and
        # hand off to _prompt_next_topic, which decides whether a makeup round is still
        # needed or it's time to say goodbye and end the call. Found live: letting this
        # turn's own automatic reply say goodbye AND leaving the session open afterward
        # meant nothing ever called ctx.shutdown() -- a later away-timeout firing could
        # trigger a second, uncoordinated farewell, and worse, since the base system
        # prompt describes the full six-topic script, the model would sometimes drift
        # back into it instead of just repeating the goodbye.
        task = asyncio.ensure_future(self._prompt_next_topic())
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        raise StopResponse()

    async def llm_node(self, chat_ctx: ChatContext, tools: list, model_settings: ModelSettings):
        """Overrides the default llm_node purely to pipe its output through
        strip_reasoning (app/reasoning.py) -- the <think> block our prompt asks for must
        never reach TTS. See that module's docstring for the buffering/latency tradeoff.
        """
        default_stream = Agent.default.llm_node(self, chat_ctx, tools, model_settings)
        async for piece in strip_reasoning(default_stream):
            yield piece


async def summarize_call(session: AgentSession, agent: PostSurgicalAgent) -> None:
    """On call end: the ONE comprehensive classification pass (see module docstring for
    why this moved here from per-turn) -- six clinical signals + triage classification
    over the FULL transcript, plus the narrative summary fields. Two separate LLM calls
    (classification, then narrative) rather than one combined prompt: keeps
    FINAL_CLASSIFICATION_PROMPT_ES focused (small-model prompt-adherence reasons, see
    app/prompts.py's module docstring) and means a narrative-generation hiccup can't
    take down the classification that actually matters for escalation.
    """
    call_id = agent.call_ctx.call_id
    transcript = "\n".join(f"{m.role}: {m.text_content}" for m in session.history.messages())
    if not transcript.strip():
        await db.mark_call_completed(call_id)
        return

    # _generate_narrative_summary only needs the transcript -- independent of the
    # classification/pathology chain below, so it runs CONCURRENTLY with it rather than
    # strictly after. Found live: this whole end-of-call pipeline (classification,
    # pathology validation, narrative summary -- three sequential LLM calls) can take
    # long enough under real Ollama contention to blow past the process's shutdown
    # timeout and get force-killed mid-write (see WorkerOptions' shutdown_process_timeout
    # comment) -- overlapping the one genuinely independent call is a real, if partial,
    # reduction in that total wall-clock time.
    narrative_task = asyncio.ensure_future(_generate_narrative_summary(transcript, call_id))

    classification = await _classify_full_call(transcript, call_id)
    agent.snapshot.merge(classification)
    await db.upsert_clinical_snapshot(call_id, agent.snapshot)

    # Pathology validation: a SEPARATE call (see PATHOLOGY_VALIDATION_PROMPT_ES's
    # docstring for why it isn't folded into the classification call above), grounded by
    # a retrieval against the just-extracted six-signal snapshot rather than the raw
    # transcript -- a compact, structured query ("dolor 9/10, apetito muy_disminuido...")
    # returns more targeted chunks than the whole conversation would. Separate from the
    # live per-turn retrieval in on_user_turn_completed (per-topic, one turn at a time).
    #
    # Comorbidities are folded into the query too -- found live: a patient who typed
    # "Diabetes" into call-interface's pre-call form had it correctly stored and shown in
    # the admin console, but pathology validation never actually SAW it (query was
    # signals-only), so the KB search couldn't surface comorbidity-relevant content (e.g.
    # diabetes + wound healing) even when the corpus had it, and the model correctly but
    # unhelpfully said there wasn't enough to correlate.
    retrieval_query = agent.snapshot.render_es()
    if agent.call_ctx.comorbidities:
        retrieval_query += f". Condiciones preexistentes: {', '.join(agent.call_ctx.comorbidities)}"
    try:
        kb_chunks = await retrieval.search(retrieval_query, top_k=8, category_hint=agent.call_ctx.category)
    except Exception:
        logger.exception("end-of-call retrieval.search failed for call_id=%s -- skipping pathology validation", call_id)
        kb_chunks = []
    pathology = await _validate_pathology(agent.snapshot, kb_chunks, agent.call_ctx, call_id)

    # classification.get("triage", "verde") is NOT a sufficient guard -- found live: the
    # model sometimes returns "triage": null explicitly (present key, None value), and
    # .get()'s default only ever applies to a MISSING key. That None then sails straight
    # through fuse()'s first branch (which returns model_level UNCHECKED whenever no rule
    # match exists -- the common case) into db.finalize_triage, where asyncpg casts
    # Python None to SQL NULL without error (unlike an invalid non-null string, which
    # WOULD raise on the ::triage_level cast) -- so this failed completely silently,
    # leaving a call "completed" with a real rationale/pathology assessment but no
    # final_triage at all. Validate explicitly instead of trusting .get()'s default.
    model_triage = classification.get("triage")
    if model_triage not in decision.TRIAGE_ORDER:
        logger.warning("call_id=%s classification returned invalid/missing triage %r -- defaulting to verde", call_id, model_triage)
        model_triage = "verde"

    # fuse() takes ONE rule match -- agent.worst_rule_match is already the running worst
    # across every turn of the call (app/main.py's on_user_turn_completed), so this is
    # still "max(model, worst rule finding)" over the whole call, not just this pass.
    fused = decision.fuse(
        model_triage,
        classification.get("rationale") or f"clasificacion del modelo (confianza={classification.get('confidence', 0)})",
        agent.worst_rule_match,
    )
    # Missing_info is computed from the FINAL, validated snapshot rather than trusted
    # from the model's own self-report -- found live: the model can be internally
    # inconsistent within the same JSON response (correctly filling "sleep":
    # "levemente_alterado" in the structured fields while ALSO listing "sueño" in
    # "missing_info", contradicting itself). agent.snapshot is the actual source of
    # truth after enum validation/fuzzy-correction, so it can't disagree with itself the
    # way raw model output can.
    model_missing_info = agent.snapshot.missing_fields()

    # Map the model's self-reported chunk_id citations back to full {chunk_id,
    # document_id, page} dicts (same shape as insert_escalation's cited_documents) --
    # only chunk_ids that were actually offered in kb_chunks are kept, so a hallucinated
    # id can't produce a fabricated citation.
    chunks_by_id = {c.chunk_id: c for c in kb_chunks}
    pathology_citations = pathology.get("pathology_citations")
    if not isinstance(pathology_citations, list):
        pathology_citations = []
    # Includes the chunk's own text (not just its id/page) -- the admin console's
    # evidence chips (app/frontend/admin-console) let a reviewer click straight through
    # to what the knowledge base actually said, without a separate document-fetch
    # endpoint or round-trip back to vector-store.
    pathology_evidence = [
        {"chunk_id": c.chunk_id, "document_id": c.document_id, "page": c.page, "text": c.text}
        for cid in pathology_citations
        if isinstance(cid, str) and (c := chunks_by_id.get(cid)) is not None
    ]

    await db.finalize_triage(
        call_id=call_id,
        level=fused.level,
        rationale=fused.rationale,
        confidence=classification.get("confidence"),
        missing_info=model_missing_info,
        pathology_assessment=_coerce_text(pathology.get("pathology_assessment")),
        pathology_evidence=pathology_evidence,
    )
    if fused.level != "verde":
        logger.info("final call classification call_id=%s level=%s triggered_by=%s", call_id, fused.level, fused.triggered_by)

    # Narrative summary is a "nice to have" on top of the triage/six-signal data already
    # persisted above -- a failure here (LLM call error, or the model returning a field
    # in a shape asyncpg can't write to a text column, found live: "symptoms_reported"
    # coming back as a nested object instead of a string) must not prevent
    # mark_call_completed below, or a call whose actual clinical data was saved fine
    # would be left stuck "active" forever, looking like the whole pipeline failed.
    try:
        summary = await narrative_task
        await db.finalize_call_summary(
            call_id=call_id,
            procedure=agent.call_ctx.procedure or agent.call_ctx.category,
            symptoms_reported=_coerce_text(summary.get("symptoms_reported")),
            decision=_coerce_text(summary.get("decision")),
            references=None,  # TODO(workstream C): aggregate cited_documents across the call's escalations
            next_steps=_coerce_text(summary.get("next_steps")),
        )
    except Exception:
        logger.exception("finalize_call_summary failed for call_id=%s -- triage/signals already saved, continuing", call_id)

    await db.mark_call_completed(call_id)


def _coerce_text(value: object) -> str | None:
    """Model-returned JSON is not guaranteed to match the requested schema (found live,
    more than once: a field meant to be a plain string coming back as a nested object)
    -- coerce to a readable string instead of letting an unexpected type crash the
    Postgres write that's supposed to be the reliable, durable half of this pipeline."""
    if value is None or isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


_SEARCH_TAG_RE = re.compile(r"<search>(.*?)</search>", re.S)


async def _decide_search(patient_text: str, call_id: str) -> str | None:
    """Isolated preflight call -- see SEARCH_DECISION_PROMPT_ES's docstring for why this
    is separate from the conversational reply. Returns the model's own search query if
    it judged the patient's turn to need a KB lookup, else None. The model reliably
    reproduces the <search>/<no_search/> tag itself but, left unconstrained, tends to
    ramble on well past it (extra "reasoning", even a hallucinated continuation of the
    scenario) -- harmless here since this call is never spoken and we only ever look for
    the FIRST <search>...</search> match, discarding everything else. num_predict caps
    generation at roughly where the tag itself would close, to keep this fast rather
    than paying for tokens we're going to throw away regardless.
    """
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{settings.ollama_host}/api/chat",
                json={
                    "model": settings.ollama_model,
                    "messages": [
                        {"role": "system", "content": SEARCH_DECISION_PROMPT_ES},
                        {"role": "user", "content": patient_text},
                    ],
                    "stream": False,
                    "options": {"num_predict": 60},
                },
            )
            resp.raise_for_status()
            text = resp.json()["message"]["content"]
    except Exception:
        logger.exception("search-decision call failed for call_id=%s -- skipping KB lookup this turn", call_id)
        return None
    match = _SEARCH_TAG_RE.search(text)
    return match.group(1).strip() if match else None


async def _classify_full_call(transcript: str, call_id: str) -> dict:
    """The end-of-call classification call -- see FINAL_CLASSIFICATION_PROMPT_ES's
    docstring for why this runs once, here, instead of per-turn."""
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{settings.ollama_host}/api/chat",
                json={
                    "model": settings.ollama_model,
                    "messages": [
                        {"role": "system", "content": FINAL_CLASSIFICATION_PROMPT_ES},
                        {"role": "user", "content": transcript},
                    ],
                    "format": "json",
                    "stream": False,
                },
            )
            resp.raise_for_status()
            return json.loads(resp.json()["message"]["content"])
    except Exception:
        logger.exception("end-of-call classification failed for call_id=%s -- defaulting to verde/low-confidence", call_id)
        return {"triage": "verde", "confidence": 0.0, "missing_info": ["clasificacion fallo, ver logs"]}


async def _validate_pathology(
    snapshot: ClinicalSnapshot, kb_chunks: list[retrieval.RetrievedChunk], call_ctx: call_context.CallContext, call_id: str
) -> dict:
    """KB-grounded pathology validation -- see PATHOLOGY_VALIDATION_PROMPT_ES's docstring
    for why this is a separate call from _classify_full_call rather than extra fields on
    it (found live: folding both into one prompt broke schema adherence on this small a
    model, worse once a KB context block was also present).

    Takes the full call_ctx (not just category) so age/comorbidities reach the model --
    found live: a patient's explicitly-provided comorbidities (e.g. "Diabetes", entered
    in call-interface's pre-call form) were being persisted and shown in the admin
    console, but never actually passed into this prompt, so the model had no way to
    correlate them against the KB even when relevant content existed."""
    kb_context = build_context_prompt(retrieval.format_for_prompt(kb_chunks), category=call_ctx.category)
    patient_context_parts = [f"Hallazgos: {snapshot.render_es()}"]
    if call_ctx.age is not None:
        patient_context_parts.append(f"Edad: {call_ctx.age} anos")
    if call_ctx.comorbidities:
        patient_context_parts.append(f"Condiciones preexistentes: {', '.join(call_ctx.comorbidities)}")
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{settings.ollama_host}/api/chat",
                json={
                    "model": settings.ollama_model,
                    "messages": [
                        {"role": "system", "content": PATHOLOGY_VALIDATION_PROMPT_ES},
                        {"role": "system", "content": kb_context},
                        {"role": "user", "content": ". ".join(patient_context_parts)},
                    ],
                    "format": "json",
                    "stream": False,
                },
            )
            resp.raise_for_status()
            return json.loads(resp.json()["message"]["content"])
    except Exception:
        logger.exception("pathology validation failed for call_id=%s", call_id)
        return {}


async def _generate_narrative_summary(transcript: str, call_id: str) -> dict:
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
            return json.loads(resp.json()["message"]["content"])
    except Exception:
        logger.exception("narrative summary generation failed for call_id=%s", call_id)
        return {}


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
        # If the patient goes quiet (no speech AND no agent speech) for this long,
        # livekit-agents marks user_state "away" -- the handler below turns that into
        # "move on" rather than waiting indefinitely. NOT the same knob as end-of-speech
        # detection (how long to wait after the patient STOPS talking mid-answer, which
        # is turn_detection/VAD's job) -- this is "never started answering at all".
        # Confirmed via the installed package: user_away_timeout only sets user state, it
        # doesn't itself continue the conversation -- that's this handler's job.
        #
        # Was 2.0s -- found live (real call transcript) that this was too aggressive: a
        # post-surgical patient taking a natural moment to process a question and start
        # answering could get moved past before they'd said anything, which read as the
        # agent "skipping ahead without waiting for an answer". livekit-agents' own
        # default is 15.0s; 6s is a middle ground -- enough real thinking time without
        # letting a genuinely silent patient hang the call for too long.
        user_away_timeout=6.0,
    )

    agent = PostSurgicalAgent(
        call_ctx=call_ctx,
        instructions=build_instructions(call_ctx, prior_snapshot_es),
        job_ctx=ctx,
    )

    @session.on("user_state_changed")
    def _on_user_state(event: agents.UserStateChangedEvent) -> None:
        if event.new_state != "away" or agent.closing:
            return
        task = asyncio.ensure_future(agent._prompt_next_topic())
        agent._background_tasks.add(task)
        task.add_done_callback(agent._background_tasks.discard)

    @session.on("close")
    def _on_session_close(event: agents.CloseEvent) -> None:
        # AgentSession closes itself (RoomInputOptions.close_on_disconnect defaults to
        # True) when the patient disconnects -- but by default it does NOT delete or
        # leave the room (delete_room_on_close defaults to False), so the job's own
        # shutdown callback (summarize_call, registered below) would otherwise never
        # fire in that case: confirmed by reading livekit-agents' own job process code,
        # the ONLY thing that reliably triggers it is either OUR OWN ctx.shutdown() call
        # (see _prompt_next_topic) or the agent's Room object itself receiving a
        # "disconnected" event, neither of which a bare session close causes on its own.
        # This is what makes "the final analysis must happen even after the call has
        # been finished" true regardless of who ends the call or how -- not just for our
        # own farewell-triggered close.
        logger.info("call_id=%s session closed (reason=%s) -- ensuring job shutdown runs", call_ctx.call_id, event.reason)
        ctx.shutdown(reason=f"session closed: {event.reason}")

    @session.on("conversation_item_added")
    def _on_item(event: agents.ConversationItemAddedEvent) -> None:
        item = event.item
        if not isinstance(item, ChatMessage):
            return
        role = "patient" if item.role == "user" else "agent"
        metrics = item.metrics or {}
        # Attaches whatever chunks grounded THIS reply, if on_user_turn_completed's
        # search-decision gate triggered a lookup for the turn that produced it -- see
        # PostSurgicalAgent.pending_citation_chunk_ids' docstring. Cleared immediately
        # after use so it only ever applies to the one reply it actually grounded.
        retrieved_chunk_ids = None
        if role == "agent" and agent.pending_citation_chunk_ids:
            retrieved_chunk_ids = agent.pending_citation_chunk_ids
            agent.pending_citation_chunk_ids = []
        asyncio.create_task(
            db.insert_turn(
                call_id=call_ctx.call_id,
                role=role,
                text=item.text_content or "",
                stt_ms=_ms(metrics.get("transcription_delay")),
                retrieval_ms=_ms(metrics.get("on_user_turn_completed_delay")),
                llm_ms=_ms(metrics.get("llm_node_ttft")),
                tts_ms=_ms(metrics.get("tts_node_ttfb")),
                retrieved_chunk_ids=retrieved_chunk_ids,
            )
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
            # Worker's own internal HTTP server (health/metrics, not anything
            # api-gateway or a frontend calls) defaults to a FIXED port 8081 in "start"
            # mode (0 = random free port only in "dev" mode) -- found live, colliding
            # with an unrelated project's container also using 8081 on this machine.
            # Nothing depends on this port being any particular number, so always use a
            # random free one regardless of dev/start mode.
            port=0,
            # Default is 10s -- found live that this is nowhere near enough for OUR
            # prewarm_fnc, which can legitimately take ~111s on a cold cache (Kokoro's
            # ~2GB weight download, see prewarm()'s docstring) and multiple worker
            # processes (num_idle_processes, default 4) run it concurrently at startup,
            # contending for the same bandwidth/CPU -- a process that hits this timeout
            # gets killed and retried, not just delayed. Generous margin over the worst
            # measured cold-cache time.
            initialize_process_timeout=300.0,
            # Default is 10s -- found live that this is nowhere near enough for OUR
            # shutdown path: summarize_call runs a sequential chain of real LLM calls
            # (end-of-call classification, pathology validation, narrative summary),
            # and the live makeup-round's own classification (app/main.py's
            # _prompt_next_topic) can independently take 18+ seconds when Ollama is
            # under contention. A process that hits the DEFAULT 10s gets force-killed
            # (SIGUSR1) mid-shutdown -- confirmed live: this is exactly what silently
            # dropped a call's final classification/pathology validation, leaving it
            # stuck "active" with no final_triage despite summarize_call having
            # actually started. Generous margin over the worst observed chain length.
            shutdown_process_timeout=120.0,
        )
    )
