-- Adds pathology validation to the end-of-call classification (0003_final_triage.up.sql):
-- whether the patient's reported symptoms, cross-checked against the given knowledge
-- base, are consistent with their known procedure/category -- with the specific KB
-- chunks (document/page) supporting that assessment, same citation shape already used
-- by escalations.cited_documents (plan §2.4's "caller must know which document and
-- part of the document a result came from").

ALTER TABLE call_summaries
    ADD COLUMN pathology_assessment text,
    ADD COLUMN pathology_evidence   jsonb NOT NULL DEFAULT '[]';

COMMENT ON COLUMN call_summaries.pathology_assessment IS
    'End-of-call RAG-grounded assessment: are the reported symptoms consistent with the '
    'patient''s known procedure/category per the knowledge base? Computed alongside '
    'final_triage in the same end-of-call pass (app/main.py''s summarize_call), grounded '
    'in KB chunks retrieved using the call transcript, not the live per-turn retrieval.';

COMMENT ON COLUMN call_summaries.pathology_evidence IS
    '[{"chunk_id","document_id","page"}, ...] -- the specific KB excerpts backing '
    'pathology_assessment, same shape as escalations.cited_documents.';
