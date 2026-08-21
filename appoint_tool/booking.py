"""
booking.py — The actual rules for booking and cancelling appointments.

In simple terms: this file is the "decision maker." It doesn't talk to
the database directly (database.py does that) — it just decides, step
by step, what should happen for a booking or a cancellation request,
and in what order.
"""

from datetime import datetime, date, timedelta
import database as db
from models import BookingConfirmation, CancellationResult, ErrorResult

# This module only implements TOKEN-mode booking. A hospital's actual mode
# always comes from the DB (hospitals.booking_mode) — this constant is not
# a hardcoded business value, it's what this specific module supports.
SUPPORTED_BOOKING_MODE = "TOKEN"


def _check_supported_mode(hospital) -> ErrorResult | None:
    """
    Makes sure this hospital uses the token/queue style of booking that
    this system knows how to handle (as opposed to fixed time-slots,
    which is a different style this system doesn't support yet).
    """
    if hospital["booking_mode"] != SUPPORTED_BOOKING_MODE:
        return ErrorResult(
            status="ERROR",
            error_code="UNSUPPORTED_BOOKING_MODE",
            message=(
                f"This module only supports {SUPPORTED_BOOKING_MODE} mode; "
                f"hospital is configured for {hospital['booking_mode']}."
            ),
        )
    return None


# ═══════════════════════════════════════════════════════════════
# ETA CALCULATION
# ═══════════════════════════════════════════════════════════════

def calculate_eta(session, doctor, patients_ahead):
    """
    Works out roughly what time this patient will actually be seen.

    Formula:
      If doctor has checked in → use actual arrival time
      If doctor hasn't checked in → use booking date + avg_checkin_time
      Then add: patients_ahead × avg_consultation_minutes
    """
    if session["started_at"] is not None:
        anchor_time = session["started_at"]
    else:
        session_date = session["date"] if session.get("date") else date.today()
        if isinstance(session_date, str):
            from datetime import date as date_cls
            session_date = date_cls.fromisoformat(session_date)
        anchor_time = datetime.combine(session_date, doctor["avg_checkin_time"])

    wait_minutes = patients_ahead * doctor["avg_consultation_minutes"]
    return anchor_time + timedelta(minutes=wait_minutes)


# ═══════════════════════════════════════════════════════════════
# BOOK FLOW (TOKEN mode)
# ═══════════════════════════════════════════════════════════════

def book(conn, payload):
    """
    Books a patient a token. Checks the hospital and doctor are real,
    creates the patient's record if they're new, hands out the next
    token number, and works out when they'll roughly be seen.
    """

    # ─── Step 1: Validate hospital exists ─────────────────────
    hospital = db.get_hospital(conn, payload.hospital_id)
    if not hospital:
        return ErrorResult(
            status="ERROR",
            error_code="HOSPITAL_NOT_FOUND",
            message=f"Hospital {payload.hospital_id} not found."
        )

    mode_error = _check_supported_mode(hospital)
    if mode_error:
        return mode_error

    # ─── Step 2: Validate doctor exists, is active, same hospital ──
    doctor = db.get_doctor(conn, payload.doctor_id, payload.hospital_id)
    if not doctor:
        return ErrorResult(
            status="ERROR",
            error_code="DOCTOR_NOT_FOUND",
            message=f"Doctor {payload.doctor_id} not found or inactive."
        )

    # ─── Step 3: Find or create patient ───────────────────────
    patient = db.find_patient(conn, payload.patient_phone, payload.hospital_id)
    if not patient:
        patient = db.insert_patient(
            conn,
            hospital_id=payload.hospital_id,
            name=payload.patient_name,
            phone=payload.patient_phone,
            age=payload.patient_age,
            location=payload.patient_location,
            diagnosis=payload.symptoms,
        )

    # ─── Step 4: Get or create session for the requested date ────────────────
    session = db.get_or_create_today_session(
        conn, payload.doctor_id, payload.hospital_id, payload.date
    )

    # ─── Step 5: Check for duplicate booking ──────────────────
    existing_token = db.find_active_token(
        conn, patient["patient_id"], payload.doctor_id, payload.date
    )
    if existing_token:
        return ErrorResult(
            status="ERROR",
            error_code="DUPLICATE_BOOKING",
            message=f"You already have token #{existing_token['token_number']} with this doctor."
        )

    # ─── Step 6: Insert new token ─────────────────────────────
    token = db.insert_token(
        conn,
        session_id=session["session_id"],
        patient_id=patient["patient_id"],
        doctor_id=payload.doctor_id,
        hospital_id=payload.hospital_id,
        department=payload.department,
    )

    # ─── Step 7: Calculate ETA ────────────────────────────────
    patients_ahead = db.count_patients_ahead(
        conn, session["session_id"], token["token_number"]
    )
    estimated_time = calculate_eta(session, doctor, patients_ahead)

    # ─── Step 8: Return confirmation ──────────────────────────
    return BookingConfirmation(
        status="CONFIRMED",
        token_number=token["token_number"],
        doctor_name=doctor["name"],
        department=payload.department,
        hospital_name=hospital["name"],
        hospital_address=hospital.get("address"),
        fee=doctor.get("fee"),
        estimated_time=estimated_time,
    )


# ═══════════════════════════════════════════════════════════════
# CANCEL FLOW (TOKEN mode)
# ═══════════════════════════════════════════════════════════════

def cancel(conn, payload):
    """
    Cancels a patient's existing token. Checks the hospital is valid,
    finds the patient by their phone number, and cancels whatever token
    they currently have waiting.
    """

    # ─── Step 1: Validate hospital + mode ─────────────────────
    hospital = db.get_hospital(conn, payload.hospital_id)
    if not hospital:
        return ErrorResult(status="ERROR", error_code="HOSPITAL_NOT_FOUND",
                            message=f"Hospital {payload.hospital_id} not found.")
    mode_error = _check_supported_mode(hospital)
    if mode_error:
        return mode_error

    # ─── Step 2: Find patient by phone ────────────────────────
    patient = db.find_patient(conn, payload.patient_phone, payload.hospital_id)
    if not patient:
        return CancellationResult(
            status="PATIENT_NOT_FOUND",
            message="No patient record found for this number."
        )

    # ─── Step 3: Find active token ────────────────────────────
    active = db.find_active_token(
        conn, patient["patient_id"], payload.doctor_id, payload.date
    )
    if not active:
        return CancellationResult(
            status="NO_ACTIVE_BOOKING",
            message="No active booking found to cancel."
        )

    # ─── Step 4: Cancel it ────────────────────────────────────
    db.cancel_token(conn, active["token_id"])

    return CancellationResult(
        status="CANCELLED",
        message=f"Your token #{active['token_number']} has been cancelled."
    )
