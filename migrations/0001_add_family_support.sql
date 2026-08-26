-- ═══════════════════════════════════════════════════════════════
-- MIGRATION: Add Family Member Support to patients table
-- Date: 2026-08-25
-- Run this in Supabase SQL Editor
-- ═══════════════════════════════════════════════════════════════

-- Step 1: Add new columns
ALTER TABLE patients
    ADD COLUMN IF NOT EXISTS requested_by_phone     VARCHAR(20),
    ADD COLUMN IF NOT EXISTS relation_to_requester   VARCHAR(50) NOT NULL DEFAULT 'self';

-- Step 2: Backfill existing rows (they were all self-bookings)
UPDATE patients
SET requested_by_phone = phone
WHERE requested_by_phone IS NULL;

-- Step 3: Make requested_by_phone NOT NULL after backfill
ALTER TABLE patients
    ALTER COLUMN requested_by_phone SET NOT NULL;

-- Step 4: Index for fast family lookups
CREATE INDEX IF NOT EXISTS idx_patients_requested_by_phone
    ON patients(hospital_id, requested_by_phone);

-- Step 5: Unique constraint to prevent duplicate family members
CREATE UNIQUE INDEX IF NOT EXISTS uq_patients_family_member
    ON patients(hospital_id, requested_by_phone, (LOWER(name)));

-- Step 6: Verify
SELECT column_name, data_type, column_default, is_nullable
FROM information_schema.columns
WHERE table_name = 'patients'
ORDER BY ordinal_position;
