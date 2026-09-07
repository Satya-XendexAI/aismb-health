-- ═══════════════════════════════════════════════════════════════
-- Generate 10-minute slots for the 6 Mithra doctors, next 7 days
-- ═══════════════════════════════════════════════════════════════
-- Requires migrations/0004_mithra_slot_hospital_setup.sql to have
-- run first (the doctor_id foreign keys must already exist, and
-- their avg_checkin_time/avg_checkout_time must be 09:00/13:00 to
-- match the window generated here).
--
-- All 6 doctors now share one window (09:00-13:00, 4 hours / 10 min
-- = 24 slots/day each), so this is one consolidated INSERT over
-- every doctor rather than 6 near-duplicate blocks — one place to
-- change if the shared window ever needs to shift.
--
-- ON CONFLICT lists all 5 columns of the real unique constraint on
-- slots — verified live: UNIQUE (hospital_id, doctor_id, date, start_time, end_time).
-- A target missing a column makes Postgres reject the insert outright
-- with "no unique or exclusion constraint matching the ON CONFLICT
-- specification".
-- ═══════════════════════════════════════════════════════════════

BEGIN;

INSERT INTO slots (hospital_id, doctor_id, date, start_time, end_time, status)
SELECT
    'mit-lbn',
    doctor_id,
    (CURRENT_DATE + day_offset)::date,
    (time '09:00' + (slot_num * interval '10 minutes'))::time,
    (time '09:00' + ((slot_num + 1) * interval '10 minutes'))::time,
    'AVAILABLE'
FROM
    unnest(ARRAY[
        'dr-mallindra-swamy-lbn',
        'dr-sreekhar-pentamsetty-lbn',
        'dr-vijay-soorampally--lbn',
        'dr-ashwin-kumar-reddy--lbn',
        'dr-kolluri-ashwanth-chowdary-lbn',
        'dr-praveen-changala--lbn'
    ]) AS doctor_id,
    generate_series(0, 6)  AS day_offset,   -- next 7 days
    generate_series(0, 23) AS slot_num      -- 09:00-13:00 / 10 min = 24 slots
ON CONFLICT (hospital_id, doctor_id, date, start_time, end_time) DO NOTHING;

COMMIT;


-- ═══════════════════════════════════════════════════════════════
-- VERIFY
-- ═══════════════════════════════════════════════════════════════

SELECT
    COUNT(*) AS total_slots,
    COUNT(DISTINCT doctor_id) AS doctors,
    COUNT(DISTINCT date) AS days_covered
FROM slots
WHERE hospital_id = 'mit-lbn';
-- Expect: total_slots=1008, doctors=6, days_covered=7   (6 x 24 x 7)

SELECT
    d.name AS doctor,
    d.specialization,
    s.date,
    COUNT(*) AS total_slots
FROM slots s
JOIN doctors d ON s.doctor_id = d.doctor_id
WHERE s.hospital_id = 'mit-lbn'
GROUP BY d.name, d.specialization, s.date
ORDER BY s.date, d.name;
-- Expect: 42 rows (6 doctors x 7 days), 24 slots each
