from datetime import datetime, date, timedelta
import tools.appointment.database as db
from models.appointment import (
    BookingConfirmation, SlotBookingConfirmation, CancellationResult, ErrorResult,
)
import psycopg2.errors


def _normalize_phone(raw: str) -> str:
    """Strip spaces, dashes, +, parentheses; drop a leading '91' country
    code on a 12-digit number. Returns digits only — caller checks length."""
    digits = "".join(ch for ch in raw if ch.isdigit())
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    return digits


def calculate_eta(session, doctor, patients_ahead):
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


def _candidate(appointment) -> dict:
    """One entry of an AMBIGUOUS_APPOINTMENT candidate list."""
    return {
        "appointment_id": str(appointment["appointment_id"]),
        "date": appointment["date"],
        "time": appointment["start_time"],
    }


def _resolve_department(doctor, payload_department) -> str:
    """The doctor's own specialization (from `doctors`) is authoritative —
    payload.department is LLM-supplied free text that can drift from the
    doctor actually being booked (e.g. carried over from an earlier part
    of the conversation about a different specialty). Only falls back to
    the payload value if this doctor has no specialization on record."""
    return doctor.get("specialization") or payload_department


def _resolve_booking_identity(conn, payload):
    """Doctor + date + phone validation, then family-identity resolution —
    identical for every booking mode, so book_token() and book_slot() both
    call this instead of duplicating it. Returns (doctor, patient, error);
    error is set (doctor/patient None) on the first failure."""
    doctor = db.get_doctor(conn, payload.doctor_id, payload.hospital_id)
    if not doctor:
        return None, None, ErrorResult(status="ERROR", error_code="DOCTOR_NOT_FOUND",
                           message=f"Doctor {payload.doctor_id} not found or inactive.")

    # Validate date — never silently default to today; the patient must have
    # been asked, and whatever they said must resolve to a real calendar date.
    if not payload.date or not payload.date.strip():
        return None, None, ErrorResult(status="ERROR", error_code="DATE_REQUIRED",
                           message="No appointment date was given.")
    try:
        date.fromisoformat(payload.date.strip())
    except ValueError:
        return None, None, ErrorResult(status="ERROR", error_code="INVALID_DATE",
                           message=f"'{payload.date}' is not a valid calendar date.")

    # Validate alternate contact number, if the family member has their own
    patient_phone = payload.patient_phone
    if patient_phone:
        patient_phone = _normalize_phone(patient_phone)
        if len(patient_phone) != 10:
            return None, None, ErrorResult(
                status="ERROR", error_code="INVALID_PHONE",
                message=f"'{payload.patient_phone}' is not a valid 10-digit mobile number for {payload.patient_name}.",
            )

    # Find or create patient (family-aware)
    patient = db.find_family_member(
        conn, payload.requester_phone, payload.hospital_id,
        payload.patient_name, payload.relation_to_requester,
    )

    # Self is identified by phone alone (see find_family_member) — if a
    # different name than what's on file was given, don't silently create
    # a second identity or rename the existing one. Ask for clarification.
    relation_norm = (payload.relation_to_requester or "self").strip().lower()
    if patient and relation_norm == "self" and patient["name"].strip().lower() != payload.patient_name.strip().lower():
        return None, None, ErrorResult(
            status="ERROR", error_code="NAME_MISMATCH",
            message=(
                f"This WhatsApp number already has a profile under the name "
                f"'{patient['name']}', but this message says '{payload.patient_name}'."
            ),
        )

    if not patient:
        patient = db.insert_family_member(
            conn,
            hospital_id=payload.hospital_id,
            requester_phone=payload.requester_phone,
            name=payload.patient_name,
            phone=patient_phone or payload.requester_phone,
            relation=payload.relation_to_requester,
            age=payload.patient_age,
            location=payload.patient_location,
            diagnosis=payload.symptoms,
        )

    return doctor, patient, None


# ─────────────────────────────────────────────────────────────────────────
# Dispatchers — every request lands here first. One hospital lookup, one
# mode check, then straight into the mode-specific function. Token-mode
# logic below is unchanged from before this file had two modes; it's just
# been extracted into book_token()/cancel_token() and takes `hospital` as
# a parameter instead of re-fetching it.
# ─────────────────────────────────────────────────────────────────────────

