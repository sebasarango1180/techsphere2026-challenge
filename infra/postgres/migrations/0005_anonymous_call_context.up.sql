-- Persists the ad-hoc patient context a caller can type into call-interface's pre-call
-- form (name/age/known conditions) for an ANONYMOUS call (no patient_id) -- found live:
-- this was already being sent to voice-agent via LiveKit room metadata (so the agent
-- could address the patient by name during the call), but was never written to Postgres
-- anywhere, so the admin console's Calls tab had nothing to show ("Paciente anonimo" /
-- "Sin contexto de procedimiento" even when the caller had filled in the form).
--
-- Lives on `calls` rather than `patients` because there's no registered patient behind
-- it -- this is exactly the anonymous-call case (services/api-gateway/internal/httpapi/
-- calls.go's CreateCall only populates these when patient_id is empty). For a
-- registered patient, `patients.name`/`age`/`comorbidities` (0002_patient_context.up.sql)
-- remain the source of truth and these columns stay null -- the admin console's list
-- query COALESCEs between the two.

ALTER TABLE calls
    ADD COLUMN patient_name  text,
    ADD COLUMN age           integer,
    ADD COLUMN comorbidities jsonb NOT NULL DEFAULT '[]';

COMMENT ON COLUMN calls.patient_name IS
    'Ad-hoc name for an anonymous call (call-interface''s pre-call form) -- null when '
    'patient_id is set, since patients.name is the source of truth there.';
COMMENT ON COLUMN calls.age IS
    'Ad-hoc age for an anonymous call -- null when patient_id is set (see patients.age).';
COMMENT ON COLUMN calls.comorbidities IS
    'Ad-hoc known conditions for an anonymous call -- empty when patient_id is set '
    '(see patients.comorbidities).';
