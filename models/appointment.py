from datetime import datetime
from typing import Literal, Optional, Union
from pydantic import BaseModel


class IncomingPayload(BaseModel):
    action:               Literal["BOOK", "CANCEL"]
    hospital_id:          str
    doctor_id:            str
    department:           str
    patient_name:         str
    patient_phone:        Optional[str] = None       # None = same as requester
    patient_age:          Optional[int] = None
    patient_location:     Optional[str] = None
    symptoms:             Optional[str] = None
    date:                 Optional[str] = None        # YYYY-MM-DD
    requester_phone:      str                         # WhatsApp sender (set by core.py)
    relation_to_requester: str = "self"               # free text: "wife", "father", etc.


class BookingConfirmation(BaseModel):
    status:               Literal["CONFIRMED"]
    token_number:         int
    patient_name:         str                         # who the booking is for
    relation_to_requester: str                        # their relation to sender
    doctor_name:          str
    department:           str
    hospital_name:        str
    hospital_address:     Optional[str]   = None
    fee:                  Optional[float] = None
    estimated_time:       datetime


class CancellationResult(BaseModel):
    status:        Literal["CANCELLED", "PATIENT_NOT_FOUND", "NO_ACTIVE_BOOKING"]
    message:       str
    cancelled_for: Optional[str] = None               # whose booking was cancelled


class ErrorResult(BaseModel):
    status:     Literal["ERROR"]
    error_code: str
    message:    str


class BookingResponse(BaseModel):
    action: Literal["BOOK", "CANCEL"]
    result: Union[BookingConfirmation, CancellationResult, ErrorResult]
