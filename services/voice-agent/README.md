# voice-agent

Real-time voice pipeline. Rationale: [`../../specs/implementation-plan.md`](../../specs/implementation-plan.md)
§2.1-§2.6, §2.10, §4.3. Joins each LiveKit room api-gateway creates and runs the STT ->
retrieve -> generate -> decide -> TTS loop, leading a routine post-op check-in and
privately inferring whether staff should be notified (never announced to the patient).

```
app/main.py               entrypoint + PostSurgicalAgent (on_user_turn_completed hook, persistence)
app/call_context.py        parses LiveKit room metadata (patient/category/procedure/postop_day)
app/clinical_snapshot.py   the 6-signal running clinical picture, merged turn-by-turn, validated before persisting
app/providers/llm.py       provider selection (Ollama default -- see docstring for why no extra abstraction)
app/providers/stt.py       dual-mode STT: groq.STT (plugin) | LocalWhisperSTT (hand-rolled, faster-whisper)
app/providers/tts.py       hand-rolled Kokoro-82M wrapper (no official livekit plugin exists)
app/reasoning.py           strips <think> reasoning out of the LLM stream before it reaches TTS
app/retrieval.py           vector-store hybrid search client (category_hint is a soft boost, not a filter)
app/decision.py            deterministic red-flag rule layer (narrow scope, see its docstring) + model/rule fusion
app/prompts.py             system + classification prompts (Spanish), per-call instruction building
app/db.py                  Postgres writes: turns, escalations, call_summaries (live snapshot + finalize)
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

The call now follows a fixed structure, matching what the reference dataset's real
conversations actually do (`docs/dataset-eda.md` §2: every `capa1_limpia` conversation
has exactly 12 turns, always the same 6-question order, `std=0.0` -- not a loose
pattern):

- **Greeting**: spoken VERBATIM (`GREETING_ES` in `app/prompts.py`) via
  `AgentSession.say()` in a new `PostSurgicalAgent.on_enter()` hook, not asked of the
  LLM as an instruction -- a ~3.8B model told to "greet the patient like this" will
  paraphrase, and this exact wording is a hard requirement. Waits for
  `SpeechHandle.wait_for_playout()` before triggering the first question, so they don't
  overlap.
- **Fixed six-topic order**: `ClinicalSnapshot.next_missing_topic()` walks pain → fever →
  mobility → wound → appetite → sleep (docs/dataset-eda.md's exact order) and returns the
  next unanswered one; injected into the LLM's context every turn (`on_user_turn_completed`)
  and used directly for the post-greeting and post-silence prompts (`_prompt_next_topic()`).
  This reverses the previous prompt, which explicitly told the model order didn't matter
  ("no hace falta preguntarlo en ese orden") -- overruled once it became clear the
  reference dataset's real agent turns never bundle or reorder topics.
- **~2s silence → continue**: `AgentSession(user_away_timeout=2.0, ...)` +
  a `user_state_changed` handler that calls `_prompt_next_topic()` when state becomes
  `"away"`. Confirmed via the installed package's source that `user_away_timeout` only
  sets user state, it doesn't itself continue the conversation -- that's this handler's
  job. Deliberately not the same knob as end-of-speech detection (how long to wait after
  the patient STOPS talking mid-answer, which is `turn_detection`/VAD's job) -- this is
  "never started answering at all".
- **Tone adaptation**: a new prompt rule asks the model to mirror the patient's register
  (more relaxed if the patient is informal, more formal if not) without becoming
  unprofessional or using unnecessary medical jargon.

All of the above verified via introspection against the real installed `livekit-agents`
API (`on_enter`, `session.say`/`generate_reply`, `SpeechHandle.wait_for_playout`,
`user_away_timeout`/`UserState`/`user_state_changed` semantics all confirmed from source,
not guessed) and via direct unit tests of `next_missing_topic()`'s ordering.

Now also verified against a real live call: first attempt was **completely silent** --
no greeting, no replies, and it looked like STT wasn't working either. Root cause, found
in the logs, was much simpler than it first looked: `.env` had `TTS_VOICE=es` (both
`.env` and `.env.example` did -- a plausible-looking but wrong guess, baked in from
early scaffolding), which 404s fetching `voices/es.pt` from hexgrad/Kokoro-82M and
silently kills every single TTS call. The conversation logic itself was working
correctly the whole time -- log evidence shows it dutifully advancing through the
greeting, then pain, then fever, right on the ~2s silence timeout, every ~2-3 minutes,
just never producing audio for any of it (each attempt logged `failed to synthesize
speech: no audio frames were pushed`, retried, gave up, moved on). Fixed: `TTS_VOICE`
now defaults to `ef_dora`, the actual correct value `app/config.py` already had as its
Python-level default before `.env` overrode it -- verified against Kokoro-82M's real
`voices/` file listing (`ef_dora`, `em_alex`, `em_santa` are the only real Spanish
voices), and confirmed live: a direct synthesis call now returns real, non-empty audio.

Still **not** run against a live LiveKit room + Ollama end to end. Before trusting it in
a demo:

- [ ] Run it end to end against a real room and confirm `on_user_turn_completed`'s
      injected context (retrieval + running snapshot) actually reaches the LLM call as expected
- [ ] Confirm `ChatMessage.metrics` fields populate the way the docstrings describe
      (`transcription_delay`, `on_user_turn_completed_delay`, `llm_node_ttft`, `tts_node_ttfb`)
- [ ] Verify `settings.tts_voice` ("ef_dora") against Kokoro's actual Spanish voice list
      once the weights are downloaded (see `app/providers/tts.py`)
- [ ] Get real per-turn token counts into `turns.tokens_in/out` -- `ChatMessage.metrics`
      has timing but not token counts; `livekit.agents.metrics.UsageCollector` has
      call-level totals and is the likely source, not yet wired in
- [ ] Clinical review of the red-flag rule table in `app/decision.py` -- narrower in scope
      now (explicitly flagged there as unreviewed either way)
- [ ] Track B's confidence output isn't wired into the clarifying-question behavior yet
      (`app/prompts.py`'s CLASSIFICATION_PROMPT_ES asks for it, `app/main.py` doesn't
      branch on it) -- currently the model handles ambiguity via its own conversational
      judgment, not a hard code path

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
