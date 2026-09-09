from models.appointment import IncomingPayload, BookingResponse, ErrorResult
from tools.appointment import booking, database


def handle_request(payload_dict: dict) -> dict:
    try:
        payload = IncomingPayload(**payload_dict)
    except Exception as e:
        return BookingResponse(
            action=payload_dict.get("action", "BOOK"),
            result=ErrorResult(
                status="ERROR",
                error_code="INVALID_PAYLOAD",
                message=f"Invalid payload: {e}",
            ),
        ).model_dump(mode="json")

    with database.get_connection() as conn:
        if payload.action == "BOOK":
            result = booking.book(conn, payload)
        elif payload.action == "CANCEL":
            result = booking.cancel(conn, payload)
        elif payload.action == "RESCHEDULE":
            result = booking.reschedule(conn, payload)
        else:
            # Unreachable while IncomingPayload.action stays a 3-value
            # Literal — kept explicit so a future 4th action fails loudly
            # instead of silently falling through to the wrong branch.
            result = ErrorResult(status="ERROR", error_code="UNKNOWN_ACTION",
                                 message=f"Unrecognized action: {payload.action}")

    return BookingResponse(action=payload.action, result=result).model_dump(mode="json")


def list_appointments(hospital_id: str, requester_phone: str, patient_name: str | None = None) -> dict:
    with database.get_connection() as conn:
        rows = database.list_active_appointments(conn, requester_phone, hospital_id, patient_name)
    return {"appointments": rows}


def list_available_slots(hospital_id: str, doctor_id: str, date: str, offset: int = 0) -> dict:
    with database.get_connection() as conn:
        rows, has_more = database.find_available_slots(conn, doctor_id, hospital_id, date, offset=offset)
    slots = [{**row, "slot_id": str(row["slot_id"])} for row in rows]
    return {
        "slots": slots,
        "has_more": has_more,
        # exact cursor for "show me more" — never left for the LLM to compute itself
        "next_offset": offset + len(slots) if has_more else None,
    }