def book(conn, payload):
    hospital = db.get_hospital(conn, payload.hospital_id)
    if not hospital:
        return ErrorResult(status="ERROR", error_code="HOSPITAL_NOT_FOUND",
                           message=f"Hospital {payload.hospital_id} not found.")
    if hospital["booking_mode"] == "SLOT":
        return book_slot(conn, payload, hospital)
    return book_token(conn, payload, hospital)


def cancel(conn, payload):
    hospital = db.get_hospital(conn, payload.hospital_id)
    if not hospital:
        return ErrorResult(status="ERROR", error_code="HOSPITAL_NOT_FOUND",
                           message=f"Hospital {payload.hospital_id} not found.")
    if hospital["booking_mode"] == "SLOT":
        return cancel_slot(conn, payload, hospital)
    return cancel_token(conn, payload, hospital)


def reschedule(conn, payload):
    """No token-mode equivalent — reschedule is slot-only, matching the
    existing prompt instruction that token-mode has no direct reschedule."""
    hospital = db.get_hospital(conn, payload.hospital_id)
    if not hospital:
        return ErrorResult(status="ERROR", error_code="HOSPITAL_NOT_FOUND",
                           message=f"Hospital {payload.hospital_id} not found.")
    if hospital["booking_mode"] != "SLOT":
        return ErrorResult(status="ERROR", error_code="RESCHEDULE_NOT_SUPPORTED",
                           message="This hospital doesn't support direct rescheduling — cancel and book a new appointment instead.")
    return reschedule_slot(conn, payload, hospital)


# ─────────────────────────────────────────────────────────────────────────
# TOKEN mode
# ─────────────────────────────────────────────────────────────────────────

def book_token(conn, payload, hospital):
    doctor, patient, error = _resolve_booking_identity(conn, payload)
    if error:
        return error
    department = _resolve_department(doctor, payload.department)

    # Get or create today's session
    session = db.get_or_create_today_session(
        conn, payload.doctor_id, payload.hospital_id, payload.date
    )

    # Check for duplicate booking
    existing_token = db.find_active_token(
        conn, patient["patient_id"], payload.doctor_id, payload.date
    )
    if existing_token:
        return ErrorResult(
            status="ERROR", error_code="DUPLICATE_BOOKING",
            message=f"{patient['name']} already has token #{existing_token['token_number']} with this doctor.",
        )

    # Create token — the SELECT-then-INSERT above has a race window between
    # two near-simultaneous requests; the DB's unique index is the backstop.
    with conn.cursor() as cur:
        cur.execute("SAVEPOINT before_insert_token")
    try:
        token = db.insert_token(
            conn,
            session_id=session["session_id"],
            patient_id=patient["patient_id"],
            doctor_id=payload.doctor_id,
            hospital_id=payload.hospital_id,
            department=department,
        )
    except psycopg2.errors.UniqueViolation as e:
        if e.diag.constraint_name != "uq_tokens_waiting_patient_session":
            raise
        with conn.cursor() as cur:
            cur.execute("ROLLBACK TO SAVEPOINT before_insert_token")
        return ErrorResult(
            status="ERROR", error_code="DUPLICATE_BOOKING",
            message=f"{patient['name']} already has an active appointment with this doctor today.",
        )

    # Mark patient as recently used
    db.touch_family_member(conn, patient["patient_id"])

    # Calculate ETA
    patients_ahead = db.count_patients_ahead(conn, session["session_id"], token["token_number"])
    estimated_time = calculate_eta(session, doctor, patients_ahead)

    return BookingConfirmation(
        status="CONFIRMED",
        token_number=token["token_number"],
        patient_name=patient["name"],
        relation_to_requester=patient["relation_to_requester"],
        doctor_name=doctor["name"],
        department=department,
        hospital_name=hospital["name"],
        hospital_address=hospital.get("address"),
        fee=doctor.get("fee"),
        estimated_time=estimated_time,
    )


