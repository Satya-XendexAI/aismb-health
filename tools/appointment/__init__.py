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
