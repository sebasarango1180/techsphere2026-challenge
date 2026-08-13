-- Reverse of 0001_init.up.sql. golang-migrate uses this for `migrate down` / rollback --
-- keep it a true mirror image of the up migration, dropped in reverse dependency order.

DROP TABLE IF EXISTS call_summaries;
DROP TABLE IF EXISTS escalations;
DROP TABLE IF EXISTS turns;
DROP TABLE IF EXISTS calls;
DROP TABLE IF EXISTS patients;
DROP TABLE IF EXISTS document_versions;
DROP TABLE IF EXISTS documents;

DROP TYPE IF EXISTS escalation_trigger;
DROP TYPE IF EXISTS triage_level;
DROP TYPE IF EXISTS turn_role;
DROP TYPE IF EXISTS call_status;
DROP TYPE IF EXISTS document_version_status;
DROP TYPE IF EXISTS document_status;
