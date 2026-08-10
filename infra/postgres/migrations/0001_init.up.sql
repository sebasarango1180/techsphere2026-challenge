-- Initial schema. Matches specs/implementation-plan.md §3.1 exactly -- treat that
-- section as the frozen contract; if this file and the plan ever disagree, fix the
-- disagreement, don't silently pick one.
--
-- Applied by golang-migrate, run automatically from api-gateway's startup
-- (services/api-gateway/internal/migrate/migrate.go) against whatever migrations are
-- newer than what's recorded in the `schema_migrations` table -- not by Postgres's own
-- docker-entrypoint-initdb.d (that only ever runs once against an empty volume, which
-- silently stops applying anything the moment a second migration file shows up). Future
-- schema changes need a new 000N_*.up.sql / .down.sql pair, not edits to this one, once
-- anything has shipped against it.
--
-- Ownership split (see plan §3.1): api-gateway (Go) owns documents/document_versions/
-- calls; voice-agent (Python) writes turns/escalations/call_summaries directly during a
-- live call. Both are trusted internal services sharing this DB.

-- gen_random_uuid() is built into Postgres core since v13; no extension needed.

CREATE TYPE document_status AS ENUM ('active', 'deleted');
CREATE TYPE document_version_status AS ENUM ('processing', 'ready', 'failed', 'superseded');
CREATE TYPE call_status AS ENUM ('active', 'completed', 'dropped');
CREATE TYPE turn_role AS ENUM ('patient', 'agent');
CREATE TYPE triage_level AS ENUM ('verde', 'amarillo', 'rojo');
CREATE TYPE escalation_trigger AS ENUM ('model', 'rule', 'both');

-- Knowledge base documents (identity/versioning source of truth; vector-store is just
-- the processing/index engine over these -- see plan §2.4).
CREATE TABLE documents (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    title           text NOT NULL,
    category        text NOT NULL,
    status          document_status NOT NULL DEFAULT 'active',
    current_version integer NOT NULL DEFAULT 1,
    created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE document_versions (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id     uuid NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    version         integer NOT NULL,
    storage_path    text NOT NULL,
    checksum        text NOT NULL,
    status          document_version_status NOT NULL DEFAULT 'processing',
    chunk_count     integer NOT NULL DEFAULT 0,
    processed_at    timestamptz,
    created_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (document_id, version)
);

CREATE INDEX idx_document_versions_document_id ON document_versions(document_id);

-- Optional demo seed from the challenge dataset (perfiles_clinicos_pacientes_silver /
-- perfiles_pacientes_co). Not required for G1-G5; useful for pre-populated demo calls.
CREATE TABLE patients (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    external_ref    text UNIQUE,           -- e.g. paciente_id from the dataset
    name            text NOT NULL,
    procedure       text,
    surgery_date    date,
    created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE calls (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    patient_id      uuid REFERENCES patients(id) ON DELETE SET NULL,
    started_at      timestamptz NOT NULL DEFAULT now(),
    ended_at        timestamptz,
    status          call_status NOT NULL DEFAULT 'active',
    stt_mode        text NOT NULL,          -- 'groq' | 'local', see plan §2.2
    llm_model       text NOT NULL,          -- resolved model tag, logged for G3 evidence
    livekit_room    text NOT NULL UNIQUE
);

CREATE INDEX idx_calls_patient_id ON calls(patient_id);
CREATE INDEX idx_calls_status ON calls(status);

-- Per-turn timing/consumption breakdown -- this table is what makes the README's
-- required P50/P95 latency and token/RAG-query metrics a real SQL query instead of a
-- hand-waved number (plan §3.1, §0). Populate stt_ms/retrieval_ms/llm_ms/tts_ms and
-- tokens_in/out from the first working pipeline, not retrofitted later.
CREATE TABLE turns (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    call_id             uuid NOT NULL REFERENCES calls(id) ON DELETE CASCADE,
    role                turn_role NOT NULL,
    text                text NOT NULL,
    audio_ref           text,
    stt_ms              integer,
    retrieval_ms        integer,
    llm_ms              integer,
    tts_ms              integer,
    tokens_in           integer,
    tokens_out          integer,
    retrieved_chunk_ids text[] NOT NULL DEFAULT '{}',
    created_at          timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_turns_call_id ON turns(call_id);
CREATE INDEX idx_turns_created_at ON turns(created_at);

-- Escalation events. triggered_by records which layer of the hybrid decision logic
-- (plan §2.3) fired: the model's structured classification, the deterministic red-flag
-- rule layer, or both. The rule layer can only escalate, never downgrade -- there is
-- deliberately no "the rule layer suppressed an escalation" case.
CREATE TABLE escalations (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    call_id             uuid NOT NULL REFERENCES calls(id) ON DELETE CASCADE,
    level               triage_level NOT NULL,
    rationale           text NOT NULL,
    triggered_by        escalation_trigger NOT NULL,
    cited_documents     jsonb NOT NULL DEFAULT '[]',
    created_at          timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_escalations_call_id ON escalations(call_id);
CREATE INDEX idx_escalations_level ON escalations(level);

CREATE TABLE call_summaries (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    call_id             uuid NOT NULL UNIQUE REFERENCES calls(id) ON DELETE CASCADE,
    procedure           text,
    symptoms_reported   text,
    decision            text,
    "references"        jsonb NOT NULL DEFAULT '[]',
    next_steps          text,
    created_at          timestamptz NOT NULL DEFAULT now()
);
