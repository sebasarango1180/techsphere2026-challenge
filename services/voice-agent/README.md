# voice-agent

Real-time voice pipeline. Rationale: [`../../specs/implementation-plan.md`](../../specs/implementation-plan.md)
§2.1-§2.6, §2.10, §4.3. Joins each LiveKit room api-gateway creates and runs the STT ->
retrieve -> generate -> decide -> TTS loop, leading a routine post-op check-in and
privately inferring whether staff should be notified (never announced to the patient).

```
app/main.py               entrypoint + PostSurgicalAgent (turn-index script, rule-layer safety net, end-of-call classification, persistence)
app/call_context.py        parses LiveKit room metadata (patient/category/procedure/postop_day/age/comorbidities)
app/clinical_snapshot.py   the 6-signal clinical picture (extracted once, end-of-call), enum/range validation before persisting
app/providers/llm.py       provider selection (Ollama default -- see docstring for why no extra abstraction)
app/providers/stt.py       dual-mode STT: groq.STT (plugin) | LocalWhisperSTT (hand-rolled, faster-whisper)
app/providers/tts.py       hand-rolled Kokoro-82M wrapper (no official livekit plugin exists)
app/reasoning.py           strips <think> reasoning out of the LLM stream before it reaches TTS
app/retrieval.py           vector-store hybrid search client (category_hint is a soft boost, not a filter)
app/decision.py            deterministic red-flag rule layer (narrow scope, see its docstring) + model/rule fusion
app/prompts.py             system prompt, end-of-call classification + pathology validation prompts (Spanish), per-call instruction building
app/db.py                  Postgres writes: turns, escalations, call_summaries (six signals + final triage + pathology validation)
```

## Status

This is real, introspection-verified code against the actual installed `livekit-agents`
1.6.9 API (not guessed from memory -- every signature used here was checked with
`inspect.signature` against the installed package before being called). Several pieces
are now integration-tested against a real Postgres (not just unit-tested in isolation):

