ALTER TABLE call_summaries
    DROP CONSTRAINT IF EXISTS call_summaries_triage_confidence_range,
    DROP COLUMN IF EXISTS final_triage,
    DROP COLUMN IF EXISTS triage_rationale,
    DROP COLUMN IF EXISTS triage_confidence,
    DROP COLUMN IF EXISTS missing_info;
