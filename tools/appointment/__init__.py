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
        else:
            result = booking.cancel(conn, payload)

    return BookingResponse(action=payload.action, result=result).model_dump(mode="json")


def list_appointments(hospital_id: str, requester_phone: str, patient_name: str | None = None) -> dict:
    with database.get_connection() as conn:
        rows = database.list_active_appointments(conn, requester_phone, hospital_id, patient_name)
    return {"appointments": rows}
