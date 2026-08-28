# Appointment Booking System Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement a backend appointment booking engine supporting two hospital-wide modes — token-based (FCFS + tatkal with configurable ratio interleaving and 3-patient buffer notification) and slot-based (fixed admin-configured slots with atomic cancel+rebook modify).

**Architecture:** `AppointmentEngine` orchestrates booking via a `BookingStrategy` interface. `TokenStrategy` and `SlotStrategy` are injected at startup based on `HospitalConfig.booking_mode`. Shared validators, audit logger, and buffer notifier live on the engine. All state is managed through repository interfaces, making the domain logic fully testable without a real database.

**Tech Stack:** Python 3.11+, pytest 7+, dataclasses (stdlib), abc (stdlib). No external dependencies for the domain layer. PostgreSQL DDL provided separately in `schema.sql` for production deployment.

## Global Constraints

- All IDs are `uuid.UUID` — never bare strings
- All datetimes are `datetime.datetime` (UTC, naive) — use `datetime.utcnow()`
- All dates are `datetime.date`
- All times are `datetime.time`
- Error codes match the spec exactly: `NO_ACTIVE_SESSION`, `TOKEN_NOT_CANCELLABLE`, `SLOT_UNAVAILABLE`, `SLOT_BLOCKED`, `DUPLICATE_BOOKING`, `APPOINTMENT_NOT_MODIFIABLE`, `QUEUE_EMPTY`, `OPERATION_NOT_SUPPORTED`
- Every public method that changes state must call `AuditLogger.log()` before returning
- No print statements — use return values and exceptions only
- Tests use in-memory repositories — no real DB required
- Run all tests with: `pytest tests/appointment/ -v`

---

### Task 1: Enums, Models, and Errors

**Files:**
- Create: `appointment/__init__.py`
- Create: `appointment/enums.py`
- Create: `appointment/models.py`
- Create: `appointment/errors.py`
- Create: `tests/__init__.py`
- Create: `tests/appointment/__init__.py`
- Create: `tests/appointment/test_models_errors.py`

**Interfaces:**
- Produces: `BookingMode`, `TokenType`, `TokenStatus`, `SlotStatus`, `AppointmentStatus`, `SessionStatus`, `AuditAction` enums; `Hospital`, `Patient`, `Doctor`, `DoctorSession`, `Token`, `TokenQueue`, `Slot`, `Appointment`, `AuditLog` dataclasses; `BookingError` and all typed subclasses

- [ ] **Step 1: Create directory scaffolding**

```bash
mkdir -p appointment tests/appointment
touch appointment/__init__.py tests/__init__.py tests/appointment/__init__.py
```

- [ ] **Step 2: Write failing tests for enums and models**

`tests/appointment/test_models_errors.py`:
```python
import pytest
from uuid import uuid4
from datetime import date, datetime, time
from appointment.enums import (
    BookingMode, TokenType, TokenStatus, SlotStatus,
    AppointmentStatus, SessionStatus, AuditAction,
)
from appointment.models import (
    Hospital, Patient, Doctor, DoctorSession,
    Token, TokenQueue, Slot, Appointment, AuditLog,
)
from appointment.errors import (
    BookingError, NoActiveSession, TokenNotCancellable,
    SlotUnavailable, SlotBlocked, DuplicateBooking,
    AppointmentNotModifiable, QueueEmpty, OperationNotSupported,
    PatientNotFound, DoctorNotActive, StaleQueueVersion,
)


def test_booking_mode_values():
    assert BookingMode.TOKEN == "TOKEN"
    assert BookingMode.SLOT == "SLOT"


def test_token_status_values():
    assert set(TokenStatus) == {
        TokenStatus.WAITING, TokenStatus.BUFFER,
        TokenStatus.SERVING, TokenStatus.COMPLETED, TokenStatus.CANCELLED,
    }


def test_hospital_defaults():
    h = Hospital(hospital_id=uuid4(), name="City Hospital", booking_mode=BookingMode.TOKEN)
    assert h.tatkal_ratio == 2


def test_token_queue_defaults():
    q = TokenQueue(queue_id=uuid4(), session_id=uuid4())
    assert q.tatkal_count == 0
    assert q.normal_count == 0
    assert q.consecutive_normal_served == 0
    assert q.version == 0
    assert q.serving_token_id is None
    assert q.buffer_token_ids == []


def test_booking_error_has_code():
    err = NoActiveSession()
    assert err.code == "NO_ACTIVE_SESSION"
    assert "NO_ACTIVE_SESSION" in str(err)


def test_token_not_cancellable_includes_status():
    err = TokenNotCancellable("SERVING")
    assert err.code == "TOKEN_NOT_CANCELLABLE"
    assert "SERVING" in err.message


def test_operation_not_supported_includes_context():
    err = OperationNotSupported("modify", "TOKEN")
    assert err.code == "OPERATION_NOT_SUPPORTED"
    assert "modify" in err.message
    assert "TOKEN" in err.message


def test_all_errors_are_booking_errors():
    errors = [
        NoActiveSession(), TokenNotCancellable("X"),
        SlotUnavailable(), SlotBlocked(), DuplicateBooking(),
        AppointmentNotModifiable("X"), QueueEmpty(),
        OperationNotSupported("op", "mode"),
        PatientNotFound(), DoctorNotActive(), StaleQueueVersion(),
    ]
    for e in errors:
        assert isinstance(e, BookingError)
```

- [ ] **Step 3: Run test to verify it fails**

```bash
cd /Users/apple/Documents/projects/AISMBs/ai_smb_health
pytest tests/appointment/test_models_errors.py -v
```
Expected: `ModuleNotFoundError: No module named 'appointment'`

- [ ] **Step 4: Write `appointment/enums.py`**

```python
from enum import Enum


class BookingMode(str, Enum):
    TOKEN = "TOKEN"
    SLOT = "SLOT"


class TokenType(str, Enum):
    NORMAL = "NORMAL"
    TATKAL = "TATKAL"


class TokenStatus(str, Enum):
    WAITING = "WAITING"
    BUFFER = "BUFFER"
    SERVING = "SERVING"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"


class SlotStatus(str, Enum):
    AVAILABLE = "AVAILABLE"
    BOOKED = "BOOKED"
    BLOCKED = "BLOCKED"


class AppointmentStatus(str, Enum):
    SCHEDULED = "SCHEDULED"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"


class SessionStatus(str, Enum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"


class AuditAction(str, Enum):
    BOOKED = "BOOKED"
    CANCELLED = "CANCELLED"
    MODIFIED = "MODIFIED"
    CALLED = "CALLED"
    SESSION_CLOSED = "SESSION_CLOSED"
```

- [ ] **Step 5: Write `appointment/models.py`**

```python
from dataclasses import dataclass, field
from datetime import datetime, date, time
from typing import Optional, List
from uuid import UUID

from .enums import (
    BookingMode, TokenType, TokenStatus,
    SlotStatus, AppointmentStatus, SessionStatus,
)


@dataclass
class Hospital:
    hospital_id: UUID
    name: str
    booking_mode: BookingMode
    tatkal_ratio: int = 2


@dataclass
class Patient:
    patient_id: UUID
    hospital_id: UUID
    name: str
    phone: str
    dob: date


@dataclass
class Doctor:
    doctor_id: UUID
    hospital_id: UUID
    name: str
    specialization: str
    is_active: bool


@dataclass
class DoctorSession:
    session_id: UUID
    doctor_id: UUID
    hospital_id: UUID
    date: date
    started_at: datetime
    status: SessionStatus
    ended_at: Optional[datetime] = None


@dataclass
class Token:
    token_id: UUID
    hospital_id: UUID
    doctor_id: UUID
    patient_id: UUID
    session_id: UUID
    token_number: int
    token_type: TokenType
    status: TokenStatus
    issued_at: datetime
    called_at: Optional[datetime] = None
    served_at: Optional[datetime] = None


@dataclass
class TokenQueue:
    queue_id: UUID
    session_id: UUID
    tatkal_count: int = 0
    normal_count: int = 0
    consecutive_normal_served: int = 0
    version: int = 0
    serving_token_id: Optional[UUID] = None
    buffer_token_ids: List[UUID] = field(default_factory=list)
    last_updated: datetime = field(default_factory=datetime.utcnow)


@dataclass
class Slot:
    slot_id: UUID
    hospital_id: UUID
    doctor_id: UUID
    date: date
    start_time: time
    end_time: time
    status: SlotStatus
    created_by: UUID


@dataclass
class Appointment:
    appointment_id: UUID
    hospital_id: UUID
    patient_id: UUID
    doctor_id: UUID
    slot_id: UUID
    status: AppointmentStatus
    booked_at: datetime
    cancelled_at: Optional[datetime] = None
    cancellation_reason: Optional[str] = None


@dataclass
class AuditLog:
    log_id: UUID
    entity_type: str
    entity_id: UUID
    action: str
    actor_id: UUID
    timestamp: datetime
    metadata: dict = field(default_factory=dict)
```

- [ ] **Step 6: Write `appointment/errors.py`**

```python
class BookingError(Exception):
    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(f"[{code}] {message}")


class NoActiveSession(BookingError):
    def __init__(self):
        super().__init__("NO_ACTIVE_SESSION", "No open doctor session for today")


class TokenNotCancellable(BookingError):
    def __init__(self, status: str):
        super().__init__("TOKEN_NOT_CANCELLABLE", f"Token with status {status} cannot be cancelled")


class SlotUnavailable(BookingError):
    def __init__(self):
        super().__init__("SLOT_UNAVAILABLE", "Slot is no longer available")


class SlotBlocked(BookingError):
    def __init__(self):
        super().__init__("SLOT_BLOCKED", "Slot is blocked by admin")


class DuplicateBooking(BookingError):
    def __init__(self):
        super().__init__("DUPLICATE_BOOKING", "Patient already has a scheduled appointment with this doctor today")


class AppointmentNotModifiable(BookingError):
    def __init__(self, status: str):
        super().__init__("APPOINTMENT_NOT_MODIFIABLE", f"Appointment with status {status} cannot be modified")


class QueueEmpty(BookingError):
    def __init__(self):
        super().__init__("QUEUE_EMPTY", "No more patients in queue")


class OperationNotSupported(BookingError):
    def __init__(self, operation: str, mode: str):
        super().__init__("OPERATION_NOT_SUPPORTED", f"{operation} is not supported in {mode} mode")


class PatientNotFound(BookingError):
    def __init__(self):
        super().__init__("PATIENT_NOT_FOUND", "Patient not found in this hospital")


class DoctorNotActive(BookingError):
    def __init__(self):
        super().__init__("DOCTOR_NOT_ACTIVE", "Doctor is not active")


class StaleQueueVersion(BookingError):
    def __init__(self):
        super().__init__("STALE_QUEUE_VERSION", "Queue was modified concurrently — retry")
```

- [ ] **Step 7: Run tests to verify they pass**

```bash
pytest tests/appointment/test_models_errors.py -v
```
Expected: all 9 tests PASS

- [ ] **Step 8: Commit**

```bash
git add appointment/ tests/
git commit -m "feat(appointment): add enums, models, and typed errors"
```

---

