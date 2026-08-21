"""
models.py — The "forms" this system uses.

In simple terms: this file describes exactly what information must come
IN when someone wants to book or cancel an appointment, and exactly what
information goes OUT as the reply. Think of each class below as a
printed form with fixed fields — nothing more, nothing less.
"""

from datetime import datetime
from typing import Literal, Optional, Union
from pydantic import BaseModel


# ═══════════════════════════════════════════════════════════════
# INPUT CONTRACT (received from colleague's orchestrator)
# ═══════════════════════════════════════════════════════════════

class IncomingPayload(BaseModel):
    """
    The "request form" — everything we need from WhatsApp to either
    book a new token for a patient, or cancel one they already have.
    """
    action: Literal["BOOK", "CANCEL"]
    hospital_id: str                         # TEXT id in the DB, e.g. "glngs-chn" — not a UUID
    doctor_id: str                           # TEXT id in the DB, e.g. "dr-selvakumar-k-chn"
    department: str                          # required for BOOK
    patient_name: str
    patient_phone: str
    patient_age: Optional[int] = None
    patient_location: Optional[str] = None
    symptoms: Optional[str] = None           # stored in patients.diagnosis
    date: Optional[str] = None               # YYYY-MM-DD; defaults to today if omitted


# ═══════════════════════════════════════════════════════════════
# OUTPUT CONTRACT (sent back to colleague's orchestrator)
# ═══════════════════════════════════════════════════════════════

class BookingConfirmation(BaseModel):
    """
    The "good news" reply — sent when a booking works. Tells the patient
    their token number, which doctor, the fee, and roughly what time
    they'll be seen.
    """
    status: Literal["CONFIRMED"]
    token_number: int
    doctor_name: str
    department: str
    hospital_name: str
    hospital_address: Optional[str] = None
    fee: Optional[float] = None
    estimated_time: datetime


class CancellationResult(BaseModel):
    """
    The reply sent after someone tries to cancel a booking — says
    whether it worked, or explains why it couldn't (e.g. nothing to
    cancel, or we don't recognize this patient).
    """
    status: Literal["CANCELLED", "PATIENT_NOT_FOUND", "NO_ACTIVE_BOOKING"]
    message: str


class ErrorResult(BaseModel):
    """
    The "something went wrong" reply — sent when a request can't be
    completed at all, e.g. the hospital or doctor doesn't exist.
    """
    status: Literal["ERROR"]
    error_code: str            # e.g. "HOSPITAL_NOT_FOUND", "DOCTOR_NOT_FOUND"
    message: str


class BookingResponse(BaseModel):
    """
    The outer envelope every reply is sent in — always says which
    action was requested (BOOK or CANCEL), and holds whichever one of
    the three reply types above actually applies.
    """
    action: Literal["BOOK", "CANCEL"]
    result: Union[BookingConfirmation, CancellationResult, ErrorResult]
