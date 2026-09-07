from datetime import datetime
from typing import List, Literal, Optional, Union
from pydantic import BaseModel


class IncomingPayload(BaseModel):
    action:               Literal["BOOK", "CANCEL", "RESCHEDULE"]
    hospital_id:          str
    doctor_id:            str
    department:           str
    patient_name:         str
    patient_phone:        Optional[str] = None       # None = same as requester
    patient_age:          Optional[int] = None
    patient_location:     Optional[str] = None
    symptoms:             Optional[str] = None
    date:                 Optional[str] = None        # YYYY-MM-DD
    slot_id:              Optional[str] = None        # SLOT-mode only: which slot to book/reschedule into
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


class SlotBookingConfirmation(BaseModel):
    status:               Literal["CONFIRMED"]
    appointment_id:       str
    patient_name:         str
    relation_to_requester: str
    doctor_name:          str
    department:           str
    hospital_name:        str
    slot_date:            str
    slot_time:            str
    fee:                  Optional[float] = None
    was_rescheduled:      bool = False   # book_slot() leaves default; reschedule_slot() sets True —
                                          # so the LLM says "moved to" not "booked for"


class CancellationResult(BaseModel):
    status:        Literal["CANCELLED", "PATIENT_NOT_FOUND", "NO_ACTIVE_BOOKING"]
    message:       str
    cancelled_for: Optional[str] = None               # whose booking was cancelled


class ErrorResult(BaseModel):
    status:     Literal["ERROR"]
    error_code: str
    message:    str
    candidates: Optional[List[dict]] = None            # AMBIGUOUS_APPOINTMENT: candidates to pick from


class BookingResponse(BaseModel):
    action: Literal["BOOK", "CANCEL", "RESCHEDULE"]
    result: Union[BookingConfirmation, SlotBookingConfirmation, CancellationResult, ErrorResult]