def cancel_token(conn, payload, hospital):
    patient = db.find_family_member(
        conn, payload.requester_phone, payload.hospital_id,
        payload.patient_name, payload.relation_to_requester,
    )
    if not patient:
        return CancellationResult(
            status="PATIENT_NOT_FOUND",
            message=f"No record found for '{payload.patient_name}'.",
        )

    active = db.find_active_token(conn, patient["patient_id"], payload.doctor_id, payload.date)
    if not active:
        return CancellationResult(status="NO_ACTIVE_BOOKING",
                                  message=f"No active booking found for {patient['name']}.")

    db.cancel_token(conn, active["token_id"])
    return CancellationResult(
        status="CANCELLED",
        message=f"Token #{active['token_number']} for {patient['name']} has been cancelled.",
        cancelled_for=patient["name"],
    )


# ─────────────────────────────────────────────────────────────────────────
# SLOT mode
# ─────────────────────────────────────────────────────────────────────────

def book_slot(conn, payload, hospital):
    doctor, patient, error = _resolve_booking_identity(conn, payload)
    if error:
        return error
    department = _resolve_department(doctor, payload.department)

    if not payload.slot_id:
        return ErrorResult(status="ERROR", error_code="SLOT_REQUIRED",
                           message="No slot was selected.")

    existing = db.find_active_appointments(conn, patient["patient_id"], payload.doctor_id, payload.date)
    if existing:
        return ErrorResult(status="ERROR", error_code="DUPLICATE_BOOKING",
                           message=f"{patient['name']} already has an appointment with this doctor on {payload.date}.")

    with conn.cursor() as cur:
        cur.execute("SAVEPOINT before_book_slot")
    slot = db.lock_slot(conn, payload.slot_id, payload.hospital_id)
    if not slot or slot["status"] != "AVAILABLE":
        with conn.cursor() as cur:
            cur.execute("ROLLBACK TO SAVEPOINT before_book_slot")
        return ErrorResult(status="ERROR", error_code="SLOT_UNAVAILABLE",
                           message="That slot is no longer available.")
    if slot["doctor_id"] != payload.doctor_id:
        # slot_id and doctor_id arrive as two independent LLM-supplied
        # arguments — nothing upstream guarantees they refer to the same
        # doctor, so the DB layer verifies it itself here.
        with conn.cursor() as cur:
            cur.execute("ROLLBACK TO SAVEPOINT before_book_slot")
        return ErrorResult(status="ERROR", error_code="SLOT_DOCTOR_MISMATCH",
                           message="That slot doesn't belong to the selected doctor.")

    db.mark_slot_booked(conn, payload.slot_id)
    # Insert using the locked slot's own hospital_id/doctor_id/date (not the
    # payload's) so the appointments->slots composite foreign key always
    # matches — the slot row we just verified is the single source of truth.
    appt = db.insert_appointment(
        conn, hospital_id=slot["hospital_id"], patient_id=patient["patient_id"],
        doctor_id=slot["doctor_id"], slot_id=slot["slot_id"],
        department=department, appointment_date=slot["date"],
    )
    db.touch_family_member(conn, patient["patient_id"])

    return SlotBookingConfirmation(
        status="CONFIRMED", appointment_id=str(appt["appointment_id"]),
        patient_name=patient["name"], relation_to_requester=patient["relation_to_requester"],
        doctor_name=doctor["name"], department=department, hospital_name=hospital["name"],
        hospital_address=hospital.get("address"),
        slot_date=str(slot["date"]), slot_time=str(slot["start_time"]), fee=doctor.get("fee"),
    )


def cancel_slot(conn, payload, hospital):
    patient = db.find_family_member(
        conn, payload.requester_phone, payload.hospital_id,
        payload.patient_name, payload.relation_to_requester,
    )
    if not patient:
        return CancellationResult(
            status="PATIENT_NOT_FOUND",
            message=f"No record found for '{payload.patient_name}'.",
        )

    matches = db.find_active_appointments(conn, patient["patient_id"], payload.doctor_id, payload.date)
    if not matches:
        return CancellationResult(status="NO_ACTIVE_BOOKING",
                                  message=f"No active booking found for {patient['name']}.")
    if len(matches) > 1:
        return ErrorResult(status="ERROR", error_code="AMBIGUOUS_APPOINTMENT",
                           message=f"{patient['name']} has more than one active appointment with this doctor — which one should be cancelled?",
                           candidates=[_candidate(m) for m in matches])
    appointment = matches[0]

    # cancel_appointment_row reads slot_id off the appointment row itself,
    # not from the payload — so the slot freed is always the one actually
    # booked for this appointment, never a separately-supplied argument.
    cancelled = db.cancel_appointment_row(conn, appointment["appointment_id"])
    if cancelled:
        db.mark_slot_available(conn, cancelled["slot_id"])

    return CancellationResult(
        status="CANCELLED",
        message=f"Appointment for {patient['name']} with Dr. {appointment['doctor_name']} on {appointment['date']} has been cancelled.",
        cancelled_for=patient["name"],
    )


