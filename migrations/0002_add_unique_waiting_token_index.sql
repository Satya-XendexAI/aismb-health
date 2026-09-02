-- Prevents two WAITING tokens for the same patient in the same doctor
-- session (session_id already encodes hospital+doctor+date, since
-- doctor_sessions has UNIQUE(hospital_id, doctor_id, date)).
CREATE UNIQUE INDEX IF NOT EXISTS uq_tokens_waiting_patient_session
ON tokens (patient_id, session_id)
WHERE status = 'WAITING';