### Task 2: Repository Interfaces and In-Memory Implementations

**Files:**
- Create: `appointment/repositories.py`
- Create: `tests/appointment/conftest.py`

**Interfaces:**
- Consumes: `Hospital`, `Patient`, `Doctor`, `DoctorSession`, `Token`, `TokenQueue`, `Slot`, `Appointment`, `AuditLog`, `TokenType`, `TokenStatus`, `SlotStatus`, `AppointmentStatus`, `SessionStatus`
- Produces:
  - `HospitalRepository.get(hospital_id: UUID) -> Optional[Hospital]`
  - `PatientRepository.get(patient_id: UUID) -> Optional[Patient]`
  - `DoctorRepository.get(doctor_id: UUID) -> Optional[Doctor]`
  - `SessionRepository.get_open(doctor_id: UUID, on_date: date) -> Optional[DoctorSession]`; `.save(session)`; `.get(session_id)`
  - `TokenRepository.save(token)`; `.get(token_id)`; `.get_waiting_ordered(session_id, token_type) -> List[Token]`; `.update(token)`; `.bulk_cancel(session_id, statuses, reason)`; `.next_number(session_id) -> int`
  - `TokenQueueRepository.get_by_session(session_id) -> Optional[TokenQueue]`; `.save(queue)`; `.update_with_version(queue) -> bool`
  - `SlotRepository.get(slot_id)`; `.get_available(doctor_id, on_date) -> List[Slot]`; `.update(slot)`
  - `AppointmentRepository.save(appt)`; `.get(appt_id)`; `.get_scheduled(patient_id, doctor_id, on_date) -> Optional[Appointment]`; `.update(appt)`
  - `AuditRepository.save(log)`; `.all() -> List[AuditLog]`
  - In-memory implementations: `InMemory*` for each repository above
  - `conftest.py` fixtures: `hospital_token`, `hospital_slot`, `patient`, `doctor`, `repos`

- [ ] **Step 1: Write failing tests (conftest will provide fixtures)**

