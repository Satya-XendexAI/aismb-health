-- ═══════════════════════════════════════════════════════════════
-- Live-hospital flag — lets one real WhatsApp Business number be
-- switched between hospitals via a plain UPDATE, no code change,
-- no restart. See docs/pre-deployment-checklist.md item #1/#2 —
-- this is the "same number, testing both hospitals" path; a second
-- real WhatsApp number (phone_number_id-based routing) is a
-- separate, later step for when both hospitals are live at once.
-- ═══════════════════════════════════════════════════════════════

BEGIN;

ALTER TABLE hospitals ADD COLUMN is_live_number BOOLEAN NOT NULL DEFAULT false;

-- Structurally enforces "at most one hospital live at a time" — a partial
-- unique index on the constant value `true` means a second row can never
-- also be true; the second UPDATE in a switch fails loudly if you forget
-- to flip the old one off first, instead of silently leaving two hospitals
-- live (which would make get_live_hospital()'s LIMIT 1 pick an arbitrary one).
CREATE UNIQUE INDEX uq_hospitals_one_live_number
    ON hospitals ((is_live_number))
    WHERE is_live_number = true;

-- Set whichever hospital is live today — change the hospital_id below.
UPDATE hospitals SET is_live_number = true WHERE hospital_id = 'glngs-chn';

COMMIT;


-- ═══════════════════════════════════════════════════════════════
-- VERIFY
-- ═══════════════════════════════════════════════════════════════

SELECT hospital_id, name, is_live_number FROM hospitals ORDER BY hospital_id;
-- Expect exactly one row with is_live_number = true.


-- ═══════════════════════════════════════════════════════════════
-- TO SWITCH LATER — run both statements together:
-- ═══════════════════════════════════════════════════════════════
-- UPDATE hospitals SET is_live_number = false WHERE hospital_id = 'glngs-chn';
-- UPDATE hospitals SET is_live_number = true  WHERE hospital_id = 'mit-lbn';