def reschedule_slot(conn, payload, hospital):
    doctor = db.get_doctor(conn, payload.doctor_id, payload.hospital_id)
    if not doctor:
        return ErrorResult(status="ERROR", error_code="DOCTOR_NOT_FOUND",
                           message=f"Doctor {payload.doctor_id} not found or inactive.")

    patient = db.find_family_member(
        conn, payload.requester_phone, payload.hospital_id,
        payload.patient_name, payload.relation_to_requester,
    )
    if not patient:
        return CancellationResult(
            status="PATIENT_NOT_FOUND",
            message=f"No record found for '{payload.patient_name}'.",
        )

    # Exactly-one-match rule: reschedule must never guess which appointment
    # the patient means. Zero → nothing to reschedule. Two or more (e.g. two
    # family members with the same doctor) → ask, don't guess.
    matches = db.find_active_appointments(conn, patient["patient_id"], payload.doctor_id)
    if not matches:
        return CancellationResult(status="NO_ACTIVE_BOOKING",
                                  message=f"No active booking found for {patient['name']} with this doctor.")
    if len(matches) > 1:
        return ErrorResult(status="ERROR", error_code="AMBIGUOUS_APPOINTMENT",
                           message=f"{patient['name']} has more than one active appointment with this doctor — which one should be rescheduled?",
                           candidates=[_candidate(m) for m in matches])
    current = matches[0]

    if not payload.slot_id:
        return ErrorResult(status="ERROR", error_code="SLOT_REQUIRED",
                           message="No new slot was selected.")

    with conn.cursor() as cur:
        cur.execute("SAVEPOINT before_reschedule")
    new_slot = db.lock_slot(conn, payload.slot_id, payload.hospital_id)
    if not new_slot or new_slot["status"] != "AVAILABLE":
        with conn.cursor() as cur:
            cur.execute("ROLLBACK TO SAVEPOINT before_reschedule")
        return ErrorResult(status="ERROR", error_code="SLOT_UNAVAILABLE",
                           message="That slot is no longer available.")
    if new_slot["doctor_id"] != current["doctor_id"]:
        # Defense-in-depth alongside the exactly-one-match rule above — that
        # rule solves *which appointment*, this solves *wrong slot argument*.
        with conn.cursor() as cur:
            cur.execute("ROLLBACK TO SAVEPOINT before_reschedule")
        return ErrorResult(status="ERROR", error_code="SLOT_DOCTOR_MISMATCH",
                           message="That slot doesn't belong to the same doctor as the current appointment.")

    # All writes below run in the same transaction as the lock above (no
    # commit/rollback of its own — the caller's connection owns that) so
    # free-old/book-new/update-appointment either all land or none do.
    db.mark_slot_available(conn, current["slot_id"])
    db.mark_slot_booked(conn, payload.slot_id)
    db.update_appointment_slot(conn, current["appointment_id"], new_slot["slot_id"], new_slot["date"])
    db.touch_family_member(conn, patient["patient_id"])

    return SlotBookingConfirmation(
        status="CONFIRMED", appointment_id=str(current["appointment_id"]),
        patient_name=patient["name"], relation_to_requester=patient["relation_to_requester"],
        doctor_name=doctor["name"], department=_resolve_department(doctor, current["department"]),
        hospital_name=hospital["name"],
        hospital_address=hospital.get("address"),
        slot_date=str(new_slot["date"]), slot_time=str(new_slot["start_time"]), fee=doctor.get("fee"),
        was_rescheduled=True,
    )
