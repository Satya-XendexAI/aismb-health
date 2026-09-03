-- ═══════════════════════════════════════════════════════════════
-- MIGRATION: Correct the family-member unique index definition
-- Date: 2026-09-01
-- Run this in Supabase SQL Editor
--
-- Context: the live index on `patients` was found to already include
-- relation_to_requester and a partial WHERE clause excluding 'self'
-- bookings — different from what 0001_add_family_support.sql documented
-- (3 columns, no WHERE clause). tools/appointment/database.py's INSERT
-- ... ON CONFLICT was written against the 0001 version and therefore
-- failed on Postgres with:
--   "there is no unique or exclusion constraint matching the ON
--    CONFLICT specification"
-- for every new family-member booking. The code has been fixed to match
-- the live index; this migration exists so a FRESH database (built from
-- 0001 alone) ends up with the same, correct index rather than
-- reintroducing this bug.
-- ═══════════════════════════════════════════════════════════════

DROP INDEX IF EXISTS uq_patients_family_member;

CREATE UNIQUE INDEX IF NOT EXISTS uq_patients_family_member
    ON patients (hospital_id, requested_by_phone, (LOWER(name)), (LOWER(relation_to_requester)))
    WHERE (LOWER(relation_to_requester) <> 'self');

-- Verify
SELECT indexname, indexdef FROM pg_indexes
WHERE tablename = 'patients' AND indexname = 'uq_patients_family_member';
