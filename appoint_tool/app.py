"""
app.py — The front door of the whole system.

In simple terms: this is the one place the WhatsApp booking tool talks
to. It hands over a request, and gets back a reply — everything that
happens in between (checking details, saving to the database, working
out the wait time) is hidden behind this one simple door.

Usage:
  from app import handle_request
  response = handle_request(payload_dict)
"""

from models import IncomingPayload, BookingResponse, ErrorResult
import booking
import database


# ═══════════════════════════════════════════════════════════════
# MAIN ENTRY POINT
# ═══════════════════════════════════════════════════════════════

def handle_request(payload_dict: dict) -> dict:
    """
    Takes one booking or cancellation request, and gives back one reply.

    Args:
      payload_dict: the request, as sent by the WhatsApp booking tool
        (either "please book me a token" or "please cancel my token")

    Returns:
      The reply — always says what was asked for, and what happened.
    """

    # Step 1: Validate incoming payload
    try:
        payload = IncomingPayload(**payload_dict)
    except Exception as e:
        error_response = BookingResponse(
            action=payload_dict.get("action", "BOOK"),
            result=ErrorResult(
                status="ERROR",
                error_code="INVALID_PAYLOAD",
                message=f"Invalid payload: {str(e)}"
            )
        )
        return error_response.model_dump(mode="json")

    # Step 2: Route to book or cancel
    with database.get_connection() as conn:
        if payload.action == "BOOK":
            result = booking.book(conn, payload)
        else:  # CANCEL
            result = booking.cancel(conn, payload)

    # Step 3: Wrap and return
    response = BookingResponse(action=payload.action, result=result)
    return response.model_dump(mode="json")
