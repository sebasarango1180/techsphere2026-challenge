-- Enriches the schema with the patient/clinical context the agent needs to actually
-- run a routine post-op check-in and privately decide whether to escalate, per
-- docs/dataset-eda.md's column-by-column comparison against the reference dataset.
-- Three things, deliberately bundled as one migration since they're the same decision:
--
-- 1. `third_party` turn role -- 151/3991 turns in the reference dataset are a family
--    member interjecting, not the patient. A real ROJO case's key symptom ("mi hija me
--    dijo que vio...") came from exactly this role. Previously unrepresentable.
-- 2. Patient clinical/demographic context -- category (the clean key decision.py's
--    rules and vector-store's category_hint actually need, previously absent entirely),
--    age, gender, comorbidities, and identity/demographic fields. Comorbidities matter
--    clinically (e.g. diabetes changes wound-healing red-flag thresholds), not just for
--    show.
-- 3. Structured clinical signals on `call_summaries` (pain_nrs, fever_c, mobility,
--    wound, appetite, sleep) -- the same six-signal taxonomy the reference dataset's
--    trajectories use. `call_summaries` changes role here: it's no longer written once
--    at call end, it's a live snapshot upserted incrementally as the model learns things
--    during the call (see services/voice-agent/app/db.py) -- `updated_at` reflects that.

ALTER TYPE turn_role ADD VALUE IF NOT EXISTS 'third_party';

CREATE TYPE mobility_level AS ENUM ('normal', 'limitada_esperada', 'incapacitante_nueva');
CREATE TYPE wound_status AS ENUM ('normal', 'eritema_leve', 'secrecion_purulenta');
CREATE TYPE appetite_level AS ENUM ('normal', 'levemente_disminuido', 'muy_disminuido');
CREATE TYPE sleep_quality AS ENUM ('normal', 'levemente_alterado', 'muy_alterado');
-- Vocabulary matches docs/dataset-eda.md §3 exactly (not translated) so a call's
-- structured summary is directly comparable to the reference dataset's own ground
-- truth format, per that doc's §7 recommendation.

ALTER TABLE patients
    ADD COLUMN category     text,       -- clean key (e.g. "cholecystitis") -- what
                                         -- decision.py's rule table and vector-store's
                                         -- category_hint key off of. `procedure` stays
                                         -- the human-readable Spanish name.
    ADD COLUMN age           integer,
    ADD COLUMN gender        text,
    ADD COLUMN comorbidities jsonb NOT NULL DEFAULT '[]',
    ADD COLUMN national_id   text,       -- documento_cc in the reference dataset
    ADD COLUMN address       text,
    ADD COLUMN city          text,
    ADD COLUMN department    text,
    ADD COLUMN eps           text;

ALTER TABLE calls
    ADD COLUMN postop_day integer;       -- dia_postop -- which check-in this call is for

CREATE INDEX idx_calls_patient_postop ON calls(patient_id, postop_day);
-- Supports "fetch this patient's most recent prior call summary" for cross-call
-- continuity (services/voice-agent/app/db.py's fetch_patient_history).

ALTER TABLE call_summaries
    ADD COLUMN pain_nrs   integer,
    ADD COLUMN fever_c    numeric(4,1),
    ADD COLUMN mobility   mobility_level,
    ADD COLUMN wound      wound_status,
    ADD COLUMN appetite   appetite_level,
    ADD COLUMN sleep      sleep_quality,
    ADD COLUMN updated_at timestamptz NOT NULL DEFAULT now(),
    ADD CONSTRAINT call_summaries_pain_nrs_range
        CHECK (pain_nrs IS NULL OR (pain_nrs BETWEEN 0 AND 10));

COMMENT ON TABLE call_summaries IS
    'Live clinical snapshot for a call, upserted incrementally as signals are learned '
    'during the conversation (not just written once at call end) -- see '
    'services/voice-agent/app/db.py''s merge-on-conflict upsert.';
