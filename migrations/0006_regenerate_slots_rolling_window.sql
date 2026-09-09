-- ═══════════════════════════════════════════════════════════════
-- Regenerate slots for a rolling N-day window — all SLOT-mode
-- hospitals, all active doctors, fully dynamic.
-- ═══════════════════════════════════════════════════════════════
-- Nothing here is hardcoded to one hospital or one doctor's shift
-- hours: the doctor list comes from `doctors` (filtered to hospitals
-- whose `booking_mode = 'SLOT'`), and each doctor's own
-- avg_checkin_time / avg_checkout_time / avg_consultation_minutes
-- columns decide how many slots they get and where they fall.
-- Change a doctor's hours in `doctors` and re-run this — the next
-- window it generates picks up the new hours automatically.
--
-- WINDOW_DAYS below is the one number you might want to change
-- (currently 10 = today + next 9 days). Re-running this safely
-- re-fills any deleted days/doctors — ON CONFLICT DO NOTHING means
-- it never duplicates or overwrites a slot that already exists,
-- so it's also the right script to run on a recurring schedule to
-- keep the rolling window topped up (see docs/pre-deployment-checklist.md
-- item #3 — this is that job).
-- ═══════════════════════════════════════════════════════════════

BEGIN;

INSERT INTO slots (hospital_id, doctor_id, date, start_time, end_time, status)
SELECT
    d.hospital_id,
    d.doctor_id,
    (CURRENT_DATE + day_offset)::date                                              AS date,
    (d.avg_checkin_time
        + (slot_num * (d.avg_consultation_minutes || ' minutes')::interval))::time AS start_time,
    (d.avg_checkin_time
        + ((slot_num + 1) * (d.avg_consultation_minutes || ' minutes')::interval))::time AS end_time,
    'AVAILABLE'
FROM doctors d
JOIN hospitals h
    ON h.hospital_id = d.hospital_id
   AND h.booking_mode = 'SLOT'
CROSS JOIN generate_series(0, 9) AS day_offset               -- 10-day window: today + next 9 days
CROSS JOIN LATERAL generate_series(
    0,
    GREATEST(
        (EXTRACT(EPOCH FROM (d.avg_checkout_time - d.avg_checkin_time))
            / 60 / NULLIF(d.avg_consultation_minutes, 0))::int - 1,
        -1   -- a misconfigured doctor (0-min consults, or checkout <= checkin)
             -- yields an empty generate_series, i.e. zero slots — skipped safely,
             -- not an error that would abort the whole batch
    )
) AS slot_num
WHERE d.is_active = true
ON CONFLICT (hospital_id, doctor_id, date, start_time, end_time) DO NOTHING;

COMMIT;


-- ═══════════════════════════════════════════════════════════════
-- VERIFY
-- ═══════════════════════════════════════════════════════════════

SELECT
    h.hospital_id,
    h.name AS hospital,
    COUNT(*)                    AS total_slots,
    COUNT(DISTINCT s.doctor_id) AS doctors,
    COUNT(DISTINCT s.date)      AS days_covered,
    MIN(s.date)                 AS first_day,
    MAX(s.date)                 AS last_day
FROM slots s
JOIN hospitals h ON h.hospital_id = s.hospital_id
WHERE h.booking_mode = 'SLOT'
GROUP BY h.hospital_id, h.name
ORDER BY h.name;
-- Expect one row per SLOT-mode hospital, days_covered = 10,
-- first_day = today, last_day = today + 9.
