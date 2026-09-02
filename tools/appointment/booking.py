from datetime import datetime, date, timedelta
import tools.appointment.database as db
from models.appointment import BookingConfirmation, CancellationResult, ErrorResult
import psycopg2.errors

SUPPORTED_BOOKING_MODE = "TOKEN"


def _check_supported_mode(hospital) -> ErrorResult | None:
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


def book(conn, payload):
    # Validate hospital
    hospital = db.get_hospital(conn, payload.hospital_id)
    if not hospital:
        return ErrorResult(status="ERROR", error_code="HOSPITAL_NOT_FOUND",
                           message=f"Hospital {payload.hospital_id} not found.")
    mode_error = _check_supported_mode(hospital)
    if mode_error:
        return mode_error

    # Validate doctor
    doctor = db.get_doctor(conn, payload.doctor_id, payload.hospital_id)
    if not doctor:
        return ErrorResult(status="ERROR", error_code="DOCTOR_NOT_FOUND",
                           message=f"Doctor {payload.doctor_id} not found or inactive.")

    # Validate date — never silently default to today; the patient must have
    # been asked, and whatever they said must resolve to a real calendar date.
    if not payload.date or not payload.date.strip():
        return ErrorResult(status="ERROR", error_code="DATE_REQUIRED",
                           message="No appointment date was given.")
    try:
        date.fromisoformat(payload.date.strip())
    except ValueError:
        return ErrorResult(status="ERROR", error_code="INVALID_DATE",
                           message=f"'{payload.date}' is not a valid calendar date.")

    # Validate alternate contact number, if the family member has their own
    patient_phone = payload.patient_phone
    if patient_phone:
        patient_phone = _normalize_phone(patient_phone)
        if len(patient_phone) != 10:
            return ErrorResult(
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
        return ErrorResult(
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
            department=payload.department,
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
        department=payload.department,
        hospital_name=hospital["name"],
        hospital_address=hospital.get("address"),
        fee=doctor.get("fee"),
        estimated_time=estimated_time,
    )


def cancel(conn, payload):
    hospital = db.get_hospital(conn, payload.hospital_id)
    if not hospital:
        return ErrorResult(status="ERROR", error_code="HOSPITAL_NOT_FOUND",
                           message=f"Hospital {payload.hospital_id} not found.")
    mode_error = _check_supported_mode(hospital)
    if mode_error:
        return mode_error

    # Find specific family member
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
