-- ═══════════════════════════════════════════════════════════════
-- Mithra Hospital, L.B. Nagar — SLOT-mode hospital + 6 real doctors
-- ═══════════════════════════════════════════════════════════════
-- Not yet applied to this database as of this file's creation —
-- verified live: no 'mit-lbn' row exists in hospitals/doctors/slots.
-- Columns used here (hospital_id, name, booking_mode, address, city
-- on hospitals; doctor_id, hospital_id, name, avg_checkin_time,
-- avg_checkout_time, avg_consultation_minutes, fee, specialization,
-- is_active on doctors) were all confirmed to exist on the live
-- tables before writing this.
-- ═══════════════════════════════════════════════════════════════

BEGIN;

-- ─── Hospital (SLOT mode) ────────────────────────────────────
INSERT INTO hospitals (hospital_id, name, city, address, booking_mode)
VALUES (
    'mit-lbn',
    'Mithra Hospital, L.B. Nagar',
    'Hyderabad',
    'L.B. Nagar, Hyderabad',
    'SLOT'
)
ON CONFLICT (hospital_id) DO NOTHING;


-- ─── 6 doctors ────────────────────────────────────────────────
-- All 6 share the same window: 09:00-13:00, 10-min consultations
-- (4 hours / 10 min = 24 slots/day each — see 0005 for the matching
-- slot-generation change).
-- CARDIOLOGY (3), ORTHOPAEDICS (2), NEUROLOGY (1)

INSERT INTO doctors (
    doctor_id, hospital_id, name,
    avg_checkin_time, avg_checkout_time,
    avg_consultation_minutes, fee, specialization, is_active
) VALUES
(
    'dr-mallindra-swamy-lbn', 'mit-lbn', 'Mallindra Swamy',
    '09:00:00', '13:00:00', 10, 800.00, 'CARDIOLOGY', true
),
(
    'dr-sreekhar-pentamsetty-lbn', 'mit-lbn', 'SREEKHAR PENTAMSETTY',
    '09:00:00', '13:00:00', 10, 800.00, 'CARDIOLOGY', true
),
(
    'dr-vijay-soorampally--lbn', 'mit-lbn', 'VIJAY SOORAMPALLY',
    '09:00:00', '13:00:00', 10, 800.00, 'CARDIOLOGY', true
),
(
    'dr-ashwin-kumar-reddy--lbn', 'mit-lbn', 'Ashwin Kumar Reddy',
    '09:00:00', '13:00:00', 10, 800.00, 'ORTHOPAEDICS', true
),
(
    'dr-kolluri-ashwanth-chowdary-lbn', 'mit-lbn', 'Kolluri Ashwanth Chowdary',
    '09:00:00', '13:00:00', 10, 800.00, 'ORTHOPAEDICS', true
),
(
    'dr-praveen-changala--lbn', 'mit-lbn', 'Praveen Changala',
    '09:00:00', '13:00:00', 10, 800.00, 'NEUROLOGY', true
)
ON CONFLICT (doctor_id) DO NOTHING;

COMMIT;


-- ═══════════════════════════════════════════════════════════════
-- VERIFY
-- ═══════════════════════════════════════════════════════════════

SELECT hospital_id, name, city, booking_mode
FROM hospitals WHERE hospital_id = 'mit-lbn';
-- Expect: 1 row

SELECT doctor_id, name, specialization, avg_checkin_time, avg_checkout_time, fee
FROM doctors WHERE hospital_id = 'mit-lbn'
ORDER BY avg_checkin_time, name;
-- Expect: 6 rows
