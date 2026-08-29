import logging
from datetime import date, datetime, timedelta

import psycopg2.extras

from tools.appointment.database import get_connection, shift_session_start

logger = logging.getLogger(__name__)


def get_delay_preview(delay_minutes: int, doctor_id: str, hospital_id: str) -> dict:
    """Build preview of patients affected by the doctor's self-reported delay."""

    today = date.today().isoformat()
    sql = """
        SELECT
            ds.session_id,
            ds.started_at,
            ds.avg_consultation_minutes,
            d.name          AS doctor_name,
            t.token_id,
            t.token_number,
            p.name          AS patient_name,
            p.phone         AS patient_phone
        FROM doctor_sessions ds
        JOIN doctors d  ON d.doctor_id  = ds.doctor_id
        JOIN tokens  t  ON t.session_id = ds.session_id AND t.status = 'WAITING'
        JOIN patients p ON p.patient_id = t.patient_id
        WHERE ds.doctor_id  = %s
          AND ds.date        = %s
          AND ds.hospital_id = %s
        ORDER BY t.token_number ASC
    """
    with get_connection() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, (str(doctor_id), today, str(hospital_id)))
            rows = cur.fetchall()

    if not rows:
        return {"error": "No waiting patients found in today's session."}

    first      = rows[0]
    session_id = str(first["session_id"])
    doctor_name = first["doctor_name"]
    avg_mins    = first["avg_consultation_minutes"] or 15
    base_time   = first["started_at"] or datetime.now()
    new_start   = base_time + timedelta(minutes=delay_minutes)

    patients = []
    for row in rows:
        token_num = row["token_number"]
        eta       = new_start + timedelta(minutes=(token_num - 1) * avg_mins)
        patients.append({
            "token_id":      str(row["token_id"]),
            "token_number":  token_num,
            "patient_name":  row["patient_name"],
            "patient_phone": row["patient_phone"] or "",
            "estimated_time": eta.strftime("%I:%M %p"),
        })

    return {
        "session_id":    session_id,
        "doctor_name":   doctor_name,
        "delay_minutes": delay_minutes,
        "patients":      patients,
    }


def execute_delay_report(preview: dict, notifier) -> dict:
    """Shift session start in DB and notify all waiting patients."""
    session_id    = preview["session_id"]
    delay_minutes = preview["delay_minutes"]
    doctor_name   = preview["doctor_name"]
    patients      = preview["patients"]

    with get_connection() as conn:
        shift_session_start(conn, session_id, delay_minutes)

    sent = 0
    failed = 0
    for p in patients:
        phone = p["patient_phone"]
        if not phone:
            continue
        msg = (
            f"Hi {p['patient_name']}, Dr. {doctor_name}'s session is running "
            f"~{delay_minutes} mins late. Your updated estimated time is "
            f"{p['estimated_time']}. We apologize for the inconvenience."
        )
        try:
            notifier.send(phone, msg)
            sent += 1
        except Exception as exc:
            logger.warning("Failed to notify %s: %s", p["patient_name"], exc)
            failed += 1

    return {
        "status":             "done",
        "session_shifted_by": delay_minutes,
        "patients_notified":  sent,
        "patients_failed":    failed,
    }
