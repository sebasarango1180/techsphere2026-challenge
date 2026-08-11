"""Postgres writes for turns/escalations/call_summaries -- voice-agent writes these
directly rather than proxying through api-gateway (plan §3.1's ownership split: Go owns
documents/calls/patients, this service owns the per-turn/escalation/summary rows
generated during a live call). Schema: infra/postgres/migrations/0001_init.up.sql +
0002_patient_context.up.sql.
"""

import json

import asyncpg

from app.clinical_snapshot import ClinicalSnapshot
from app.config import settings

_pool: asyncpg.Pool | None = None


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(settings.database_url, min_size=1, max_size=5)
    return _pool


async def insert_turn(
    *,
    call_id: str,
    role: str,
    text: str,
    stt_ms: int | None = None,
    retrieval_ms: int | None = None,
    llm_ms: int | None = None,
    tts_ms: int | None = None,
    tokens_in: int | None = None,
    tokens_out: int | None = None,
    retrieved_chunk_ids: list[str] | None = None,
) -> None:
    pool = await get_pool()
    await pool.execute(
        """
        INSERT INTO turns (call_id, role, text, stt_ms, retrieval_ms, llm_ms, tts_ms,
                            tokens_in, tokens_out, retrieved_chunk_ids)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
        """,
        call_id, role, text, stt_ms, retrieval_ms, llm_ms, tts_ms,
        tokens_in, tokens_out, retrieved_chunk_ids or [],
    )


async def insert_escalation(
    *,
    call_id: str,
    level: str,
    rationale: str,
    triggered_by: str,
    cited_documents: list[dict] | None = None,
) -> None:
    pool = await get_pool()
    await pool.execute(
        """
        INSERT INTO escalations (call_id, level, rationale, triggered_by, cited_documents)
        VALUES ($1, $2, $3, $4, $5)
        """,
        call_id, level, rationale, triggered_by, json.dumps(cited_documents or []),
    )


async def upsert_clinical_snapshot(call_id: str, snapshot: ClinicalSnapshot) -> None:
    """Persists the CURRENT full running snapshot after every turn -- not just at call
    end (plan §2.10's "context at any point in time"). COALESCE on every column so a
    concurrent/out-of-order write can never null out a field this call already knows,
    even though app/clinical_snapshot.py's merge() already does the same thing in
    memory -- this is the durable half of that, and what lets a call survive a
    reconnect or an agent process restart without losing what was already learned.
    """
    pool = await get_pool()
    await pool.execute(
        """
        INSERT INTO call_summaries (call_id, pain_nrs, fever_c, mobility, wound, appetite, sleep, updated_at)
        VALUES ($1, $2, $3, $4::mobility_level, $5::wound_status, $6::appetite_level, $7::sleep_quality, now())
        ON CONFLICT (call_id) DO UPDATE SET
            pain_nrs   = COALESCE(EXCLUDED.pain_nrs, call_summaries.pain_nrs),
            fever_c    = COALESCE(EXCLUDED.fever_c, call_summaries.fever_c),
            mobility   = COALESCE(EXCLUDED.mobility, call_summaries.mobility),
            wound      = COALESCE(EXCLUDED.wound, call_summaries.wound),
            appetite   = COALESCE(EXCLUDED.appetite, call_summaries.appetite),
            sleep      = COALESCE(EXCLUDED.sleep, call_summaries.sleep),
            updated_at = now()
        """,
        call_id, snapshot.pain_nrs, snapshot.fever_c, snapshot.mobility,
        snapshot.wound, snapshot.appetite, snapshot.sleep,
    )


