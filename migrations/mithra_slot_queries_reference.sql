-- ═══════════════════════════════════════════════════════════════
-- REFERENCE ONLY — not a migration, nothing here writes data.
-- Run these individually as needed once 0004 and 0005 have applied.
-- All patient columns here appear only in SELECT, never filtered
-- on, so unlike the (still-broken, not included here) book_slot /
-- cancel_appointment / reschedule_appointment functions, nothing
-- in this file depends on requested_by_phone / relation_to_requester.
-- ═══════════════════════════════════════════════════════════════


-- ═══ QUERY 1: Next 5 available slots for one doctor ═══
SELECT
    slot_id, doctor_id, date, start_time, end_time,
    TO_CHAR(date, 'Dy, DD Mon') AS formatted_date,
    TO_CHAR(start_time, 'HH12:MI AM') AS formatted_time
FROM slots
WHERE doctor_id = 'dr-mallindra-swamy-lbn'      -- change doctor here
  AND hospital_id = 'mit-lbn'
  AND status = 'AVAILABLE'
  AND (
    date > CURRENT_DATE
    OR (date = CURRENT_DATE AND start_time > CURRENT_TIME)
  )
ORDER BY date, start_time
LIMIT 5;


-- ═══ QUERY 2: Available doctors by specialty ═══
SELECT
    d.doctor_id, d.name,
    d.avg_checkin_time AS shift_start, d.avg_checkout_time AS shift_end,
    d.fee,
    COUNT(s.slot_id) AS available_slots_today
FROM doctors d
LEFT JOIN slots s ON s.doctor_id = d.doctor_id
                  AND s.date = CURRENT_DATE
                  AND s.status = 'AVAILABLE'
WHERE d.hospital_id = 'mit-lbn'
  AND UPPER(d.specialization) = 'CARDIOLOGY'    -- change specialty here
  AND d.is_active = true
GROUP BY d.doctor_id, d.name, d.avg_checkin_time, d.avg_checkout_time, d.fee
ORDER BY d.avg_checkin_time;


-- ═══ QUERY 3: All available slots for a specific date ═══
SELECT
    d.name AS doctor, d.specialization,
    s.start_time, s.end_time, s.slot_id
FROM slots s
JOIN doctors d ON s.doctor_id = d.doctor_id
WHERE s.hospital_id = 'mit-lbn'
  AND s.date = CURRENT_DATE + INTERVAL '1 day'   -- change date here
  AND s.status = 'AVAILABLE'
ORDER BY s.start_time, d.name;


-- ═══ QUERY 4: Doctor's schedule for a specific date (booked patients) ═══
SELECT
    s.start_time, s.end_time, s.status AS slot_status,
    p.name AS patient_name, p.phone AS patient_phone, p.age,
    p.diagnosis AS symptoms, a.status AS appointment_status
FROM slots s
LEFT JOIN appointments a ON a.slot_id = s.slot_id AND a.status = 'BOOKED'
LEFT JOIN patients p ON a.patient_id = p.patient_id
WHERE s.doctor_id = 'dr-mallindra-swamy-lbn'     -- change doctor here
  AND s.hospital_id = 'mit-lbn'
  AND s.date = CURRENT_DATE + INTERVAL '1 day'   -- change date here
ORDER BY s.start_time;


-- ═══ QUERY 5: Occupancy summary, all doctors, next 7 days ═══
SELECT
    d.name AS doctor, d.specialization, s.date,
    COUNT(*) AS total_slots,
    COUNT(*) FILTER (WHERE s.status = 'AVAILABLE') AS available,
    COUNT(*) FILTER (WHERE s.status = 'BOOKED') AS booked,
    ROUND(100.0 * COUNT(*) FILTER (WHERE s.status = 'BOOKED') / COUNT(*), 1) AS occupancy_pct
FROM slots s
JOIN doctors d ON s.doctor_id = d.doctor_id
WHERE s.hospital_id = 'mit-lbn'
  AND s.date BETWEEN CURRENT_DATE AND CURRENT_DATE + INTERVAL '7 days'
GROUP BY d.name, d.specialization, s.date
ORDER BY s.date, d.name;


-- ═══ QUERY 6: Find one slot by exact time (to get its slot_id) ═══
SELECT slot_id, status
FROM slots
WHERE doctor_id = 'dr-mallindra-swamy-lbn'       -- change doctor here
  AND hospital_id = 'mit-lbn'
  AND date = '2026-09-05'                         -- change date here
  AND start_time = '09:30:00';                    -- change time here
