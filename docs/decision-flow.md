# Decision / escalation flow

Second half of entregable **02**. Rationale in
[`specs/implementation-plan.md`](../specs/implementation-plan.md) §2.3 — the short version
is: escalation is never LLM-only, because a missed escalation (false negative) is scored
as the worst possible failure in the rubric, and the rule layer can only push severity
*up*, never down. The triage classification itself is **private** — the agent leads a
routine check-in conversation and never announces "verde/amarillo/rojo" out loud; when it
escalates, the spoken reply says what happens next in plain language instead.

```mermaid
flowchart TD
    S[Call starts] --> S2{Patient known?}
    S2 -- yes --> S3["Load prior call's clinical snapshot\n(cross-call continuity, plan §2.10)"]
    S2 -- no --> A
    S3 --> A

    A[Patient turn transcribed] --> B[Hybrid retrieval: relevant clinical chunks]
    B --> C1["Track A: conversational reply\n(streamed sentence-by-sentence into TTS)"]
    B --> C2["Track B: structured classification\n(Ollama JSON mode)\n{triage, confidence, missing_info[], citations[],\npain_nrs, fever_c, mobility, wound, appetite, sleep}"]
    A --> D["Deterministic red-flag layer\n(objective thresholds + absence-statements +\nlow-ambiguity emergency vocabulary only --\nNOT free-text symptom-description matching, see below)"]

    C2 --> C3["Merge 6 signals into running\nclinical snapshot, upsert to call_summaries\n(every turn, not just on escalation)"]

    C2 --> E{"Track B confidence low\nor missing_info non-empty?"}
    E -- yes --> F["Ask a clarifying question\nbefore deciding (no guess)"]
    F --> A

    E -- no --> G["final_triage =\nmax(Track B triage, rule-layer triage)"]
    D --> G

    G --> H{final_triage}
    H -- verde --> I["Continue conversation normally\n(never announced to patient)"]
    H -- amarillo --> J["Escalate: log + tell patient in plain language\nwhat happens next, keep talking"]
    H -- rojo --> K["Escalate immediately: log + tell patient in plain\nlanguage what happens next"]

    J --> L[(escalations table:\nlevel, rationale, triggered_by, cited_documents)]
    K --> L

    M[Call ends] --> N["Summarize: procedure, symptoms,\ndecision, references, next steps\n(narrative fields only -- the 6 signals\nare already persisted incrementally)"]
    N --> O[(call_summaries table)]
```

## Notes for whoever implements this (workstream C)

- `triggered_by` on the `escalations` row must record whether Track B, the rule layer, or
  both fired — this is what lets the informe show "the rule layer caught a case the model
  missed" as evidence the asymmetric-risk design actually works, not just a claim.
- The clarifying-question branch (`E -- yes`) is what the rubric's "¿indaga antes de
  decidir?" descriptor is checking for — don't let the model silently guess when Track B
  itself reports low confidence.
- **Red-flag rule table scope, stated plainly because a first version got this wrong**:
  `services/voice-agent/app/decision.py` is scoped to objective numeric thresholds,
  structurally rigid absence-statements ("no orino"), low-ambiguity emergency vocabulary,
  and a few narrow category-specific domain correlations — **not** free-text symptom
  *description* matching (what does wound infection sound like in lay speech). That
  turned out to be a losing game against "lenguaje cotidiano, ambiguo y regional" (the
  challenge's own framing): every phrasing caught invites a slightly different one that
  isn't. That recognition job belongs to Track B, grounded by retrieval — see that
  module's docstring for the concrete mistake (and the concrete false positive) that
  established this boundary.
- The six structured signals (`pain_nrs`, `fever_c`, `mobility`, `wound`, `appetite`,
  `sleep`) persist to `call_summaries` after **every** turn via
  `db.upsert_clinical_snapshot`, COALESCE-merged so a turn that doesn't mention a signal
  never nulls out what an earlier turn established — this is what "keep all the relevant
  patient context for the agent at any point in time" actually means operationally, not
  just within one call but carried forward to the next one for the same patient
  (`db.fetch_latest_snapshot_for_patient`).

<!-- TODO(workstream C): once real test cases exist (verde/amarillo/rojo examples from
dataset_final.xlsx's label_ground_truth), link a short table here of "case → expected
triage → actual triage" as running evidence for the informe. -->
