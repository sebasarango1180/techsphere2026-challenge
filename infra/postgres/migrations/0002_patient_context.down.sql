-- Reverse of 0002_patient_context.up.sql. Postgres has no `ALTER TYPE ... DROP VALUE`,
-- so removing 'third_party' means recreating turn_role -- guarded to fail loudly rather
-- than silently discard data if any turn actually used it.

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM turns WHERE role = 'third_party') THEN
        RAISE EXCEPTION 'cannot roll back 0002: turns.role has rows using third_party';
    END IF;
END $$;

ALTER TABLE call_summaries
    DROP CONSTRAINT IF EXISTS call_summaries_pain_nrs_range,
    DROP COLUMN IF EXISTS pain_nrs,
    DROP COLUMN IF EXISTS fever_c,
    DROP COLUMN IF EXISTS mobility,
    DROP COLUMN IF EXISTS wound,
    DROP COLUMN IF EXISTS appetite,
    DROP COLUMN IF EXISTS sleep,
    DROP COLUMN IF EXISTS updated_at;

DROP INDEX IF EXISTS idx_calls_patient_postop;
ALTER TABLE calls DROP COLUMN IF EXISTS postop_day;

ALTER TABLE patients
    DROP COLUMN IF EXISTS category,
    DROP COLUMN IF EXISTS age,
    DROP COLUMN IF EXISTS gender,
    DROP COLUMN IF EXISTS comorbidities,
    DROP COLUMN IF EXISTS national_id,
    DROP COLUMN IF EXISTS address,
    DROP COLUMN IF EXISTS city,
    DROP COLUMN IF EXISTS department,
    DROP COLUMN IF EXISTS eps;

DROP TYPE IF EXISTS sleep_quality;
DROP TYPE IF EXISTS appetite_level;
DROP TYPE IF EXISTS wound_status;
DROP TYPE IF EXISTS mobility_level;

ALTER TYPE turn_role RENAME TO turn_role_old;
CREATE TYPE turn_role AS ENUM ('patient', 'agent');
ALTER TABLE turns ALTER COLUMN role TYPE turn_role USING role::text::turn_role;
DROP TYPE turn_role_old;
