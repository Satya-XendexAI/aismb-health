from datetime import datetime
from typing import Literal, Optional, Union
from pydantic import BaseModel


class IncomingPayload(BaseModel):
    action:           Literal["BOOK", "CANCEL"]
    hospital_id:      str
    doctor_id:        str
    department:       str
    patient_name:     str
    patient_phone:    str
    patient_age:      Optional[int] = None
    patient_location: Optional[str] = None
    symptoms:         Optional[str] = None
    date:             Optional[str] = None  # YYYY-MM-DD


class BookingConfirmation(BaseModel):
    status:           Literal["CONFIRMED"]
    token_number:     int
    doctor_name:      str
    department:       str
    hospital_name:    str
    hospital_address: Optional[str]   = None
    fee:              Optional[float] = None
    estimated_time:   datetime


class CancellationResult(BaseModel):
    status:  Literal["CANCELLED", "PATIENT_NOT_FOUND", "NO_ACTIVE_BOOKING"]
    message: str


class ErrorResult(BaseModel):
    status:     Literal["ERROR"]
    error_code: str
    message:    str


class BookingResponse(BaseModel):
    action: Literal["BOOK", "CANCEL"]
    result: Union[BookingConfirmation, CancellationResult, ErrorResult]