async def finalize_call_summary(
    *,
    call_id: str,
    procedure: str | None,
    symptoms_reported: str | None,
    decision: str | None,
    references: list[dict] | None,
    next_steps: str | None,
) -> None:
    """The end-of-call pass -- fills in the narrative fields the live per-turn
    upsert_clinical_snapshot() never touches. COALESCE here too: if this somehow raced
    with a final upsert_clinical_snapshot() call, neither should be able to blank out
    what the other wrote.
    """
    pool = await get_pool()
    await pool.execute(
        """
        INSERT INTO call_summaries (call_id, procedure, symptoms_reported, decision, "references", next_steps, updated_at)
        VALUES ($1, $2, $3, $4, $5::jsonb, $6, now())
        ON CONFLICT (call_id) DO UPDATE SET
            procedure         = COALESCE(EXCLUDED.procedure, call_summaries.procedure),
            symptoms_reported = COALESCE(EXCLUDED.symptoms_reported, call_summaries.symptoms_reported),
            decision          = COALESCE(EXCLUDED.decision, call_summaries.decision),
            "references"      = COALESCE(EXCLUDED."references", call_summaries."references"),
            next_steps        = COALESCE(EXCLUDED.next_steps, call_summaries.next_steps),
            updated_at        = now()
        """,
        call_id, procedure, symptoms_reported, decision, json.dumps(references or []), next_steps,
    )


async def finalize_triage(
    *,
    call_id: str,
    level: str,
    rationale: str,
    confidence: float | None,
    missing_info: list[str] | None,
    pathology_assessment: str | None = None,
    pathology_evidence: list[dict] | None = None,
) -> None:
    """The authoritative end-of-call classification -- see
    infra/postgres/migrations/0003_final_triage.up.sql's comment for why this is
    computed once at call end (over the full transcript) rather than incrementally per
    turn like upsert_clinical_snapshot's six signals. Separate from the real-time
    escalations table: a mid-call rule-layer hit still writes its own escalations row
    immediately (app/main.py's on_user_turn_completed, unaffected by this), this is the
    final, whole-call assessment recorded once the six-topic script is complete.

    pathology_assessment/pathology_evidence (infra/postgres/migrations/
    0004_pathology_validation.up.sql): the KB-grounded read on whether the reported
    symptoms match a normal recovery or a possible complication, with the specific
    chunks backing it -- same cited_documents shape as insert_escalation.
    """
    pool = await get_pool()
    await pool.execute(
        """
        INSERT INTO call_summaries (call_id, final_triage, triage_rationale, triage_confidence,
                                     missing_info, pathology_assessment, pathology_evidence, updated_at)
        VALUES ($1, $2::triage_level, $3, $4, $5::jsonb, $6, $7::jsonb, now())
        ON CONFLICT (call_id) DO UPDATE SET
            final_triage          = COALESCE(EXCLUDED.final_triage, call_summaries.final_triage),
            triage_rationale      = COALESCE(EXCLUDED.triage_rationale, call_summaries.triage_rationale),
            triage_confidence     = COALESCE(EXCLUDED.triage_confidence, call_summaries.triage_confidence),
            missing_info          = COALESCE(EXCLUDED.missing_info, call_summaries.missing_info),
            pathology_assessment  = COALESCE(EXCLUDED.pathology_assessment, call_summaries.pathology_assessment),
            pathology_evidence    = COALESCE(EXCLUDED.pathology_evidence, call_summaries.pathology_evidence),
            updated_at            = now()
        """,
        call_id, level, rationale, confidence, json.dumps(missing_info or []),
        pathology_assessment, json.dumps(pathology_evidence or []),
    )


async def fetch_latest_snapshot_for_patient(patient_id: str, exclude_call_id: str) -> dict | None:
    """The most recent PRIOR call's clinical snapshot for this patient -- cross-call
    continuity (plan §2.10): a day-3 check-in should know what day-1 reported. Returns
    None for a patient's first call, which is the common/expected case, not an error.
    """
    pool = await get_pool()
    row = await pool.fetchrow(
        """
        SELECT c.postop_day, cs.pain_nrs, cs.fever_c, cs.mobility, cs.wound, cs.appetite, cs.sleep
        FROM call_summaries cs
        JOIN calls c ON c.id = cs.call_id
        WHERE c.patient_id = $1 AND c.id != $2
        ORDER BY c.started_at DESC
        LIMIT 1
        """,
        patient_id, exclude_call_id,
    )
    return dict(row) if row else None


async def mark_call_completed(call_id: str) -> None:
    pool = await get_pool()
    await pool.execute(
        "UPDATE calls SET status = 'completed', ended_at = now() WHERE id = $1", call_id
    )