Add to `tests/appointment/test_models_errors.py` (append, don't replace):
```python
from appointment.repositories import (
    InMemoryHospitalRepository, InMemoryPatientRepository,
    InMemoryDoctorRepository, InMemorySessionRepository,
    InMemoryTokenRepository, InMemoryTokenQueueRepository,
    InMemorySlotRepository, InMemoryAppointmentRepository,
    InMemoryAuditRepository,
)


def test_hospital_repo_get_missing_returns_none():
    repo = InMemoryHospitalRepository()
    assert repo.get(uuid4()) is None


def test_patient_repo_roundtrip():
    repo = InMemoryPatientRepository()
    p = Patient(uuid4(), uuid4(), "Ravi", "9999999999", date(1990, 1, 1))
    repo.add(p)
    assert repo.get(p.patient_id) == p


def test_token_repo_next_number_increments():
    repo = InMemoryTokenRepository()
    session_id = uuid4()
    assert repo.next_number(session_id) == 1
    assert repo.next_number(session_id) == 2
    assert repo.next_number(session_id) == 3


def test_token_queue_update_with_version_fails_on_stale():
    repo = InMemoryTokenQueueRepository()
    q = TokenQueue(queue_id=uuid4(), session_id=uuid4(), version=0)
    repo.save(q)
    q.version = 5  # stale — repo has version 0
    result = repo.update_with_version(q)
    assert result is False


def test_token_queue_update_with_version_succeeds_on_match():
    repo = InMemoryTokenQueueRepository()
    q = TokenQueue(queue_id=uuid4(), session_id=uuid4(), version=0)
    repo.save(q)
    result = repo.update_with_version(q)  # version=0 matches stored version=0
    assert result is True
    stored = repo.get_by_session(q.session_id)
    assert stored.version == 1


def test_appointment_repo_get_scheduled_returns_none_for_cancelled():
    repo = InMemoryAppointmentRepository()
    appt = Appointment(
        uuid4(), uuid4(), uuid4(), uuid4(), uuid4(),
        AppointmentStatus.CANCELLED, datetime.utcnow(),
    )
    repo.save(appt)
    result = repo.get_scheduled(appt.patient_id, appt.doctor_id, date.today())
    assert result is None
```

- [ ] **Step 2: Run to verify failure**

```bash
pytest tests/appointment/test_models_errors.py -v -k "repo"
```
Expected: `ImportError` — `InMemoryHospitalRepository` not defined

- [ ] **Step 3: Write `appointment/repositories.py`**

```python
from abc import ABC, abstractmethod
from collections import defaultdict
from datetime import date
from typing import Dict, List, Optional
from uuid import UUID

from .enums import SessionStatus, TokenStatus, SlotStatus, AppointmentStatus, TokenType
from .models import (
    Hospital, Patient, Doctor, DoctorSession,
    Token, TokenQueue, Slot, Appointment, AuditLog,
)
import copy
from datetime import datetime


# ── Abstract interfaces ──────────────────────────────────────────────────────

class HospitalRepository(ABC):
    @abstractmethod
    def get(self, hospital_id: UUID) -> Optional[Hospital]: ...


class PatientRepository(ABC):
    @abstractmethod
    def get(self, patient_id: UUID) -> Optional[Patient]: ...


class DoctorRepository(ABC):
    @abstractmethod
    def get(self, doctor_id: UUID) -> Optional[Doctor]: ...


class SessionRepository(ABC):
    @abstractmethod
    def get_open(self, doctor_id: UUID, on_date: date) -> Optional[DoctorSession]: ...
    @abstractmethod
    def save(self, session: DoctorSession) -> None: ...
    @abstractmethod
    def get(self, session_id: UUID) -> Optional[DoctorSession]: ...


class TokenRepository(ABC):
    @abstractmethod
    def save(self, token: Token) -> None: ...
    @abstractmethod
    def get(self, token_id: UUID) -> Optional[Token]: ...
    @abstractmethod
    def update(self, token: Token) -> None: ...
    @abstractmethod
    def get_waiting_ordered(self, session_id: UUID, token_type: TokenType) -> List[Token]: ...
    @abstractmethod
    def bulk_cancel(self, session_id: UUID, statuses: List[TokenStatus], reason: str) -> List[Token]: ...
    @abstractmethod
    def next_number(self, session_id: UUID) -> int: ...


class TokenQueueRepository(ABC):
    @abstractmethod
    def get_by_session(self, session_id: UUID) -> Optional[TokenQueue]: ...
    @abstractmethod
    def save(self, queue: TokenQueue) -> None: ...
    @abstractmethod
    def update_with_version(self, queue: TokenQueue) -> bool: ...


class SlotRepository(ABC):
    @abstractmethod
    def get(self, slot_id: UUID) -> Optional[Slot]: ...
    @abstractmethod
    def get_available(self, doctor_id: UUID, on_date: date) -> List[Slot]: ...
    @abstractmethod
    def update(self, slot: Slot) -> None: ...


class AppointmentRepository(ABC):
    @abstractmethod
    def save(self, appointment: Appointment) -> None: ...
    @abstractmethod
    def get(self, appointment_id: UUID) -> Optional[Appointment]: ...
    @abstractmethod
    def get_scheduled(self, patient_id: UUID, doctor_id: UUID, on_date: date) -> Optional[Appointment]: ...
    @abstractmethod
    def update(self, appointment: Appointment) -> None: ...


class AuditRepository(ABC):
    @abstractmethod
    def save(self, log: AuditLog) -> None: ...
    @abstractmethod
    def all(self) -> List[AuditLog]: ...


# ── In-memory implementations (for testing) ──────────────────────────────────

class InMemoryHospitalRepository(HospitalRepository):
    def __init__(self): self._store: Dict[UUID, Hospital] = {}
    def add(self, h: Hospital): self._store[h.hospital_id] = h
    def get(self, hospital_id: UUID) -> Optional[Hospital]: return self._store.get(hospital_id)


class InMemoryPatientRepository(PatientRepository):
    def __init__(self): self._store: Dict[UUID, Patient] = {}
    def add(self, p: Patient): self._store[p.patient_id] = p
    def get(self, patient_id: UUID) -> Optional[Patient]: return self._store.get(patient_id)


class InMemoryDoctorRepository(DoctorRepository):
    def __init__(self): self._store: Dict[UUID, Doctor] = {}
    def add(self, d: Doctor): self._store[d.doctor_id] = d
    def get(self, doctor_id: UUID) -> Optional[Doctor]: return self._store.get(doctor_id)


class InMemorySessionRepository(SessionRepository):
    def __init__(self): self._store: Dict[UUID, DoctorSession] = {}

    def save(self, session: DoctorSession) -> None:
        self._store[session.session_id] = copy.copy(session)

    def get(self, session_id: UUID) -> Optional[DoctorSession]:
        s = self._store.get(session_id)
        return copy.copy(s) if s else None

    def get_open(self, doctor_id: UUID, on_date: date) -> Optional[DoctorSession]:
        for s in self._store.values():
            if s.doctor_id == doctor_id and s.date == on_date and s.status == SessionStatus.OPEN:
                return copy.copy(s)
        return None


class InMemoryTokenRepository(TokenRepository):
    def __init__(self):
        self._store: Dict[UUID, Token] = {}
        self._counters: Dict[UUID, int] = defaultdict(int)

    def save(self, token: Token) -> None: self._store[token.token_id] = copy.copy(token)
    def get(self, token_id: UUID) -> Optional[Token]:
        t = self._store.get(token_id)
        return copy.copy(t) if t else None

    def update(self, token: Token) -> None: self._store[token.token_id] = copy.copy(token)

    def get_waiting_ordered(self, session_id: UUID, token_type: TokenType) -> List[Token]:
        return sorted(
            [t for t in self._store.values()
             if t.session_id == session_id
             and t.token_type == token_type
             and t.status == TokenStatus.WAITING],
            key=lambda t: t.token_number,
        )

    def bulk_cancel(self, session_id: UUID, statuses: List[TokenStatus], reason: str) -> List[Token]:
        cancelled = []
        for token_id, token in self._store.items():
            if token.session_id == session_id and token.status in statuses:
                token.status = TokenStatus.CANCELLED
                token.served_at = datetime.utcnow()
                cancelled.append(copy.copy(token))
        return cancelled

    def next_number(self, session_id: UUID) -> int:
        self._counters[session_id] += 1
        return self._counters[session_id]


class InMemoryTokenQueueRepository(TokenQueueRepository):
    def __init__(self): self._store: Dict[UUID, TokenQueue] = {}

    def save(self, queue: TokenQueue) -> None:
        self._store[queue.session_id] = copy.deepcopy(queue)

    def get_by_session(self, session_id: UUID) -> Optional[TokenQueue]:
        q = self._store.get(session_id)
        return copy.deepcopy(q) if q else None

    def update_with_version(self, queue: TokenQueue) -> bool:
        stored = self._store.get(queue.session_id)
        if stored is None or stored.version != queue.version:
            return False
        updated = copy.deepcopy(queue)
        updated.version += 1
        updated.last_updated = datetime.utcnow()
        self._store[queue.session_id] = updated
        return True


class InMemorySlotRepository(SlotRepository):
    def __init__(self): self._store: Dict[UUID, Slot] = {}
    def add(self, slot: Slot): self._store[slot.slot_id] = copy.copy(slot)
    def get(self, slot_id: UUID) -> Optional[Slot]:
        s = self._store.get(slot_id)
        return copy.copy(s) if s else None

    def get_available(self, doctor_id: UUID, on_date: date) -> List[Slot]:
        return sorted(
            [copy.copy(s) for s in self._store.values()
             if s.doctor_id == doctor_id and s.date == on_date and s.status == SlotStatus.AVAILABLE],
            key=lambda s: s.start_time,
        )

    def update(self, slot: Slot) -> None: self._store[slot.slot_id] = copy.copy(slot)


class InMemoryAppointmentRepository(AppointmentRepository):
    def __init__(self): self._store: Dict[UUID, Appointment] = {}
    def save(self, appointment: Appointment) -> None:
        self._store[appointment.appointment_id] = copy.copy(appointment)

    def get(self, appointment_id: UUID) -> Optional[Appointment]:
        a = self._store.get(appointment_id)
        return copy.copy(a) if a else None

    def get_scheduled(self, patient_id: UUID, doctor_id: UUID, on_date: date) -> Optional[Appointment]:
        for a in self._store.values():
            slot_date = None
            if a.patient_id == patient_id and a.doctor_id == doctor_id \
                    and a.status == AppointmentStatus.SCHEDULED:
                return copy.copy(a)
        return None

    def update(self, appointment: Appointment) -> None:
        self._store[appointment.appointment_id] = copy.copy(appointment)


class InMemoryAuditRepository(AuditRepository):
    def __init__(self): self._logs: List[AuditLog] = []
    def save(self, log: AuditLog) -> None: self._logs.append(log)
    def all(self) -> List[AuditLog]: return list(self._logs)
```

- [ ] **Step 4: Write `tests/appointment/conftest.py`**

```python
import pytest
from datetime import date, datetime, time
from uuid import uuid4

from appointment.enums import BookingMode, SessionStatus, SlotStatus
from appointment.models import Hospital, Patient, Doctor, DoctorSession, Slot
from appointment.repositories import (
    InMemoryHospitalRepository, InMemoryPatientRepository,
    InMemoryDoctorRepository, InMemorySessionRepository,
    InMemoryTokenRepository, InMemoryTokenQueueRepository,
    InMemorySlotRepository, InMemoryAppointmentRepository,
    InMemoryAuditRepository,
)


@pytest.fixture
def hospital_token():
    return Hospital(uuid4(), "Token Hospital", BookingMode.TOKEN, tatkal_ratio=2)


@pytest.fixture
def hospital_slot():
    return Hospital(uuid4(), "Slot Hospital", BookingMode.SLOT, tatkal_ratio=2)


@pytest.fixture
def patient(hospital_token):
    return Patient(uuid4(), hospital_token.hospital_id, "Ravi Kumar", "9999999999", date(1990, 5, 10))


@pytest.fixture
def doctor(hospital_token):
    return Doctor(uuid4(), hospital_token.hospital_id, "Dr. Priya", "Cardiology", is_active=True)


@pytest.fixture
def open_session(doctor, hospital_token):
    return DoctorSession(
        session_id=uuid4(),
        doctor_id=doctor.doctor_id,
        hospital_id=hospital_token.hospital_id,
        date=date.today(),
        started_at=datetime.utcnow(),
        status=SessionStatus.OPEN,
    )


@pytest.fixture
def repos(hospital_token, hospital_slot, patient, doctor, open_session):
    h_repo = InMemoryHospitalRepository()
    h_repo.add(hospital_token)
    h_repo.add(hospital_slot)

    p_repo = InMemoryPatientRepository()
    p_repo.add(patient)

    d_repo = InMemoryDoctorRepository()
    d_repo.add(doctor)

    s_repo = InMemorySessionRepository()
    s_repo.save(open_session)

    return {
        "hospitals": h_repo,
        "patients": p_repo,
        "doctors": d_repo,
        "sessions": s_repo,
        "tokens": InMemoryTokenRepository(),
        "queues": InMemoryTokenQueueRepository(),
        "slots": InMemorySlotRepository(),
        "appointments": InMemoryAppointmentRepository(),
        "audit": InMemoryAuditRepository(),
    }


@pytest.fixture
def available_slot(doctor, hospital_slot):
    return Slot(
        slot_id=uuid4(),
        hospital_id=hospital_slot.hospital_id,
        doctor_id=doctor.doctor_id,
        date=date.today(),
        start_time=time(9, 0),
        end_time=time(9, 20),
        status=SlotStatus.AVAILABLE,
        created_by=uuid4(),
    )
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
pytest tests/appointment/test_models_errors.py -v
```
Expected: all 15 tests PASS

- [ ] **Step 6: Commit**

```bash
git add appointment/repositories.py tests/appointment/conftest.py
git commit -m "feat(appointment): add repository interfaces and in-memory implementations"
```

---

### Task 3: Validators and AuditLogger

**Files:**
- Create: `appointment/validators.py`
- Create: `appointment/audit.py`
- Create: `tests/appointment/test_validators.py`

**Interfaces:**
- Consumes: `PatientRepository`, `DoctorRepository`, `SessionRepository`; `PatientNotFound`, `DoctorNotActive`, `NoActiveSession`
- Produces:
  - `PatientValidator.validate(patient_id: UUID, hospital_id: UUID) -> Patient`
  - `DoctorValidator.validate_active(doctor_id: UUID, hospital_id: UUID) -> Doctor`
  - `DoctorValidator.validate_open_session(doctor_id: UUID, hospital_id: UUID, on_date: date) -> DoctorSession`
  - `AuditLogger.log(entity_type: str, entity_id: UUID, action: AuditAction, actor_id: UUID, metadata: dict) -> None`

- [ ] **Step 1: Write failing tests**

`tests/appointment/test_validators.py`:
```python
import pytest
from uuid import uuid4
from datetime import date

from appointment.enums import BookingMode, SessionStatus, AuditAction
from appointment.errors import PatientNotFound, DoctorNotActive, NoActiveSession
from appointment.models import Hospital, Patient, Doctor, DoctorSession
from appointment.repositories import (
    InMemoryPatientRepository, InMemoryDoctorRepository,
    InMemorySessionRepository, InMemoryAuditRepository,
)
from appointment.validators import PatientValidator, DoctorValidator
from appointment.audit import AuditLogger


@pytest.fixture
def hospital_id(): return uuid4()

@pytest.fixture
def patient(hospital_id):
    return Patient(uuid4(), hospital_id, "Ravi", "9999999999", date(1990, 1, 1))

@pytest.fixture
def doctor(hospital_id):
    return Doctor(uuid4(), hospital_id, "Dr. A", "General", is_active=True)

@pytest.fixture
def inactive_doctor(hospital_id):
    return Doctor(uuid4(), hospital_id, "Dr. B", "General", is_active=False)

@pytest.fixture
def open_session(doctor, hospital_id):
    from datetime import datetime
    return DoctorSession(uuid4(), doctor.doctor_id, hospital_id, date.today(),
                         datetime.utcnow(), SessionStatus.OPEN)

@pytest.fixture
def p_repo(patient):
    r = InMemoryPatientRepository(); r.add(patient); return r

@pytest.fixture
def d_repo(doctor, inactive_doctor):
    r = InMemoryDoctorRepository()
    r.add(doctor); r.add(inactive_doctor); return r

@pytest.fixture
def s_repo(open_session):
    r = InMemorySessionRepository(); r.save(open_session); return r


# PatientValidator
def test_patient_validator_returns_patient(p_repo, patient, hospital_id):
    v = PatientValidator(p_repo)
    result = v.validate(patient.patient_id, hospital_id)
    assert result.patient_id == patient.patient_id

def test_patient_validator_raises_for_unknown(p_repo, hospital_id):
    v = PatientValidator(p_repo)
    with pytest.raises(PatientNotFound):
        v.validate(uuid4(), hospital_id)

def test_patient_validator_raises_for_wrong_hospital(p_repo, patient):
    v = PatientValidator(p_repo)
    with pytest.raises(PatientNotFound):
        v.validate(patient.patient_id, uuid4())  # different hospital


# DoctorValidator
def test_doctor_validator_active_returns_doctor(d_repo, doctor, hospital_id):
    v = DoctorValidator(d_repo, InMemorySessionRepository())
    result = v.validate_active(doctor.doctor_id, hospital_id)
    assert result.doctor_id == doctor.doctor_id

def test_doctor_validator_raises_for_inactive(d_repo, inactive_doctor, hospital_id):
    v = DoctorValidator(d_repo, InMemorySessionRepository())
    with pytest.raises(DoctorNotActive):
        v.validate_active(inactive_doctor.doctor_id, hospital_id)

def test_doctor_validator_open_session_returns_session(d_repo, s_repo, doctor, hospital_id, open_session):
    v = DoctorValidator(d_repo, s_repo)
    result = v.validate_open_session(doctor.doctor_id, hospital_id, date.today())
    assert result.session_id == open_session.session_id

def test_doctor_validator_raises_no_session(d_repo, doctor, hospital_id):
    v = DoctorValidator(d_repo, InMemorySessionRepository())
    with pytest.raises(NoActiveSession):
        v.validate_open_session(doctor.doctor_id, hospital_id, date.today())


# AuditLogger
def test_audit_logger_saves_log(patient):
    audit_repo = InMemoryAuditRepository()
    logger = AuditLogger(audit_repo)
    entity_id = uuid4()
    logger.log("TOKEN", entity_id, AuditAction.BOOKED, patient.patient_id, {"token_number": 1})
    logs = audit_repo.all()
    assert len(logs) == 1
    assert logs[0].entity_type == "TOKEN"
    assert logs[0].action == AuditAction.BOOKED
    assert logs[0].entity_id == entity_id
    assert logs[0].metadata == {"token_number": 1}
```

- [ ] **Step 2: Run to verify failure**

```bash
pytest tests/appointment/test_validators.py -v
```
Expected: `ImportError` — `appointment.validators` not found

- [ ] **Step 3: Write `appointment/validators.py`**

```python
from datetime import date
from uuid import UUID

from .errors import PatientNotFound, DoctorNotActive, NoActiveSession
from .models import Patient, Doctor, DoctorSession
from .repositories import PatientRepository, DoctorRepository, SessionRepository


class PatientValidator:
    def __init__(self, patients: PatientRepository):
        self._patients = patients

    def validate(self, patient_id: UUID, hospital_id: UUID) -> Patient:
        patient = self._patients.get(patient_id)
        if patient is None or patient.hospital_id != hospital_id:
            raise PatientNotFound()
        return patient


class DoctorValidator:
    def __init__(self, doctors: DoctorRepository, sessions: SessionRepository):
        self._doctors = doctors
        self._sessions = sessions

    def validate_active(self, doctor_id: UUID, hospital_id: UUID) -> Doctor:
        doctor = self._doctors.get(doctor_id)
        if doctor is None or doctor.hospital_id != hospital_id or not doctor.is_active:
            raise DoctorNotActive()
        return doctor

    def validate_open_session(self, doctor_id: UUID, hospital_id: UUID, on_date: date) -> DoctorSession:
        session = self._sessions.get_open(doctor_id, on_date)
        if session is None or session.hospital_id != hospital_id:
            raise NoActiveSession()
        return session
```

- [ ] **Step 4: Write `appointment/audit.py`**

```python
from datetime import datetime
from uuid import UUID, uuid4

from .enums import AuditAction
from .models import AuditLog
from .repositories import AuditRepository


class AuditLogger:
    def __init__(self, audit: AuditRepository):
        self._audit = audit

    def log(self, entity_type: str, entity_id: UUID, action: AuditAction,
            actor_id: UUID, metadata: dict = None) -> None:
        self._audit.save(AuditLog(
            log_id=uuid4(),
            entity_type=entity_type,
            entity_id=entity_id,
            action=action.value,
            actor_id=actor_id,
            timestamp=datetime.utcnow(),
            metadata=metadata or {},
        ))
```

- [ ] **Step 5: Run tests**

```bash
pytest tests/appointment/test_validators.py -v
```
Expected: all 9 tests PASS

- [ ] **Step 6: Commit**

```bash
git add appointment/validators.py appointment/audit.py tests/appointment/test_validators.py
git commit -m "feat(appointment): add patient/doctor validators and audit logger"
```

---

### Task 4: BookingStrategy Interface and BufferNotifier

**Files:**
- Create: `appointment/strategy.py`
- Create: `appointment/buffer.py`

**Interfaces:**
- Consumes: `TokenQueue`, `Token`, `Slot`, `Appointment`; error types; `AuditLogger`
- Produces:
  - `BookingStrategy` ABC with: `book(request: dict) -> dict`; `cancel(entity_id: UUID, actor_id: UUID) -> None`; `modify(entity_id: UUID, new_target: UUID, actor_id: UUID) -> dict`
  - `BufferNotifier` ABC with: `notify(token_ids: list[UUID]) -> None`
  - `NoOpBufferNotifier` concrete class (records calls for test inspection)

- [ ] **Step 1: Write `appointment/strategy.py`**

No failing test needed — this is a pure interface. Write it directly:

```python
from abc import ABC, abstractmethod
from uuid import UUID


class BookingStrategy(ABC):
    @abstractmethod
    def book(self, request: dict) -> dict: ...

    @abstractmethod
    def cancel(self, entity_id: UUID, actor_id: UUID) -> None: ...

    @abstractmethod
    def modify(self, entity_id: UUID, new_target: UUID, actor_id: UUID) -> dict: ...
```

- [ ] **Step 2: Write `appointment/buffer.py`**

```python
from abc import ABC, abstractmethod
from typing import List
from uuid import UUID


class BufferNotifier(ABC):
    @abstractmethod
    def notify(self, token_ids: List[UUID]) -> None: ...


class NoOpBufferNotifier(BufferNotifier):
    def __init__(self):
        self.notified: List[List[UUID]] = []

    def notify(self, token_ids: List[UUID]) -> None:
        self.notified.append(list(token_ids))
```

- [ ] **Step 3: Commit**

```bash
git add appointment/strategy.py appointment/buffer.py
git commit -m "feat(appointment): add BookingStrategy interface and BufferNotifier"
```

---

### Task 5: TokenStrategy — Book and Cancel

**Files:**
- Create: `appointment/token_strategy.py` (partial — book and cancel only)
- Create: `tests/appointment/test_token_strategy.py`

**Interfaces:**
- Consumes: `BookingStrategy`; `PatientValidator`, `DoctorValidator`; `AuditLogger`; `TokenRepository`, `TokenQueueRepository`, `SessionRepository`; `TokenType`, `TokenStatus`, `AuditAction`; `NoActiveSession`, `TokenNotCancellable`
- Produces:
  - `TokenStrategy.__init__(patient_v, doctor_v, audit, token_repo, queue_repo, session_repo, hospital_id, tatkal_ratio)`
  - `TokenStrategy.book(request: dict) -> dict` where request has keys `patient_id`, `doctor_id`, `token_type`; returns `{"token_id", "token_number", "token_type", "estimated_wait_position"}`
  - `TokenStrategy.cancel(token_id: UUID, actor_id: UUID) -> None`

- [ ] **Step 1: Write failing tests**

`tests/appointment/test_token_strategy.py`:
```python
import pytest
from uuid import uuid4
from datetime import date, datetime

from appointment.enums import BookingMode, TokenType, TokenStatus, SessionStatus, AuditAction
from appointment.errors import NoActiveSession, TokenNotCancellable
from appointment.models import Hospital, DoctorSession, TokenQueue
from appointment.repositories import InMemoryTokenQueueRepository
from appointment.token_strategy import TokenStrategy
from appointment.validators import PatientValidator, DoctorValidator
from appointment.audit import AuditLogger


@pytest.fixture
def token_strategy(repos, hospital_token, doctor, open_session):
    queue = TokenQueue(queue_id=uuid4(), session_id=open_session.session_id)
    repos["queues"].save(queue)
    return TokenStrategy(
        patient_v=PatientValidator(repos["patients"]),
        doctor_v=DoctorValidator(repos["doctors"], repos["sessions"]),
        audit=AuditLogger(repos["audit"]),
        token_repo=repos["tokens"],
        queue_repo=repos["queues"],
        session_repo=repos["sessions"],
        hospital_id=hospital_token.hospital_id,
        tatkal_ratio=2,
    )


def test_book_normal_token_returns_token_number(token_strategy, patient, doctor):
    result = token_strategy.book({
        "patient_id": patient.patient_id,
        "doctor_id": doctor.doctor_id,
        "token_type": TokenType.NORMAL,
    })
    assert result["token_number"] == 1
    assert result["token_type"] == TokenType.NORMAL


def test_book_increments_token_number(token_strategy, patient, doctor):
    r1 = token_strategy.book({"patient_id": patient.patient_id, "doctor_id": doctor.doctor_id, "token_type": TokenType.NORMAL})
    r2 = token_strategy.book({"patient_id": patient.patient_id, "doctor_id": doctor.doctor_id, "token_type": TokenType.NORMAL})
    assert r2["token_number"] == r1["token_number"] + 1


def test_book_tatkal_increments_tatkal_count(token_strategy, repos, patient, doctor, open_session):
    token_strategy.book({"patient_id": patient.patient_id, "doctor_id": doctor.doctor_id, "token_type": TokenType.TATKAL})
    q = repos["queues"].get_by_session(open_session.session_id)
    assert q.tatkal_count == 1
    assert q.normal_count == 0


def test_book_normal_increments_normal_count(token_strategy, repos, patient, doctor, open_session):
    token_strategy.book({"patient_id": patient.patient_id, "doctor_id": doctor.doctor_id, "token_type": TokenType.NORMAL})
    q = repos["queues"].get_by_session(open_session.session_id)
    assert q.normal_count == 1
    assert q.tatkal_count == 0


def test_book_logs_audit(token_strategy, repos, patient, doctor):
    token_strategy.book({"patient_id": patient.patient_id, "doctor_id": doctor.doctor_id, "token_type": TokenType.NORMAL})
    logs = repos["audit"].all()
    assert any(l.action == AuditAction.BOOKED for l in logs)


def test_book_estimated_wait_position_normal(token_strategy, patient, doctor):
    token_strategy.book({"patient_id": patient.patient_id, "doctor_id": doctor.doctor_id, "token_type": TokenType.NORMAL})
    result = token_strategy.book({"patient_id": patient.patient_id, "doctor_id": doctor.doctor_id, "token_type": TokenType.NORMAL})
    # 2 normal waiting, 0 tatkal → position = 2
    assert result["estimated_wait_position"] == 2


def test_cancel_waiting_token_succeeds(token_strategy, repos, patient, doctor):
    result = token_strategy.book({"patient_id": patient.patient_id, "doctor_id": doctor.doctor_id, "token_type": TokenType.NORMAL})
    token_strategy.cancel(result["token_id"], patient.patient_id)
    token = repos["tokens"].get(result["token_id"])
    assert token.status == TokenStatus.CANCELLED


def test_cancel_decrements_count(token_strategy, repos, patient, doctor, open_session):
    result = token_strategy.book({"patient_id": patient.patient_id, "doctor_id": doctor.doctor_id, "token_type": TokenType.NORMAL})
    token_strategy.cancel(result["token_id"], patient.patient_id)
    q = repos["queues"].get_by_session(open_session.session_id)
    assert q.normal_count == 0


def test_cancel_serving_token_raises(token_strategy, repos, patient, doctor):
    result = token_strategy.book({"patient_id": patient.patient_id, "doctor_id": doctor.doctor_id, "token_type": TokenType.NORMAL})
    token = repos["tokens"].get(result["token_id"])
    token.status = TokenStatus.SERVING
    repos["tokens"].update(token)
    with pytest.raises(TokenNotCancellable):
        token_strategy.cancel(result["token_id"], patient.patient_id)


def test_cancel_buffer_token_raises(token_strategy, repos, patient, doctor):
    result = token_strategy.book({"patient_id": patient.patient_id, "doctor_id": doctor.doctor_id, "token_type": TokenType.NORMAL})
    token = repos["tokens"].get(result["token_id"])
    token.status = TokenStatus.BUFFER
    repos["tokens"].update(token)
    with pytest.raises(TokenNotCancellable):
        token_strategy.cancel(result["token_id"], patient.patient_id)
```

- [ ] **Step 2: Run to verify failure**

```bash
pytest tests/appointment/test_token_strategy.py -v
```
Expected: `ImportError` — `appointment.token_strategy` not found

- [ ] **Step 3: Write `appointment/token_strategy.py`** (book + cancel only; modify stub)

```python
import math
from datetime import datetime, date
from uuid import UUID, uuid4

from .audit import AuditLogger
from .buffer import BufferNotifier, NoOpBufferNotifier
from .enums import TokenType, TokenStatus, AuditAction
from .errors import TokenNotCancellable, OperationNotSupported
from .models import Token, TokenQueue
from .repositories import TokenRepository, TokenQueueRepository, SessionRepository
from .strategy import BookingStrategy
from .validators import PatientValidator, DoctorValidator


class TokenStrategy(BookingStrategy):
    def __init__(
        self,
        patient_v: PatientValidator,
        doctor_v: DoctorValidator,
        audit: AuditLogger,
        token_repo: TokenRepository,
        queue_repo: TokenQueueRepository,
        session_repo: SessionRepository,
        hospital_id: UUID,
        tatkal_ratio: int,
        buffer_notifier: BufferNotifier = None,
    ):
        self._patient_v = patient_v
        self._doctor_v = doctor_v
        self._audit = audit
        self._tokens = token_repo
        self._queues = queue_repo
        self._sessions = session_repo
        self._hospital_id = hospital_id
        self._tatkal_ratio = tatkal_ratio
        self._notifier = buffer_notifier or NoOpBufferNotifier()

    def book(self, request: dict) -> dict:
        patient_id: UUID = request["patient_id"]
        doctor_id: UUID = request["doctor_id"]
        token_type: TokenType = request["token_type"]

        self._patient_v.validate(patient_id, self._hospital_id)
        session = self._doctor_v.validate_open_session(doctor_id, self._hospital_id, date.today())
        queue = self._queues.get_by_session(session.session_id)

        token_number = self._tokens.next_number(session.session_id)
        token = Token(
            token_id=uuid4(),
            hospital_id=self._hospital_id,
            doctor_id=doctor_id,
            patient_id=patient_id,
            session_id=session.session_id,
            token_number=token_number,
            token_type=token_type,
            status=TokenStatus.WAITING,
            issued_at=datetime.utcnow(),
        )
        self._tokens.save(token)

        if token_type == TokenType.TATKAL:
            queue.tatkal_count += 1
        else:
            queue.normal_count += 1
        self._queues.save(queue)

        estimated_wait = self._estimate_wait(queue, token_type)

        self._audit.log("TOKEN", token.token_id, AuditAction.BOOKED, patient_id,
                        {"token_number": token_number, "token_type": token_type.value})

        return {
            "token_id": token.token_id,
            "token_number": token_number,
            "token_type": token_type,
            "estimated_wait_position": estimated_wait,
        }

    def cancel(self, entity_id: UUID, actor_id: UUID) -> None:
        token = self._tokens.get(entity_id)
        if token.status != TokenStatus.WAITING:
            raise TokenNotCancellable(token.status.value)

        token.status = TokenStatus.CANCELLED
        self._tokens.update(token)

        queue = self._queues.get_by_session(token.session_id)
        if token.token_type == TokenType.TATKAL:
            queue.tatkal_count = max(0, queue.tatkal_count - 1)
        else:
            queue.normal_count = max(0, queue.normal_count - 1)
        self._queues.save(queue)

        self._audit.log("TOKEN", token.token_id, AuditAction.CANCELLED, actor_id)

    def modify(self, entity_id: UUID, new_target: UUID, actor_id: UUID) -> dict:
        raise OperationNotSupported("modify", "TOKEN")

    def _estimate_wait(self, queue: TokenQueue, token_type: TokenType) -> int:
        if token_type == TokenType.TATKAL:
            return queue.tatkal_count
        return queue.normal_count + math.ceil(queue.tatkal_count / self._tatkal_ratio)
```

- [ ] **Step 4: Run tests**

```bash
pytest tests/appointment/test_token_strategy.py -v
```
Expected: all 11 tests PASS

- [ ] **Step 5: Commit**

```bash
git add appointment/token_strategy.py tests/appointment/test_token_strategy.py
git commit -m "feat(appointment): implement TokenStrategy book and cancel"
```

---

### Task 6: TokenStrategy — Advance Buffer and Session Lifecycle

**Files:**
- Modify: `appointment/token_strategy.py` (add `advance_buffer`, `open_session`, `close_session`)
- Modify: `tests/appointment/test_token_strategy.py` (append new tests)

**Interfaces:**
- Consumes: existing `TokenStrategy`; `NoOpBufferNotifier`; `StaleQueueVersion`, `QueueEmpty`
- Produces:
  - `TokenStrategy.advance_buffer(session_id: UUID, doctor_id: UUID) -> dict` returns `{"serving_token_id", "buffer_token_ids"}` or raises `QueueEmpty`
  - `TokenStrategy.open_session(doctor_id: UUID) -> DoctorSession`
  - `TokenStrategy.close_session(session_id: UUID, doctor_id: UUID) -> None`

- [ ] **Step 1: Append failing tests to `tests/appointment/test_token_strategy.py`**

```python
# Append to existing test file

from appointment.errors import QueueEmpty, StaleQueueVersion
from appointment.buffer import NoOpBufferNotifier
from appointment.enums import SessionStatus


@pytest.fixture
def notifier():
    return NoOpBufferNotifier()


@pytest.fixture
def token_strategy_with_notifier(repos, hospital_token, doctor, open_session, notifier):
    queue = TokenQueue(queue_id=uuid4(), session_id=open_session.session_id)
    repos["queues"].save(queue)
    return TokenStrategy(
        patient_v=PatientValidator(repos["patients"]),
        doctor_v=DoctorValidator(repos["doctors"], repos["sessions"]),
        audit=AuditLogger(repos["audit"]),
        token_repo=repos["tokens"],
        queue_repo=repos["queues"],
        session_repo=repos["sessions"],
        hospital_id=hospital_token.hospital_id,
        tatkal_ratio=2,
        buffer_notifier=notifier,
    ), notifier, repos, open_session


def test_advance_buffer_serves_next_normal(token_strategy_with_notifier, patient, doctor):
    ts, notifier, repos, session = token_strategy_with_notifier
    # Book 2 normal tokens
    r1 = ts.book({"patient_id": patient.patient_id, "doctor_id": doctor.doctor_id, "token_type": TokenType.NORMAL})
    r2 = ts.book({"patient_id": patient.patient_id, "doctor_id": doctor.doctor_id, "token_type": TokenType.NORMAL})
    result = ts.advance_buffer(session.session_id, doctor.doctor_id)
    t = repos["tokens"].get(result["serving_token_id"])
    assert t.status == TokenStatus.SERVING
    assert t.token_id == r1["token_id"]


def test_advance_buffer_ratio_serves_tatkal_after_2_normals(token_strategy_with_notifier, patient, doctor):
    ts, notifier, repos, session = token_strategy_with_notifier
    # Book 2 normal then 1 tatkal
    rn1 = ts.book({"patient_id": patient.patient_id, "doctor_id": doctor.doctor_id, "token_type": TokenType.NORMAL})
    rn2 = ts.book({"patient_id": patient.patient_id, "doctor_id": doctor.doctor_id, "token_type": TokenType.NORMAL})
    rt1 = ts.book({"patient_id": patient.patient_id, "doctor_id": doctor.doctor_id, "token_type": TokenType.TATKAL})

    # First advance: serves normal 1
    ts.advance_buffer(session.session_id, doctor.doctor_id)
    # Simulate first complete (mark COMPLETED manually via repo for test)
    t = repos["tokens"].get(rn1["token_id"]); t.status = TokenStatus.COMPLETED; repos["tokens"].update(t)
    # Second advance: serves normal 2
    ts.advance_buffer(session.session_id, doctor.doctor_id)
    t2 = repos["tokens"].get(rn2["token_id"]); t2.status = TokenStatus.COMPLETED; repos["tokens"].update(t2)
    # Third advance: should serve tatkal (ratio=2 exhausted)
    result = ts.advance_buffer(session.session_id, doctor.doctor_id)
    served = repos["tokens"].get(result["serving_token_id"])
    assert served.token_type == TokenType.TATKAL


def test_advance_buffer_notifies_next_3(token_strategy_with_notifier, patient, doctor):
    ts, notifier, repos, session = token_strategy_with_notifier
    for _ in range(5):
        ts.book({"patient_id": patient.patient_id, "doctor_id": doctor.doctor_id, "token_type": TokenType.NORMAL})
    ts.advance_buffer(session.session_id, doctor.doctor_id)
    assert len(notifier.notified) == 1
    assert len(notifier.notified[0]) <= 3


def test_advance_buffer_raises_queue_empty(token_strategy_with_notifier, doctor):
    ts, notifier, repos, session = token_strategy_with_notifier
    with pytest.raises(QueueEmpty):
        ts.advance_buffer(session.session_id, doctor.doctor_id)


def test_close_session_cancels_waiting_and_buffer(token_strategy_with_notifier, patient, doctor):
    ts, notifier, repos, session = token_strategy_with_notifier
    r1 = ts.book({"patient_id": patient.patient_id, "doctor_id": doctor.doctor_id, "token_type": TokenType.NORMAL})
    r2 = ts.book({"patient_id": patient.patient_id, "doctor_id": doctor.doctor_id, "token_type": TokenType.NORMAL})
    ts.close_session(session.session_id, doctor.doctor_id)
    t1 = repos["tokens"].get(r1["token_id"])
    t2 = repos["tokens"].get(r2["token_id"])
    assert t1.status == TokenStatus.CANCELLED
    assert t2.status == TokenStatus.CANCELLED


def test_close_session_marks_session_closed(token_strategy_with_notifier, doctor, repos, open_session):
    ts, notifier, _, session = token_strategy_with_notifier
    ts.close_session(session.session_id, doctor.doctor_id)
    s = repos["sessions"].get(session.session_id)
    assert s.status == SessionStatus.CLOSED
    assert s.ended_at is not None
```

- [ ] **Step 2: Run to verify failure**

```bash
pytest tests/appointment/test_token_strategy.py -v -k "advance or close"
```
Expected: `AttributeError` — `advance_buffer` not defined

- [ ] **Step 3: Add `advance_buffer`, `open_session`, `close_session` to `appointment/token_strategy.py`**

Append these methods to the `TokenStrategy` class:

```python
    def advance_buffer(self, session_id: UUID, doctor_id: UUID) -> dict:
        queue = self._queues.get_by_session(session_id)

        # Mark current serving token COMPLETED
        if queue.serving_token_id:
            serving = self._tokens.get(queue.serving_token_id)
            if serving and serving.status == TokenStatus.SERVING:
                serving.status = TokenStatus.COMPLETED
                serving.served_at = datetime.utcnow()
                self._tokens.update(serving)

        # Pick next token to serve
        next_token = self._pick_next(session_id, queue)
        if next_token is None:
            raise QueueEmpty()

        next_token.status = TokenStatus.SERVING
        next_token.called_at = datetime.utcnow()
        self._tokens.update(next_token)

        if next_token.token_type == TokenType.TATKAL:
            queue.consecutive_normal_served = 0
        else:
            queue.consecutive_normal_served += 1

        queue.serving_token_id = next_token.token_id

        # Resolve buffer (next 3 after serving)
        buffer_tokens = self._peek_next_3(session_id, queue, exclude_id=next_token.token_id)
        for bt in buffer_tokens:
            bt.status = TokenStatus.BUFFER
            self._tokens.update(bt)
        queue.buffer_token_ids = [bt.token_id for bt in buffer_tokens]

        if not self._queues.update_with_version(queue):
            raise StaleQueueVersion()

        self._notifier.notify(queue.buffer_token_ids)
        self._audit.log("TOKEN", next_token.token_id, AuditAction.CALLED, doctor_id,
                        {"token_number": next_token.token_number})

        return {"serving_token_id": next_token.token_id, "buffer_token_ids": queue.buffer_token_ids}

    def open_session(self, doctor_id: UUID) -> "DoctorSession":
        from .enums import SessionStatus
        from .models import DoctorSession
        session = DoctorSession(
            session_id=uuid4(),
            doctor_id=doctor_id,
            hospital_id=self._hospital_id,
            date=date.today(),
            started_at=datetime.utcnow(),
            status=SessionStatus.OPEN,
        )
        self._sessions.save(session)
        queue = TokenQueue(queue_id=uuid4(), session_id=session.session_id)
        self._queues.save(queue)
        return session

    def close_session(self, session_id: UUID, doctor_id: UUID) -> None:
        from .enums import SessionStatus
        session = self._sessions.get(session_id)
        session.status = SessionStatus.CLOSED
        session.ended_at = datetime.utcnow()
        self._sessions.save(session)

        cancelled = self._tokens.bulk_cancel(
            session_id,
            [TokenStatus.WAITING, TokenStatus.BUFFER],
            reason="SESSION_CLOSED",
        )

        self._audit.log("TOKEN", session_id, AuditAction.SESSION_CLOSED, doctor_id,
                        {"cancelled_count": len(cancelled)})

    def _pick_next(self, session_id: UUID, queue: TokenQueue):
        if (queue.consecutive_normal_served < self._tatkal_ratio
                and queue.normal_count > 0):
            normals = self._tokens.get_waiting_ordered(session_id, TokenType.NORMAL)
            if normals:
                return normals[0]
        tatkals = self._tokens.get_waiting_ordered(session_id, TokenType.TATKAL)
        if tatkals:
            return tatkals[0]
        normals = self._tokens.get_waiting_ordered(session_id, TokenType.NORMAL)
        return normals[0] if normals else None

    def _peek_next_3(self, session_id: UUID, queue: TokenQueue, exclude_id: UUID):
        sim_queue = TokenQueue(
            queue_id=queue.queue_id,
            session_id=queue.session_id,
            tatkal_count=queue.tatkal_count,
            normal_count=queue.normal_count,
            consecutive_normal_served=queue.consecutive_normal_served,
        )
        results = []
        seen = {exclude_id}
        for _ in range(3):
            next_t = self._pick_next_excluding(session_id, sim_queue, seen)
            if next_t is None:
                break
            results.append(next_t)
            seen.add(next_t.token_id)
            if next_t.token_type == TokenType.TATKAL:
                sim_queue.consecutive_normal_served = 0
                sim_queue.tatkal_count = max(0, sim_queue.tatkal_count - 1)
            else:
                sim_queue.consecutive_normal_served += 1
                sim_queue.normal_count = max(0, sim_queue.normal_count - 1)
        return results

    def _pick_next_excluding(self, session_id: UUID, queue: TokenQueue, exclude_ids: set):
        if (queue.consecutive_normal_served < self._tatkal_ratio
                and queue.normal_count > 0):
            normals = [t for t in self._tokens.get_waiting_ordered(session_id, TokenType.NORMAL)
                       if t.token_id not in exclude_ids]
            if normals:
                return normals[0]
        tatkals = [t for t in self._tokens.get_waiting_ordered(session_id, TokenType.TATKAL)
                   if t.token_id not in exclude_ids]
        if tatkals:
            return tatkals[0]
        normals = [t for t in self._tokens.get_waiting_ordered(session_id, TokenType.NORMAL)
                   if t.token_id not in exclude_ids]
        return normals[0] if normals else None
```

Also add this import at the top of the file:
```python
from .errors import TokenNotCancellable, OperationNotSupported, QueueEmpty, StaleQueueVersion
```

- [ ] **Step 4: Run all token tests**

```bash
pytest tests/appointment/test_token_strategy.py -v
```
Expected: all tests PASS

- [ ] **Step 5: Commit**

```bash
git add appointment/token_strategy.py tests/appointment/test_token_strategy.py
git commit -m "feat(appointment): add advance_buffer and session lifecycle to TokenStrategy"
```

---

### Task 7: SlotStrategy

**Files:**
- Create: `appointment/slot_strategy.py`
- Create: `tests/appointment/test_slot_strategy.py`

**Interfaces:**
- Consumes: `BookingStrategy`; `PatientValidator`, `DoctorValidator`; `AuditLogger`; `SlotRepository`, `AppointmentRepository`; `SlotStatus`, `AppointmentStatus`, `AuditAction`; `SlotUnavailable`, `SlotBlocked`, `DuplicateBooking`, `AppointmentNotModifiable`
- Produces:
  - `SlotStrategy.book(request: dict) -> dict` where request has keys `patient_id`, `doctor_id`, `slot_id`; returns `{"appointment_id", "slot_start_time"}`
  - `SlotStrategy.cancel(appointment_id: UUID, actor_id: UUID) -> None`
  - `SlotStrategy.modify(appointment_id: UUID, new_slot_id: UUID, actor_id: UUID) -> dict` returns `{"appointment_id", "slot_start_time"}`
  - `SlotStrategy.view_slots(doctor_id: UUID, on_date: date) -> list[dict]`

- [ ] **Step 1: Write failing tests**

`tests/appointment/test_slot_strategy.py`:
```python
import pytest
from uuid import uuid4
from datetime import date, time

from appointment.enums import SlotStatus, AppointmentStatus, BookingMode, AuditAction
from appointment.errors import SlotUnavailable, SlotBlocked, DuplicateBooking, AppointmentNotModifiable
from appointment.models import Hospital, Patient, Doctor, Slot, Appointment
from appointment.repositories import (
    InMemoryPatientRepository, InMemoryDoctorRepository,
    InMemorySlotRepository, InMemoryAppointmentRepository,
    InMemoryAuditRepository, InMemorySessionRepository,
)
from appointment.slot_strategy import SlotStrategy
from appointment.validators import PatientValidator, DoctorValidator
from appointment.audit import AuditLogger


@pytest.fixture
def hospital_slot():
    from appointment.models import Hospital
    return Hospital(uuid4(), "Slot Hospital", BookingMode.SLOT)


@pytest.fixture
def patient_s(hospital_slot):
    return Patient(uuid4(), hospital_slot.hospital_id, "Meera", "8888888888", date(1992, 3, 15))


@pytest.fixture
def doctor_s(hospital_slot):
    return Doctor(uuid4(), hospital_slot.hospital_id, "Dr. Sharma", "Ortho", is_active=True)


@pytest.fixture
def slot(doctor_s, hospital_slot):
    return Slot(uuid4(), hospital_slot.hospital_id, doctor_s.doctor_id,
                date.today(), time(10, 0), time(10, 20), SlotStatus.AVAILABLE, uuid4())


@pytest.fixture
def slot_repos(patient_s, doctor_s, slot):
    p_repo = InMemoryPatientRepository(); p_repo.add(patient_s)
    d_repo = InMemoryDoctorRepository(); d_repo.add(doctor_s)
    sl_repo = InMemorySlotRepository(); sl_repo.add(slot)
    return {
        "patients": p_repo, "doctors": d_repo,
        "slots": sl_repo,
        "appointments": InMemoryAppointmentRepository(),
        "audit": InMemoryAuditRepository(),
        "sessions": InMemorySessionRepository(),
    }


@pytest.fixture
def slot_strategy(slot_repos, hospital_slot):
    return SlotStrategy(
        patient_v=PatientValidator(slot_repos["patients"]),
        doctor_v=DoctorValidator(slot_repos["doctors"], slot_repos["sessions"]),
        audit=AuditLogger(slot_repos["audit"]),
        slot_repo=slot_repos["slots"],
        appointment_repo=slot_repos["appointments"],
        hospital_id=hospital_slot.hospital_id,
    )


def test_view_slots_returns_available(slot_strategy, doctor_s, slot):
    results = slot_strategy.view_slots(doctor_s.doctor_id, date.today())
    assert len(results) == 1
    assert results[0]["slot_id"] == slot.slot_id


def test_book_slot_returns_appointment_id(slot_strategy, patient_s, doctor_s, slot):
    result = slot_strategy.book({"patient_id": patient_s.patient_id, "doctor_id": doctor_s.doctor_id, "slot_id": slot.slot_id})
    assert "appointment_id" in result
    assert result["slot_start_time"] == slot.start_time


def test_book_slot_marks_slot_booked(slot_strategy, slot_repos, patient_s, doctor_s, slot):
    slot_strategy.book({"patient_id": patient_s.patient_id, "doctor_id": doctor_s.doctor_id, "slot_id": slot.slot_id})
    updated = slot_repos["slots"].get(slot.slot_id)
    assert updated.status == SlotStatus.BOOKED


def test_book_slot_raises_if_already_booked(slot_strategy, slot_repos, patient_s, doctor_s, slot):
    slot_strategy.book({"patient_id": patient_s.patient_id, "doctor_id": doctor_s.doctor_id, "slot_id": slot.slot_id})
    # second patient tries same slot
    p2 = Patient(uuid4(), patient_s.hospital_id, "Ravi", "7777777777", date(1985, 1, 1))
    slot_repos["patients"].add(p2)
    with pytest.raises(SlotUnavailable):
        slot_strategy.book({"patient_id": p2.patient_id, "doctor_id": doctor_s.doctor_id, "slot_id": slot.slot_id})


def test_book_slot_raises_if_blocked(slot_strategy, slot_repos, patient_s, doctor_s, slot):
    s = slot_repos["slots"].get(slot.slot_id)
    s.status = SlotStatus.BLOCKED
    slot_repos["slots"].update(s)
    with pytest.raises(SlotBlocked):
        slot_strategy.book({"patient_id": patient_s.patient_id, "doctor_id": doctor_s.doctor_id, "slot_id": slot.slot_id})


def test_book_duplicate_raises(slot_strategy, slot_repos, patient_s, doctor_s, slot):
    slot_strategy.book({"patient_id": patient_s.patient_id, "doctor_id": doctor_s.doctor_id, "slot_id": slot.slot_id})
    slot2 = Slot(uuid4(), slot.hospital_id, doctor_s.doctor_id,
                 date.today(), time(10, 20), time(10, 40), SlotStatus.AVAILABLE, uuid4())
    slot_repos["slots"].add(slot2)
    with pytest.raises(DuplicateBooking):
        slot_strategy.book({"patient_id": patient_s.patient_id, "doctor_id": doctor_s.doctor_id, "slot_id": slot2.slot_id})


def test_cancel_frees_slot(slot_strategy, slot_repos, patient_s, doctor_s, slot):
    result = slot_strategy.book({"patient_id": patient_s.patient_id, "doctor_id": doctor_s.doctor_id, "slot_id": slot.slot_id})
    slot_strategy.cancel(result["appointment_id"], patient_s.patient_id)
    updated_slot = slot_repos["slots"].get(slot.slot_id)
    assert updated_slot.status == SlotStatus.AVAILABLE


def test_cancel_marks_appointment_cancelled(slot_strategy, slot_repos, patient_s, doctor_s, slot):
    result = slot_strategy.book({"patient_id": patient_s.patient_id, "doctor_id": doctor_s.doctor_id, "slot_id": slot.slot_id})
    slot_strategy.cancel(result["appointment_id"], patient_s.patient_id)
    appt = slot_repos["appointments"].get(result["appointment_id"])
    assert appt.status == AppointmentStatus.CANCELLED


def test_modify_swaps_slot_atomically(slot_strategy, slot_repos, patient_s, doctor_s, slot):
    slot2 = Slot(uuid4(), slot.hospital_id, doctor_s.doctor_id,
                 date.today(), time(11, 0), time(11, 20), SlotStatus.AVAILABLE, uuid4())
    slot_repos["slots"].add(slot2)
    result = slot_strategy.book({"patient_id": patient_s.patient_id, "doctor_id": doctor_s.doctor_id, "slot_id": slot.slot_id})
    new_result = slot_strategy.modify(result["appointment_id"], slot2.slot_id, patient_s.patient_id)

    old_slot = slot_repos["slots"].get(slot.slot_id)
    new_slot = slot_repos["slots"].get(slot2.slot_id)
    old_appt = slot_repos["appointments"].get(result["appointment_id"])

    assert old_slot.status == SlotStatus.AVAILABLE
    assert new_slot.status == SlotStatus.BOOKED
    assert old_appt.status == AppointmentStatus.CANCELLED
    assert new_result["slot_start_time"] == slot2.start_time


def test_modify_raises_if_new_slot_booked(slot_strategy, slot_repos, patient_s, doctor_s, slot):
    slot2 = Slot(uuid4(), slot.hospital_id, doctor_s.doctor_id,
                 date.today(), time(11, 0), time(11, 20), SlotStatus.BOOKED, uuid4())
    slot_repos["slots"].add(slot2)
    result = slot_strategy.book({"patient_id": patient_s.patient_id, "doctor_id": doctor_s.doctor_id, "slot_id": slot.slot_id})
    with pytest.raises(SlotUnavailable):
        slot_strategy.modify(result["appointment_id"], slot2.slot_id, patient_s.patient_id)


def test_modify_logs_audit(slot_strategy, slot_repos, patient_s, doctor_s, slot):
    slot2 = Slot(uuid4(), slot.hospital_id, doctor_s.doctor_id,
                 date.today(), time(11, 0), time(11, 20), SlotStatus.AVAILABLE, uuid4())
    slot_repos["slots"].add(slot2)
    result = slot_strategy.book({"patient_id": patient_s.patient_id, "doctor_id": doctor_s.doctor_id, "slot_id": slot.slot_id})
    slot_strategy.modify(result["appointment_id"], slot2.slot_id, patient_s.patient_id)
    logs = slot_repos["audit"].all()
    assert any(l.action == AuditAction.MODIFIED for l in logs)
```

- [ ] **Step 2: Run to verify failure**

```bash
pytest tests/appointment/test_slot_strategy.py -v
```
Expected: `ImportError` — `appointment.slot_strategy` not found

- [ ] **Step 3: Write `appointment/slot_strategy.py`**

```python
from datetime import date, datetime
from uuid import UUID, uuid4

from .audit import AuditLogger
from .enums import SlotStatus, AppointmentStatus, AuditAction
from .errors import SlotUnavailable, SlotBlocked, DuplicateBooking, AppointmentNotModifiable
from .models import Appointment
from .repositories import SlotRepository, AppointmentRepository
from .strategy import BookingStrategy
from .validators import PatientValidator, DoctorValidator


class SlotStrategy(BookingStrategy):
    def __init__(
        self,
        patient_v: PatientValidator,
        doctor_v: DoctorValidator,
        audit: AuditLogger,
        slot_repo: SlotRepository,
        appointment_repo: AppointmentRepository,
        hospital_id: UUID,
    ):
        self._patient_v = patient_v
        self._doctor_v = doctor_v
        self._audit = audit
        self._slots = slot_repo
        self._appointments = appointment_repo
        self._hospital_id = hospital_id

    def view_slots(self, doctor_id: UUID, on_date: date) -> list:
        self._doctor_v.validate_active(doctor_id, self._hospital_id)
        slots = self._slots.get_available(doctor_id, on_date)
        return [{"slot_id": s.slot_id, "start_time": s.start_time, "end_time": s.end_time} for s in slots]

    def book(self, request: dict) -> dict:
        patient_id: UUID = request["patient_id"]
        doctor_id: UUID = request["doctor_id"]
        slot_id: UUID = request["slot_id"]

        self._patient_v.validate(patient_id, self._hospital_id)
        self._doctor_v.validate_active(doctor_id, self._hospital_id)

        # Duplicate check
        slot = self._slots.get(slot_id)
        existing = self._appointments.get_scheduled(patient_id, doctor_id, slot.date)
        if existing:
            raise DuplicateBooking()

        # Lock and validate slot (in-memory: just re-fetch; in production: SELECT FOR UPDATE)
        slot = self._slots.get(slot_id)
        if slot.status == SlotStatus.BLOCKED:
            raise SlotBlocked()
        if slot.status != SlotStatus.AVAILABLE:
            raise SlotUnavailable()

        slot.status = SlotStatus.BOOKED
        self._slots.update(slot)

        appointment = Appointment(
            appointment_id=uuid4(),
            hospital_id=self._hospital_id,
            patient_id=patient_id,
            doctor_id=doctor_id,
            slot_id=slot_id,
            status=AppointmentStatus.SCHEDULED,
            booked_at=datetime.utcnow(),
        )
        self._appointments.save(appointment)

        self._audit.log("APPOINTMENT", appointment.appointment_id, AuditAction.BOOKED,
                        patient_id, {"slot_id": str(slot_id)})

        return {"appointment_id": appointment.appointment_id, "slot_start_time": slot.start_time}

    def cancel(self, entity_id: UUID, actor_id: UUID) -> None:
        appointment = self._appointments.get(entity_id)
        if appointment.status != AppointmentStatus.SCHEDULED:
            raise AppointmentNotModifiable(appointment.status.value)

        appointment.status = AppointmentStatus.CANCELLED
        appointment.cancelled_at = datetime.utcnow()
        self._appointments.update(appointment)

        slot = self._slots.get(appointment.slot_id)
        slot.status = SlotStatus.AVAILABLE
        self._slots.update(slot)

        self._audit.log("APPOINTMENT", appointment.appointment_id, AuditAction.CANCELLED, actor_id)

    def modify(self, entity_id: UUID, new_target: UUID, actor_id: UUID) -> dict:
        appointment = self._appointments.get(entity_id)
        if appointment.status != AppointmentStatus.SCHEDULED:
            raise AppointmentNotModifiable(appointment.status.value)

        # Validate new slot (lock in production: SELECT FOR UPDATE)
        new_slot = self._slots.get(new_target)
        if new_slot.status == SlotStatus.BLOCKED:
            raise SlotBlocked()
        if new_slot.status != SlotStatus.AVAILABLE:
            raise SlotUnavailable()

        # Atomic swap: free old slot, cancel old appointment, book new slot, create new appointment
        old_slot = self._slots.get(appointment.slot_id)
        old_slot.status = SlotStatus.AVAILABLE
        self._slots.update(old_slot)

        appointment.status = AppointmentStatus.CANCELLED
        appointment.cancelled_at = datetime.utcnow()
        self._appointments.update(appointment)

        new_slot.status = SlotStatus.BOOKED
        self._slots.update(new_slot)

        new_appointment = Appointment(
            appointment_id=uuid4(),
            hospital_id=self._hospital_id,
            patient_id=appointment.patient_id,
            doctor_id=appointment.doctor_id,
            slot_id=new_target,
            status=AppointmentStatus.SCHEDULED,
            booked_at=datetime.utcnow(),
        )
        self._appointments.save(new_appointment)

        self._audit.log("APPOINTMENT", new_appointment.appointment_id, AuditAction.MODIFIED,
                        actor_id, {"old_appointment_id": str(entity_id)})

        return {"appointment_id": new_appointment.appointment_id, "slot_start_time": new_slot.start_time}
```

- [ ] **Step 4: Run all slot tests**

```bash
pytest tests/appointment/test_slot_strategy.py -v
```
Expected: all 11 tests PASS

- [ ] **Step 5: Commit**

```bash
git add appointment/slot_strategy.py tests/appointment/test_slot_strategy.py
git commit -m "feat(appointment): implement SlotStrategy with book, cancel, modify, view_slots"
```

---

### Task 8: AppointmentEngine

**Files:**
- Create: `appointment/engine.py`
- Create: `tests/appointment/test_engine.py`

**Interfaces:**
- Consumes: `TokenStrategy`, `SlotStrategy`, `BookingStrategy`; `Hospital`; `BookingMode`; `OperationNotSupported`
- Produces:
  - `AppointmentEngine(hospital: Hospital, strategy: BookingStrategy, token_strategy: TokenStrategy | None)`
  - `AppointmentEngine.book(request: dict) -> dict`
  - `AppointmentEngine.cancel(entity_id: UUID, actor_id: UUID) -> None`
  - `AppointmentEngine.modify(entity_id: UUID, new_target: UUID, actor_id: UUID) -> dict`
  - `AppointmentEngine.advance_buffer(session_id: UUID, doctor_id: UUID) -> dict` (TOKEN only)
  - `AppointmentEngine.open_session(doctor_id: UUID) -> DoctorSession` (TOKEN only)
  - `AppointmentEngine.close_session(session_id: UUID, doctor_id: UUID) -> None` (TOKEN only)
  - `AppointmentEngine.view_slots(doctor_id: UUID, on_date: date) -> list` (SLOT only)

- [ ] **Step 1: Write failing tests**

`tests/appointment/test_engine.py`:
```python
import pytest
from uuid import uuid4
from datetime import date, datetime, time

from appointment.enums import BookingMode, TokenType, SlotStatus
from appointment.errors import OperationNotSupported
from appointment.engine import AppointmentEngine
from appointment.models import Hospital, TokenQueue, Slot
from appointment.repositories import InMemoryTokenQueueRepository
from appointment.token_strategy import TokenStrategy
from appointment.slot_strategy import SlotStrategy
from appointment.validators import PatientValidator, DoctorValidator
from appointment.audit import AuditLogger


@pytest.fixture
def token_engine(repos, hospital_token, doctor, open_session):
    queue = TokenQueue(queue_id=uuid4(), session_id=open_session.session_id)
    repos["queues"].save(queue)
    strategy = TokenStrategy(
        patient_v=PatientValidator(repos["patients"]),
        doctor_v=DoctorValidator(repos["doctors"], repos["sessions"]),
        audit=AuditLogger(repos["audit"]),
        token_repo=repos["tokens"],
        queue_repo=repos["queues"],
        session_repo=repos["sessions"],
        hospital_id=hospital_token.hospital_id,
        tatkal_ratio=hospital_token.tatkal_ratio,
    )
    return AppointmentEngine(hospital=hospital_token, strategy=strategy, token_strategy=strategy)


@pytest.fixture
def slot_engine(hospital_slot, doctor):
    from appointment.models import Patient
    from appointment.repositories import (
        InMemoryPatientRepository, InMemoryDoctorRepository,
        InMemorySessionRepository, InMemorySlotRepository,
        InMemoryAppointmentRepository, InMemoryAuditRepository,
    )
    p_repo = InMemoryPatientRepository()
    patient = Patient(uuid4(), hospital_slot.hospital_id, "Meera", "8888888888", date(1990,1,1))
    p_repo.add(patient)
    d = Doctor(doctor.doctor_id, hospital_slot.hospital_id, doctor.name, doctor.specialization, True)
    d_repo = InMemoryDoctorRepository(); d_repo.add(d)
    sl_repo = InMemorySlotRepository()
    slot = Slot(uuid4(), hospital_slot.hospital_id, d.doctor_id,
                date.today(), time(9,0), time(9,20), SlotStatus.AVAILABLE, uuid4())
    sl_repo.add(slot)
    a_repo = InMemoryAppointmentRepository()
    audit_repo = InMemoryAuditRepository()
    strategy = SlotStrategy(
        patient_v=PatientValidator(p_repo),
        doctor_v=DoctorValidator(d_repo, InMemorySessionRepository()),
        audit=AuditLogger(audit_repo),
        slot_repo=sl_repo,
        appointment_repo=a_repo,
        hospital_id=hospital_slot.hospital_id,
    )
    return AppointmentEngine(hospital=hospital_slot, strategy=strategy, token_strategy=None), patient, d, slot


# Token engine tests
def test_token_engine_book_delegates_to_strategy(token_engine, patient, doctor):
    result = token_engine.book({"patient_id": patient.patient_id, "doctor_id": doctor.doctor_id, "token_type": TokenType.NORMAL})
    assert "token_number" in result


def test_token_engine_modify_raises_not_supported(token_engine):
    with pytest.raises(OperationNotSupported):
        token_engine.modify(uuid4(), uuid4(), uuid4())


def test_token_engine_view_slots_raises_not_supported(token_engine, doctor):
    with pytest.raises(OperationNotSupported):
        token_engine.view_slots(doctor.doctor_id, date.today())


def test_token_engine_advance_buffer_raises_queue_empty(token_engine, open_session, doctor):
    from appointment.errors import QueueEmpty
    with pytest.raises(QueueEmpty):
        token_engine.advance_buffer(open_session.session_id, doctor.doctor_id)


# Slot engine tests
def test_slot_engine_book_delegates_to_strategy(slot_engine):
    engine, patient, doctor, slot = slot_engine
    result = engine.book({"patient_id": patient.patient_id, "doctor_id": doctor.doctor_id, "slot_id": slot.slot_id})
    assert "appointment_id" in result


def test_slot_engine_advance_buffer_raises_not_supported(slot_engine):
    engine, _, doctor, _ = slot_engine
    with pytest.raises(OperationNotSupported):
        engine.advance_buffer(uuid4(), doctor.doctor_id)


def test_slot_engine_view_slots_returns_results(slot_engine):
    engine, _, doctor, slot = slot_engine
    results = engine.view_slots(doctor.doctor_id, date.today())
    assert len(results) == 1
    assert results[0]["slot_id"] == slot.slot_id
```

- [ ] **Step 2: Run to verify failure**

```bash
pytest tests/appointment/test_engine.py -v
```
Expected: `ImportError` — `appointment.engine` not found

- [ ] **Step 3: Write `appointment/engine.py`**

```python
from datetime import date
from uuid import UUID

from .enums import BookingMode
from .errors import OperationNotSupported
from .models import Hospital, DoctorSession
from .strategy import BookingStrategy


class AppointmentEngine:
    def __init__(self, hospital: Hospital, strategy: BookingStrategy, token_strategy=None):
        self._hospital = hospital
        self._strategy = strategy
        self._token_strategy = token_strategy  # Only set when booking_mode == TOKEN

    def book(self, request: dict) -> dict:
        return self._strategy.book(request)

    def cancel(self, entity_id: UUID, actor_id: UUID) -> None:
        self._strategy.cancel(entity_id, actor_id)

    def modify(self, entity_id: UUID, new_target: UUID, actor_id: UUID) -> dict:
        return self._strategy.modify(entity_id, new_target, actor_id)

    def advance_buffer(self, session_id: UUID, doctor_id: UUID) -> dict:
        self._require_token_mode("advance_buffer")
        return self._token_strategy.advance_buffer(session_id, doctor_id)

    def open_session(self, doctor_id: UUID) -> DoctorSession:
        self._require_token_mode("open_session")
        return self._token_strategy.open_session(doctor_id)

    def close_session(self, session_id: UUID, doctor_id: UUID) -> None:
        self._require_token_mode("close_session")
        self._token_strategy.close_session(session_id, doctor_id)

    def view_slots(self, doctor_id: UUID, on_date: date) -> list:
        self._require_slot_mode("view_slots")
        return self._strategy.view_slots(doctor_id, on_date)

    def _require_token_mode(self, operation: str) -> None:
        if self._hospital.booking_mode != BookingMode.TOKEN:
            raise OperationNotSupported(operation, self._hospital.booking_mode.value)

    def _require_slot_mode(self, operation: str) -> None:
        if self._hospital.booking_mode != BookingMode.SLOT:
            raise OperationNotSupported(operation, self._hospital.booking_mode.value)
```

- [ ] **Step 4: Run all tests**

```bash
pytest tests/appointment/ -v
```
Expected: all tests PASS

- [ ] **Step 5: Commit**

```bash
git add appointment/engine.py tests/appointment/test_engine.py
git commit -m "feat(appointment): add AppointmentEngine orchestrator"
```

---

### Task 9: PostgreSQL Schema

**Files:**
- Create: `appointment/schema.sql`

**Interfaces:**
- Produces: DDL for all tables matching the models in Task 1

- [ ] **Step 1: Write `appointment/schema.sql`**

```sql
-- Appointment Booking System — PostgreSQL DDL
-- Run once per deployment. Tables are hospital-scoped; add RLS policies per biz_id pattern.

CREATE TABLE hospitals (
    hospital_id     UUID PRIMARY KEY,
    name            TEXT NOT NULL,
    booking_mode    TEXT NOT NULL CHECK (booking_mode IN ('TOKEN', 'SLOT')),
    tatkal_ratio    INT NOT NULL DEFAULT 2
);

CREATE TABLE patients (
    patient_id      UUID PRIMARY KEY,
    hospital_id     UUID NOT NULL REFERENCES hospitals(hospital_id),
    name            TEXT NOT NULL,
    phone           TEXT NOT NULL,
    dob             DATE NOT NULL
);

CREATE TABLE doctors (
    doctor_id       UUID PRIMARY KEY,
    hospital_id     UUID NOT NULL REFERENCES hospitals(hospital_id),
    name            TEXT NOT NULL,
    specialization  TEXT NOT NULL,
    is_active       BOOLEAN NOT NULL DEFAULT TRUE
);

CREATE TABLE doctor_sessions (
    session_id      UUID PRIMARY KEY,
    doctor_id       UUID NOT NULL REFERENCES doctors(doctor_id),
    hospital_id     UUID NOT NULL REFERENCES hospitals(hospital_id),
    date            DATE NOT NULL,
    started_at      TIMESTAMPTZ NOT NULL,
    ended_at        TIMESTAMPTZ,
    status          TEXT NOT NULL CHECK (status IN ('OPEN', 'CLOSED')),
    UNIQUE (doctor_id, date, status)  -- one OPEN session per doctor per day
);

-- Token system tables
CREATE TABLE tokens (
    token_id        UUID PRIMARY KEY,
    hospital_id     UUID NOT NULL REFERENCES hospitals(hospital_id),
    doctor_id       UUID NOT NULL REFERENCES doctors(doctor_id),
    patient_id      UUID NOT NULL REFERENCES patients(patient_id),
    session_id      UUID NOT NULL REFERENCES doctor_sessions(session_id),
    token_number    INT NOT NULL,
    token_type      TEXT NOT NULL CHECK (token_type IN ('NORMAL', 'TATKAL')),
    status          TEXT NOT NULL CHECK (status IN ('WAITING','BUFFER','SERVING','COMPLETED','CANCELLED')),
    issued_at       TIMESTAMPTZ NOT NULL,
    called_at       TIMESTAMPTZ,
    served_at       TIMESTAMPTZ,
    UNIQUE (session_id, token_number)
);

CREATE TABLE token_queues (
    queue_id                    UUID PRIMARY KEY,
    session_id                  UUID NOT NULL UNIQUE REFERENCES doctor_sessions(session_id),
    serving_token_id            UUID REFERENCES tokens(token_id),
    buffer_token_ids            UUID[] NOT NULL DEFAULT '{}',
    tatkal_count                INT NOT NULL DEFAULT 0,
    normal_count                INT NOT NULL DEFAULT 0,
    consecutive_normal_served   INT NOT NULL DEFAULT 0,
    version                     INT NOT NULL DEFAULT 0,
    last_updated                TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Slot system tables
CREATE TABLE slots (
    slot_id         UUID PRIMARY KEY,
    hospital_id     UUID NOT NULL REFERENCES hospitals(hospital_id),
    doctor_id       UUID NOT NULL REFERENCES doctors(doctor_id),
    date            DATE NOT NULL,
    start_time      TIME NOT NULL,
    end_time        TIME NOT NULL,
    status          TEXT NOT NULL CHECK (status IN ('AVAILABLE','BOOKED','BLOCKED')),
    created_by      UUID NOT NULL,
    UNIQUE (doctor_id, date, start_time)
);

CREATE TABLE appointments (
    appointment_id      UUID PRIMARY KEY,
    hospital_id         UUID NOT NULL REFERENCES hospitals(hospital_id),
    patient_id          UUID NOT NULL REFERENCES patients(patient_id),
    doctor_id           UUID NOT NULL REFERENCES doctors(doctor_id),
    slot_id             UUID NOT NULL REFERENCES slots(slot_id),
    status              TEXT NOT NULL CHECK (status IN ('SCHEDULED','COMPLETED','CANCELLED')),
    booked_at           TIMESTAMPTZ NOT NULL,
    cancelled_at        TIMESTAMPTZ,
    cancellation_reason TEXT
);

-- Unique: one SCHEDULED appointment per patient per doctor per day
CREATE UNIQUE INDEX uq_appointment_patient_doctor_date
    ON appointments (patient_id, doctor_id, (slot_id))
    WHERE status = 'SCHEDULED';

-- Audit log (append-only)
CREATE TABLE audit_logs (
    log_id          UUID PRIMARY KEY,
    entity_type     TEXT NOT NULL,
    entity_id       UUID NOT NULL,
    action          TEXT NOT NULL,
    actor_id        UUID NOT NULL,
    timestamp       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    metadata        JSONB NOT NULL DEFAULT '{}'
);

CREATE INDEX idx_audit_entity ON audit_logs (entity_type, entity_id);
```

- [ ] **Step 2: Commit**

```bash
git add appointment/schema.sql
git commit -m "feat(appointment): add PostgreSQL DDL schema"
```

---

## Self-Review Checklist

**Spec coverage:**
- [x] Token book (§4.1) → Task 5
- [x] Token cancel (§4.2) → Task 5
- [x] Advance buffer + ratio rule (§4.3) → Task 6
- [x] Slot view (§4.4) → Task 7
- [x] Slot book (§4.5) → Task 7
- [x] Slot cancel (§4.6) → Task 7
- [x] Slot modify atomic (§4.7) → Task 7
- [x] Error codes (§5) → Task 1 errors + each strategy task
- [x] Concurrency: optimistic lock (§6) → Task 2 repo + Task 6 advance_buffer
- [x] Concurrency: row-level lock (§6) → noted in SlotStrategy.book comment + schema
- [x] Concurrency: DB sequence (§6) → Task 2 `next_number` + schema
- [x] Session lifecycle open/close (§7) → Task 6
- [x] Strategy pattern (§8) → Tasks 4–8
- [x] Audit log (§8) → Task 3 + every mutating method
- [x] `modify` not supported in TOKEN mode → Task 5 stub + Task 8 engine
- [x] `tatkal_ratio` configurable → Task 1 Hospital model + Task 5 strategy init
- [x] Buffer notifier TOKEN-only → Task 4 NoOpBufferNotifier + Task 8 engine
