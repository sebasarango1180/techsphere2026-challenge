-- Adds the END-OF-CALL authoritative classification to call_summaries. Reworked design
-- (was: a per-turn model classification, computed incrementally on every patient turn):
-- classification now runs ONCE, after the six-topic script is complete, over the full
-- transcript -- both because a partial-conversation classification is necessarily less
-- accurate than one over the complete clinical picture, and because running the
-- classification LLM call concurrently with the conversational LLM call on every single
-- turn was contending for the same local Ollama instance and causing real timeouts /
-- dropped connections during live calls. The deterministic rule layer (app/decision.py)
-- is UNAFFECTED by this change and still runs per-turn for real-time safety -- this
-- migration's `final_triage` is `fuse(end_of_call_model_classification,
-- worst_rule_match_seen_during_the_call)`, not a replacement for real-time escalation.
--
-- admin-console's new "Calls" tab (separate from "Documents") reads these columns to
-- show, per call, the six signals already on this table plus this final classification.

ALTER TABLE call_summaries
    ADD COLUMN final_triage      triage_level,
    ADD COLUMN triage_rationale  text,
    ADD COLUMN triage_confidence numeric(3,2),
    ADD COLUMN missing_info      jsonb NOT NULL DEFAULT '[]',
    ADD CONSTRAINT call_summaries_triage_confidence_range
        CHECK (triage_confidence IS NULL OR (triage_confidence BETWEEN 0 AND 1));

COMMENT ON COLUMN call_summaries.final_triage IS
    'Authoritative end-of-call classification -- max(model classification over the full '
    'transcript, worst deterministic rule-layer match seen during the call). NOT the '
    'same thing as a real-time escalations row, though a real-time rule match during '
    'the call may also be reflected here if it was the worst finding overall.';