- `db.upsert_clinical_snapshot` / `finalize_call_summary` / `fetch_latest_snapshot_for_patient`
  -- verified a 2-turn snapshot merge doesn't null out earlier fields, `finalize` doesn't
  clobber structured signals, and cross-call continuity correctly finds a prior call's
  snapshot (and correctly finds nothing for a patient's first call)
- `call_context.from_room` -- verified against the exact metadata shape api-gateway
  produces on a real LiveKit server (see `services/api-gateway/README.md`), plus edge
  cases: empty metadata, `None` metadata, malformed JSON -- all degrade to an anonymous
  call rather than crashing
- `decision.py`'s rule table and negation handling -- 9 cases covering the recalibrated
  scope (objective/numeric/absence/emergency triggers only)
- `reasoning.py`'s `<think>`-stripping state machine -- same-chunk tags, tag boundaries
  split across chunks, and a malformed-unclosed-tag safety net

## Warmup (every heavy asset, loaded once per worker process, not once per call)

`livekit-agents` spawns one worker **process** per concurrent call slot and reuses each
across many calls -- `WorkerOptions.prewarm_fnc` (`app/main.py`'s `prewarm()`) runs once
per process before it's handed any job, confirmed by reading the installed package's
`ipc/job_proc_lazy_main.py` (`_JobProc.initialize()` calls it synchronously, before that
process even has an asyncio event loop). Without it, every heavy asset below would be
loaded by whichever call happened to land on a fresh process first -- the same class of
bug already found and fixed for BGE-M3 in vector-store (see its README), just spread
across more assets here:

- **Silero VAD** -- genuinely prewarmable: `silero.VAD.load()` has no dependency on an
  active job (confirmed by reading livekit-plugins-silero's source), so it's loaded once
  in `prewarm()` and stashed in `proc.userdata`, read back in `entrypoint()` via
  `ctx.proc.userdata`.
- **Turn detector (`MultilingualModel`)** -- deliberately **NOT** built in `prewarm()`,
  even though it looks like the same kind of asset. Verified live that constructing it
  outside a job raises `RuntimeError: no job context found`: `EOUModelBase.__init__`
  calls `get_job_context().inference_executor`, and that only exists once `entrypoint()`
  is running (its ONNX inference runs out-of-process via that executor, managed by
  livekit-agents itself). What's prewarmed instead is the weight file: `python -m
  app.main download-files` (livekit-agents' own CLI mechanism, backed by a HuggingFace
  Hub fetch) runs at Docker build time (`Dockerfile`) and, in native mode, in parallel
  with everything else in `scripts/setup.sh` -- so by the time `entrypoint()` builds
  `MultilingualModel()` per call, it's wiring against an already-local file, not a
  network fetch.
- **Kokoro TTS pipeline** (`app/providers/tts.py`) -- `_get_pipeline()`'s `lru_cache`
  already meant the model loads once per process rather than once per call; `prewarm()`
  now also calls it via a public `warm_up()` so that one load happens before the first
  call is dispatched, not during it. Separately, found a real device bug while auditing
  Metal usage: Kokoro's own `KPipeline.__init__` auto-selects a device with `'cuda' if
  torch.cuda.is_available() else 'cpu'` -- no MPS branch at all, so it silently ran on
  CPU even in native-agent mode (macOS, specifically to get Metal). Fixed by explicitly
  passing `device='mps'` when available (`_pick_device()`); verified live: model loads
  onto `mps:0`, and per-synthesis latency dropped from ~2.6s to ~1.2s (warm, averaged
  over 3 calls).
- **faster-whisper (local STT mode)** -- found a real bug here, not just a missing
  prewarm: `LocalWhisperSTT` stored its `WhisperModel` on `self`, but `get_stt()` runs
  inside `entrypoint()` (once per *call*, not per process), so every single call in local
  STT mode was reloading the model from scratch. Fixed by moving the model into a
  module-level `lru_cache` keyed on model size (`app/providers/stt.py`), decoupling it
  from the STT wrapper's per-call lifecycle; `prewarm()` calls its `warm_up()` when
  `STT_MODE=local`.
- **Ollama** (`settings.ollama_model`, default `phi3.5:3.8b`) -- two separate gaps, both
  found live rather than assumed: (1) `scripts/setup.sh` only ever pulled the model in
  native mode -- in Docker mode the `ollama` container came up with no model in it at
  all, a total-failure gap, not just a slow one; fixed by bringing the `ollama` container
  up early (parallel with the other services' image builds) and driving the pull over
  its HTTP API. (2) Even with the model pulled, Ollama's own first-inference load cost
  was unaddressed -- fixed the same way as BGE-M3: `POST /api/generate` with an empty
  prompt is Ollama's documented load-only request shape (`done_reason: "load"`, no
  actual generation). Verified live end to end against a real local Ollama running the
  actual challenge model: the empty-prompt warmup call took ~2.1s on a cold model, and an
  immediately-following real prompt then reported `load_duration` of ~22ms (i.e.
  effectively free) instead of paying that cost. `prewarm()` calls this too, so a
  worker's first real call doesn't pay it either.

`prewarm()` as a whole was run live end to end (not just reviewed): on a fully cold
cache it took ~111s, dominated by Kokoro's one-time ~2GB weight download from Hugging
Face -- a real cost, but paid once per worker process at startup rather than by whichever
patient's call happens to be first.

## Conversation script

The call follows a fixed structure, matching what the reference dataset's real
conversations actually do (`docs/dataset-eda.md` §2: every `capa1_limpia` conversation
has exactly 12 turns, always the same 6-question order, `std=0.0` -- not a loose
pattern), reworked more than once against real live-call evidence -- see
`docs/decision-flow.md` for the full "why" behind each pivot below.

- **Greeting AND farewell are spoken VERBATIM** (`GREETING_ES`/`build_farewell()` in
  `app/prompts.py`) via `AgentSession.say()`, never generated by the LLM from an
  instruction -- a ~3.8B model told to "say goodbye naturally" isn't any more reliable at
  staying in character than one told to "greet the patient like this" will paraphrase.
  Found live: an LLM-generated closing sometimes produced confused, process-narrating
  text ("esperamos la siguiente instruccion medica...") instead of a simple goodbye.
- **Fixed six-topic order via a plain turn counter** (`PostSurgicalAgent.topic_index`
  into `clinical_snapshot.QUESTION_ORDER`), advanced unconditionally after every patient
  turn -- deliberately NOT dependent on any LLM call succeeding. An earlier
  snapshot-driven version stalled on the same topic whenever its per-turn classification
  call failed; this doesn't have that failure mode. The "next topic" hint given to the
  model is phrased as a parenthetical internal note, not an instruction to react to --
  found live that a more imperative phrasing measurably correlated with the model
  narrating the mechanism out loud ("procedamos al siguiente tema").
- **Missing-topic makeup round, bounded to one attempt**: once all six topics have been
  asked, the agent runs the SAME end-of-call classification live (not just at
  `summarize_call`) purely to check whether the patient's answers actually covered all
  six signals, and if not, asks specifically about what's missing before saying goodbye
  -- this is what makes "if the agent considers a question hasn't been answered, it
  should introduce it again before closing" true, since the turn counter alone can't
  distinguish a real answer from STT noise or a clarification exchange eating a slot.
- **The agent ends the call itself**, not the client: after the farewell finishes
  playing, `_prompt_next_topic()` calls `ctx.shutdown()` directly -- this is what
  guarantees the end-of-call classification (`summarize_call`, a registered shutdown
  callback) runs promptly rather than depending on the patient's client disconnecting
  cleanly. A `session.on("close")` handler in `entrypoint()` ALSO calls `ctx.shutdown()`
  for any other close reason (e.g. the patient disconnecting first) -- found live that
  `AgentSession`'s own auto-close-on-disconnect behavior does not, by default, leave or
  delete the room, so without this the shutdown callback would silently never fire in
  that path.
- **~6s silence → continue**: `AgentSession(user_away_timeout=6.0, ...)` + a
  `user_state_changed` handler that calls `_prompt_next_topic()` when state becomes
  `"away"`. Was 2.0s -- found live (real call transcript) that this was aggressive enough
  to move past a question before a post-surgical patient had actually started answering
  it. Deliberately not the same knob as end-of-speech detection (how long to wait after
  the patient STOPS talking mid-answer, which is `turn_detection`/VAD's job) -- this is
  "never started answering at all".
- **Tone adaptation + explicit anti-narration rules**: prompt rules ask the model to
  mirror the patient's register, never repeat back numbers the patient already gave, and
  -- added after finding this leaking into real calls -- never comment on its own
  process/instructions/rules out loud (concrete bad examples included in the prompt,
  since abstract instructions alone weren't reliable enough for a ~3.8B model).

## End-of-call classification + pathology validation

Two separate LLM calls (see `docs/decision-flow.md` for why not one combined prompt),
run once, at call end, over the complete transcript:

1. **`_classify_full_call`**: the six clinical signals + overall triage, using
   `FINAL_CLASSIFICATION_PROMPT_ES` -- includes a qualitative-to-numeric mapping guide
   (e.g. "mucho" ≈ 8/10) since patients often describe severity in words, not numbers.
2. **`_validate_pathology`**: a KB-grounded read on whether the reported symptoms are
   consistent with a normal recovery or point at a possible complication, with specific
   `[chunk_id]` citations -- grounded by a retrieval query built from the just-extracted
   six-signal snapshot, not the raw transcript.

`final_triage = max(model classification, worst deterministic rule-layer finding across
the whole call)` -- `decision.fuse()`. Both are persisted to `call_summaries`
(`infra/postgres/migrations/0003_final_triage.up.sql`,
`0004_pathology_validation.up.sql`) and surfaced in the admin console's "Llamadas" tab.

Model-output robustness, found live rather than assumed: a small model's JSON
occasionally deviates from the requested schema (a field coming back as a nested object
instead of a string, an unrelated key set entirely). `_coerce_text()` and defensive
type-checks around citation/evidence mapping mean a single malformed field degrades that
one piece of data rather than crashing the whole end-of-call pass -- a call's real
triage/six-signal data being saved should never depend on the narrative-summary or
pathology-validation calls also succeeding.

## Remaining known gaps

- [ ] Get real per-turn token counts into `turns.tokens_in/out` -- `ChatMessage.metrics`
      has timing but not token counts; `livekit.agents.metrics.UsageCollector` has
      call-level totals and is the likely source, not yet wired in
- [ ] Clinical review of the red-flag rule table in `app/decision.py` -- narrower in scope
      by design (explicitly flagged there as unreviewed either way)
- [ ] Pathology validation occasionally still deviates from its JSON schema on certain
      inputs even after prompt hardening (3.8B model variance) -- degrades gracefully
      (empty field, not a crash), but isn't 100% reliable

## Run locally (native, not Docker -- needed for Metal on macOS, see plan §2.5)

```sh
export DATABASE_URL=postgres://techsphere:changeme@localhost:5432/techsphere?sslmode=disable
export VECTOR_STORE_URL=http://localhost:8001
export OLLAMA_HOST=http://localhost:11434
export LIVEKIT_URL=ws://localhost:7880
export LIVEKIT_API_KEY=devkey
export LIVEKIT_API_SECRET=changeme_min_32_chars_______________
uv run python -m app.main dev   # hot-reload dev mode; use `start` for production
```
