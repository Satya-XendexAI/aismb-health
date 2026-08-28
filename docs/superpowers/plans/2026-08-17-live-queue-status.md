# Live OPD / Diagnostic Queue Status Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a standalone `QueueStatusService` that gives patients real-time OPD/diagnostic queue visibility, estimated wait times, push alerts, and a diagnostic hold/reinstate flow — all external dependencies behind swappable adapter interfaces.

**Architecture:** `QueueStatusService` orchestrates four adapter interfaces (`QueueDataProvider`, `QueueStateWriter`, `AlertNotifier`, `DelayStore`) plus an `InternalStore` for its own state. A pure `WaitCalculator` handles wait time math. An `AlertEvaluator` handles idempotent alert firing. All stubs are in-memory; real adapters wire in during integration.

**Tech Stack:** Python 3.11+, pytest 7+, dataclasses (stdlib), abc (stdlib). No external dependencies.

## Global Constraints

- All IDs are `uuid.UUID` — never bare strings
- All datetimes are `datetime.datetime` UTC naive — use `datetime.utcnow()`
- Error codes match spec exactly: `INVALID_DELAY`, `SESSION_NOT_FOUND`, `INVALID_ROOM_STATUS`, `DOCTOR_NOT_FOUND`, `TOKEN_NOT_HOLDABLE`, `INVALID_HOLD_REQUEST`, `ALREADY_ON_HOLD`, `ORDER_NOT_FOUND`, `TOKEN_NOT_ON_HOLD`, `DIAGNOSTICS_INCOMPLETE`, `STALE_QUEUE_VERSION`, `UNAUTHORISED`
- `evaluate_alerts` must be idempotent — never fire the same alert type twice per patient per session
- `AlertNotifier` failures do not raise — logged as `FAILED` in `AlertRecord`, flow continues
- Run all tests with: `pytest tests/queue_status/ -v`

---

### Task 1: Enums, Models, Errors

**Files:**
- Create: `queue_status/__init__.py`
- Create: `queue_status/enums.py`
- Create: `queue_status/models.py`
- Create: `queue_status/errors.py`
- Create: `tests/queue_status/__init__.py`
- Create: `tests/queue_status/test_models_errors.py`

**Interfaces:**
- Produces: all enums, dataclasses, and typed exceptions used by every later task

- [ ] **Step 1: Create directory scaffolding**

```bash
mkdir -p queue_status tests/queue_status
touch queue_status/__init__.py tests/queue_status/__init__.py
```

- [ ] **Step 2: Write failing tests**

`tests/queue_status/test_models_errors.py`:
```python
import pytest
from uuid import uuid4
from datetime import datetime, time

from queue_status.enums import (
    RoomStatusEnum, AlertType, AlertStatus, DiagnosticOrderStatus,
    NotificationStatus, BookingMode, TokenType, TokenStatus,
)
from queue_status.models import (
    AlertConfig, OPDQueueView, DiagnosticQueueView,
    PatientStatusView, DoctorDelay, RoomStatus,
    AlertRecord, DiagnosticHold, DiagnosticCompletionNotification,
)
from queue_status.errors import (
    QueueStatusError, InvalidDelay, SessionNotFound, InvalidRoomStatus,
    DoctorNotFound, TokenNotHoldable, InvalidHoldRequest, AlreadyOnHold,
    OrderNotFound, TokenNotOnHold, DiagnosticsIncomplete,
    StaleQueueVersion, Unauthorised,
)


def test_token_status_includes_diagnostic_hold():
    assert TokenStatus.DIAGNOSTIC_HOLD == "DIAGNOSTIC_HOLD"


def test_alert_config_defaults():
    cfg = AlertConfig(patient_id=uuid4(), session_id=uuid4())
    assert cfg.position_threshold == 3
    assert cfg.time_threshold_mins == 10
    assert cfg.position_alert_fired is False
    assert cfg.time_alert_fired is False


def test_patient_status_view_defaults():
    v = PatientStatusView(patient_id=uuid4(), hospital_id=uuid4(), retrieved_at=datetime.utcnow())
    assert v.opd_queue is None
    assert v.diagnostic_queues == []


def test_all_errors_are_queue_status_error():
    errors = [
        InvalidDelay(), SessionNotFound(), InvalidRoomStatus("X"),
        DoctorNotFound(), TokenNotHoldable("SERVING"), InvalidHoldRequest(),
        AlreadyOnHold(), OrderNotFound(), TokenNotOnHold(),
        DiagnosticsIncomplete(), StaleQueueVersion(), Unauthorised(),
    ]
    for e in errors:
        assert isinstance(e, QueueStatusError)
        assert e.code in e.args[0]


def test_error_codes_match_spec():
    assert InvalidDelay().code == "INVALID_DELAY"
    assert SessionNotFound().code == "SESSION_NOT_FOUND"
    assert InvalidRoomStatus("X").code == "INVALID_ROOM_STATUS"
    assert DoctorNotFound().code == "DOCTOR_NOT_FOUND"
    assert TokenNotHoldable("X").code == "TOKEN_NOT_HOLDABLE"
    assert InvalidHoldRequest().code == "INVALID_HOLD_REQUEST"
    assert AlreadyOnHold().code == "ALREADY_ON_HOLD"
    assert OrderNotFound().code == "ORDER_NOT_FOUND"
    assert TokenNotOnHold().code == "TOKEN_NOT_ON_HOLD"
    assert DiagnosticsIncomplete().code == "DIAGNOSTICS_INCOMPLETE"
    assert StaleQueueVersion().code == "STALE_QUEUE_VERSION"
    assert Unauthorised().code == "UNAUTHORISED"
```

- [ ] **Step 3: Run to verify failure**

```bash
pytest tests/queue_status/test_models_errors.py -v
```
Expected: `ModuleNotFoundError: No module named 'queue_status'`

- [ ] **Step 4: Write `queue_status/enums.py`**

```python
from enum import Enum


class RoomStatusEnum(str, Enum):
    OPEN = "OPEN"
    IN_CONSULTATION = "IN_CONSULTATION"
    BREAK = "BREAK"
    CLOSED = "CLOSED"


class AlertType(str, Enum):
    POSITION = "POSITION"
    TIME = "TIME"
    REINSTATED = "REINSTATED"
    BACK_IN_QUEUE = "BACK_IN_QUEUE"
    DIAGNOSTIC_COMPLETE = "DIAGNOSTIC_COMPLETE"


class AlertStatus(str, Enum):
    SENT = "SENT"
    FAILED = "FAILED"


class DiagnosticOrderStatus(str, Enum):
    WAITING = "WAITING"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"


class NotificationStatus(str, Enum):
    PENDING_REINSTATEMENT = "PENDING_REINSTATEMENT"
    REINSTATED = "REINSTATED"


class SlotQueueStatus(str, Enum):
    SCHEDULED = "SCHEDULED"
    UPCOMING = "UPCOMING"
    MISSED = "MISSED"


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
    SKIPPED = "SKIPPED"
    DIAGNOSTIC_HOLD = "DIAGNOSTIC_HOLD"
```

- [ ] **Step 5: Write `queue_status/models.py`**

```python
from dataclasses import dataclass, field
from datetime import datetime, time
from typing import Optional, List
from uuid import UUID

from .enums import (
    RoomStatusEnum, AlertType, AlertStatus, DiagnosticOrderStatus,
    NotificationStatus, SlotQueueStatus, BookingMode, TokenType,
)


@dataclass
class AlertConfig:
    patient_id: UUID
    session_id: UUID
    position_threshold: int = 3
    time_threshold_mins: int = 10
    position_alert_fired: bool = False
    time_alert_fired: bool = False


@dataclass
class OPDQueueView:
    session_id: UUID
    doctor_id: UUID
    doctor_name: str
    room_status: RoomStatusEnum
    booking_mode: BookingMode
    estimated_wait_mins: int
    doctor_delay_mins: int
    alert_config: AlertConfig
    token_number: Optional[int] = None
    token_type: Optional[TokenType] = None
    queue_position: Optional[int] = None
    tokens_ahead: Optional[int] = None
    slot_time: Optional[time] = None
    slot_status: Optional[SlotQueueStatus] = None
    hold_status: Optional[str] = None
    message: Optional[str] = None


@dataclass
class DiagnosticQueueView:
    order_id: UUID
    test_name: str
    estimated_wait_mins: int
    status: DiagnosticOrderStatus
    counter: Optional[str] = None
    queue_position: Optional[int] = None


@dataclass
class PatientStatusView:
    patient_id: UUID
    hospital_id: UUID
    retrieved_at: datetime
    opd_queue: Optional[OPDQueueView] = None
    diagnostic_queues: List[DiagnosticQueueView] = field(default_factory=list)


@dataclass
class DoctorDelay:
    delay_id: UUID
    session_id: UUID
    doctor_id: UUID
    delay_minutes: int
    entered_by: UUID
    entered_at: datetime


@dataclass
class RoomStatus:
    doctor_id: UUID
    hospital_id: UUID
    status: RoomStatusEnum
    updated_at: datetime
    updated_by: UUID


@dataclass
class AlertRecord:
    alert_id: UUID
    patient_id: UUID
    session_id: UUID
    alert_type: AlertType
    triggered_at: datetime
    status: AlertStatus
    payload: dict = field(default_factory=dict)


@dataclass
class DiagnosticHold:
    hold_id: UUID
    token_id: UUID
    patient_id: UUID
    session_id: UUID
    order_ids: List[UUID]
    sent_at: datetime
    attender_id: UUID


@dataclass
class DiagnosticCompletionNotification:
    notification_id: UUID
    patient_id: UUID
    token_id: UUID
    hold_id: UUID
    completed_at: datetime
    status: NotificationStatus
```

- [ ] **Step 6: Write `queue_status/errors.py`**

```python
class QueueStatusError(Exception):
    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(f"[{code}] {message}")


class InvalidDelay(QueueStatusError):
    def __init__(self):
        super().__init__("INVALID_DELAY", "delay_minutes must be >= 0")


class SessionNotFound(QueueStatusError):
    def __init__(self):
        super().__init__("SESSION_NOT_FOUND", "Doctor session not found")


class InvalidRoomStatus(QueueStatusError):
    def __init__(self, value: str):
        super().__init__("INVALID_ROOM_STATUS", f"Invalid room status: {value}")


class DoctorNotFound(QueueStatusError):
    def __init__(self):
        super().__init__("DOCTOR_NOT_FOUND", "Doctor not found in this hospital")


class TokenNotHoldable(QueueStatusError):
    def __init__(self, status: str):
        super().__init__("TOKEN_NOT_HOLDABLE", f"Token with status {status} cannot be held")


class InvalidHoldRequest(QueueStatusError):
    def __init__(self):
        super().__init__("INVALID_HOLD_REQUEST", "At least one diagnostic order required")


class AlreadyOnHold(QueueStatusError):
    def __init__(self):
        super().__init__("ALREADY_ON_HOLD", "Token is already on diagnostic hold")


class OrderNotFound(QueueStatusError):
    def __init__(self):
        super().__init__("ORDER_NOT_FOUND", "Diagnostic order not found")


class TokenNotOnHold(QueueStatusError):
    def __init__(self):
        super().__init__("TOKEN_NOT_ON_HOLD", "Token is not in DIAGNOSTIC_HOLD status")


class DiagnosticsIncomplete(QueueStatusError):
    def __init__(self):
        super().__init__("DIAGNOSTICS_INCOMPLETE", "Not all diagnostic orders are completed")


class StaleQueueVersion(QueueStatusError):
    def __init__(self):
        super().__init__("STALE_QUEUE_VERSION", "Queue modified concurrently — retry")


class Unauthorised(QueueStatusError):
    def __init__(self):
        super().__init__("UNAUTHORISED", "Attender not authorised for this hospital")
```

- [ ] **Step 7: Run tests**

```bash
pytest tests/queue_status/test_models_errors.py -v
```
Expected: all 5 tests PASS

- [ ] **Step 8: Commit**

```bash
git add queue_status/ tests/queue_status/
git commit -m "feat(queue-status): add enums, models, and typed errors"
```

---

### Task 2: Adapter Interfaces + In-Memory Stubs + Conftest

**Files:**
- Create: `queue_status/providers.py`
- Create: `queue_status/writers.py`
- Create: `queue_status/notifiers.py`
- Create: `queue_status/delay_store.py`
- Create: `tests/queue_status/conftest.py`

**Interfaces:**
- Produces:
  - `OPDPosition`, `DiagOrder`, `SessionConfig`, `WaitingPatient`, `DiagnosticTestConfig` DTOs
  - `QueueDataProvider` ABC + `InMemoryQueueDataProvider`
  - `QueueStateWriter` ABC + `InMemoryQueueStateWriter`
  - `AlertNotifier` ABC + `InMemoryAlertNotifier`
  - `DelayStore` ABC + `InMemoryDelayStore`
  - conftest fixtures: `session_id`, `patient_id`, `doctor_id`, `hospital_id`, `token_id`, `provider`, `writer`, `notifier`, `delay_store`

- [ ] **Step 1: Write `queue_status/providers.py`**

```python
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, time
from typing import Optional, List
from uuid import UUID

from .enums import BookingMode, TokenType, TokenStatus, DiagnosticOrderStatus


@dataclass
class OPDPosition:
    session_id: UUID
    doctor_id: UUID
    token_id: UUID
    token_number: int
    token_type: TokenType
    queue_position: int
    tokens_ahead: int
    booking_mode: BookingMode
    token_status: TokenStatus
    slot_time: Optional[time] = None
    slot_status: Optional[str] = None


@dataclass
class DiagOrder:
    order_id: UUID
    test_name: str
    queue_position: int
    status: DiagnosticOrderStatus
    counter: Optional[str] = None


@dataclass
class SessionConfig:
    session_id: UUID
    doctor_id: UUID
    doctor_name: str
    started_at: datetime
    avg_consultation_minutes: int
    booking_mode: BookingMode
    slot_duration_minutes: int = 0


@dataclass
class WaitingPatient:
    patient_id: UUID
    token_id: UUID
    queue_position: int
    token_type: TokenType
    token_status: TokenStatus


@dataclass
class DiagnosticTestConfig:
    test_name: str
    avg_turnaround_minutes: int = 15


class QueueDataProvider(ABC):
    @abstractmethod
    def get_opd_position(self, patient_id: UUID, hospital_id: UUID) -> Optional[OPDPosition]: ...
    @abstractmethod
    def get_diagnostic_orders(self, patient_id: UUID, hospital_id: UUID) -> List[DiagOrder]: ...
    @abstractmethod
    def get_session_config(self, session_id: UUID) -> Optional[SessionConfig]: ...
    @abstractmethod
    def get_waiting_patients(self, session_id: UUID) -> List[WaitingPatient]: ...
    @abstractmethod
    def get_diagnostic_test_config(self, test_name: str) -> DiagnosticTestConfig: ...


class InMemoryQueueDataProvider(QueueDataProvider):
    def __init__(self):
        self._positions: dict = {}       # patient_id -> OPDPosition
        self._orders: dict = {}          # patient_id -> List[DiagOrder]
        self._configs: dict = {}         # session_id -> SessionConfig
        self._waiting: dict = {}         # session_id -> List[WaitingPatient]
        self._test_configs: dict = {}    # test_name -> avg_turnaround_minutes

    def set_opd_position(self, patient_id: UUID, pos: OPDPosition):
        self._positions[patient_id] = pos

    def set_diagnostic_orders(self, patient_id: UUID, orders: List[DiagOrder]):
        self._orders[patient_id] = orders

    def set_session_config(self, config: SessionConfig):
        self._configs[config.session_id] = config

    def set_waiting_patients(self, session_id: UUID, patients: List[WaitingPatient]):
        self._waiting[session_id] = patients

    def set_test_config(self, test_name: str, avg_minutes: int):
        self._test_configs[test_name] = avg_minutes

    def get_opd_position(self, patient_id: UUID, hospital_id: UUID) -> Optional[OPDPosition]:
        return self._positions.get(patient_id)

    def get_diagnostic_orders(self, patient_id: UUID, hospital_id: UUID) -> List[DiagOrder]:
        return self._orders.get(patient_id, [])

    def get_session_config(self, session_id: UUID) -> Optional[SessionConfig]:
        return self._configs.get(session_id)

    def get_waiting_patients(self, session_id: UUID) -> List[WaitingPatient]:
        return self._waiting.get(session_id, [])

    def get_diagnostic_test_config(self, test_name: str) -> DiagnosticTestConfig:
        mins = self._test_configs.get(test_name, 15)
        return DiagnosticTestConfig(test_name=test_name, avg_turnaround_minutes=mins)
```

- [ ] **Step 2: Write `queue_status/writers.py`**

```python
from abc import ABC, abstractmethod
from collections import defaultdict
from typing import Dict, List
from uuid import UUID

from .enums import TokenType, TokenStatus


class QueueStateWriter(ABC):
    @abstractmethod
    def set_token_status(self, token_id: UUID, new_status: TokenStatus) -> None: ...
    @abstractmethod
    def adjust_queue_counts(self, session_id: UUID, token_type: TokenType, delta: int) -> None: ...
    @abstractmethod
    def remove_from_buffer(self, session_id: UUID, token_id: UUID) -> None: ...
    @abstractmethod
    def add_to_buffer(self, session_id: UUID, token_id: UUID) -> None: ...
    @abstractmethod
    def update_queue_version(self, session_id: UUID) -> bool: ...


class InMemoryQueueStateWriter(QueueStateWriter):
    def __init__(self):
        self._token_statuses: Dict[UUID, TokenStatus] = {}
        self._buffer: Dict[UUID, List[UUID]] = defaultdict(list)
        self._counts: Dict[UUID, Dict[str, int]] = defaultdict(lambda: {"NORMAL": 0, "TATKAL": 0})
        self._versions: Dict[UUID, int] = defaultdict(int)
        self.version_fail: bool = False  # set True in tests to simulate conflict

    def set_token_status(self, token_id: UUID, new_status: TokenStatus) -> None:
        self._token_statuses[token_id] = new_status

    def adjust_queue_counts(self, session_id: UUID, token_type: TokenType, delta: int) -> None:
        key = token_type.value
        self._counts[session_id][key] = max(0, self._counts[session_id][key] + delta)

    def remove_from_buffer(self, session_id: UUID, token_id: UUID) -> None:
        if token_id in self._buffer[session_id]:
            self._buffer[session_id].remove(token_id)

    def add_to_buffer(self, session_id: UUID, token_id: UUID) -> None:
        if token_id not in self._buffer[session_id]:
            self._buffer[session_id].append(token_id)

    def update_queue_version(self, session_id: UUID) -> bool:
        if self.version_fail:
            return False
        self._versions[session_id] += 1
        return True

    # Test helpers (not in ABC)
    def get_token_status(self, token_id: UUID) -> TokenStatus:
        return self._token_statuses.get(token_id)

    def get_buffer(self, session_id: UUID) -> List[UUID]:
        return list(self._buffer[session_id])

    def get_count(self, session_id: UUID, token_type: TokenType) -> int:
        return self._counts[session_id][token_type.value]
```

- [ ] **Step 3: Write `queue_status/notifiers.py`**

```python
from abc import ABC, abstractmethod
from typing import List, Tuple
from uuid import UUID

from .enums import AlertType


class AlertNotifier(ABC):
    @abstractmethod
    def send_to_patient(self, patient_id: UUID, alert_type: AlertType, payload: dict) -> None: ...
    @abstractmethod
    def send_to_attender(self, attender_id: UUID, alert_type: AlertType, payload: dict) -> None: ...


class InMemoryAlertNotifier(AlertNotifier):
    def __init__(self):
        self.patient_alerts: List[Tuple[UUID, AlertType, dict]] = []
        self.attender_alerts: List[Tuple[UUID, AlertType, dict]] = []
        self.should_fail: bool = False  # set True to simulate send failure

    def send_to_patient(self, patient_id: UUID, alert_type: AlertType, payload: dict) -> None:
        if self.should_fail:
            raise RuntimeError("Simulated notifier failure")
        self.patient_alerts.append((patient_id, alert_type, payload))

    def send_to_attender(self, attender_id: UUID, alert_type: AlertType, payload: dict) -> None:
        if self.should_fail:
            raise RuntimeError("Simulated notifier failure")
        self.attender_alerts.append((attender_id, alert_type, payload))
```

- [ ] **Step 4: Write `queue_status/delay_store.py`**

```python
from abc import ABC, abstractmethod
from typing import Dict, Optional
from uuid import UUID

from .models import DoctorDelay


class DelayStore(ABC):
    @abstractmethod
    def get(self, session_id: UUID) -> Optional[DoctorDelay]: ...
    @abstractmethod
    def upsert(self, delay: DoctorDelay) -> None: ...


class InMemoryDelayStore(DelayStore):
    def __init__(self):
        self._store: Dict[UUID, DoctorDelay] = {}

    def get(self, session_id: UUID) -> Optional[DoctorDelay]:
        return self._store.get(session_id)

    def upsert(self, delay: DoctorDelay) -> None:
        self._store[delay.session_id] = delay
```

- [ ] **Step 5: Write `tests/queue_status/conftest.py`**

```python
import pytest
from datetime import datetime, date
from uuid import uuid4

from queue_status.enums import BookingMode, TokenType, TokenStatus, DiagnosticOrderStatus
from queue_status.providers import (
    InMemoryQueueDataProvider, OPDPosition, DiagOrder, SessionConfig, WaitingPatient,
)
from queue_status.writers import InMemoryQueueStateWriter
from queue_status.notifiers import InMemoryAlertNotifier
from queue_status.delay_store import InMemoryDelayStore


@pytest.fixture
def hospital_id(): return uuid4()
@pytest.fixture
def patient_id(): return uuid4()
@pytest.fixture
def doctor_id(): return uuid4()
@pytest.fixture
def session_id(): return uuid4()
@pytest.fixture
def token_id(): return uuid4()
@pytest.fixture
def attender_id(): return uuid4()


@pytest.fixture
def session_config(session_id, doctor_id):
    return SessionConfig(
        session_id=session_id,
        doctor_id=doctor_id,
        doctor_name="Dr. Priya",
        started_at=datetime.utcnow(),
        avg_consultation_minutes=10,
        booking_mode=BookingMode.TOKEN,
    )


@pytest.fixture
def opd_position(session_id, doctor_id, token_id):
    return OPDPosition(
        session_id=session_id,
        doctor_id=doctor_id,
        token_id=token_id,
        token_number=5,
        token_type=TokenType.NORMAL,
        queue_position=4,
        tokens_ahead=4,
        booking_mode=BookingMode.TOKEN,
        token_status=TokenStatus.WAITING,
    )


@pytest.fixture
def provider(patient_id, hospital_id, session_id, session_config, opd_position):
    p = InMemoryQueueDataProvider()
    p.set_opd_position(patient_id, opd_position)
    p.set_session_config(session_config)
    p.set_waiting_patients(session_id, [
        WaitingPatient(patient_id, opd_position.token_id, 4, TokenType.NORMAL, TokenStatus.WAITING)
    ])
    return p


@pytest.fixture
def writer(): return InMemoryQueueStateWriter()
@pytest.fixture
def notifier(): return InMemoryAlertNotifier()
@pytest.fixture
def delay_store(): return InMemoryDelayStore()
```

- [ ] **Step 6: Run a smoke test to verify imports work**

```bash
pytest tests/queue_status/ -v --collect-only
```
Expected: conftest loads without error

- [ ] **Step 7: Commit**

```bash
git add queue_status/providers.py queue_status/writers.py \
        queue_status/notifiers.py queue_status/delay_store.py \
        tests/queue_status/conftest.py
git commit -m "feat(queue-status): add adapter interfaces and in-memory stubs"
```

---

### Task 3: InternalStore

**Files:**
- Create: `queue_status/internal_store.py`
- Append tests to: `tests/queue_status/test_models_errors.py`

**Interfaces:**
- Produces:
  - `InternalStore` ABC + `InMemoryInternalStore`
  - Methods: `get_alert_config`, `save_alert_config`, `save_alert_record`, `get_alert_records`, `get_room_status`, `save_room_status`, `get_hold_by_token`, `save_hold`, `get_notification_by_token`, `save_notification`, `get_order_status`, `set_order_status`

- [ ] **Step 1: Append failing tests**

Append to `tests/queue_status/test_models_errors.py`:
```python
from queue_status.internal_store import InMemoryInternalStore
from queue_status.enums import (
    RoomStatusEnum, AlertType, AlertStatus,
    DiagnosticOrderStatus, NotificationStatus,
)
from queue_status.models import (
    AlertConfig, AlertRecord, RoomStatus,
    DiagnosticHold, DiagnosticCompletionNotification,
)
from datetime import datetime
from uuid import uuid4


def test_internal_store_alert_config_created_with_defaults():
    store = InMemoryInternalStore()
    pid, sid = uuid4(), uuid4()
    cfg = store.get_or_create_alert_config(pid, sid)
    assert cfg.position_threshold == 3
    assert cfg.time_threshold_mins == 10
    assert cfg.position_alert_fired is False


def test_internal_store_alert_config_persisted():
    store = InMemoryInternalStore()
    pid, sid = uuid4(), uuid4()
    cfg = store.get_or_create_alert_config(pid, sid)
    cfg.position_alert_fired = True
    store.save_alert_config(cfg)
    cfg2 = store.get_or_create_alert_config(pid, sid)
    assert cfg2.position_alert_fired is True


def test_internal_store_alert_records_append():
    store = InMemoryInternalStore()
    sid = uuid4()
    rec = AlertRecord(uuid4(), uuid4(), sid, AlertType.POSITION,
                      datetime.utcnow(), AlertStatus.SENT, {})
    store.save_alert_record(rec)
    assert len(store.get_alert_records(sid)) == 1


def test_internal_store_room_status_roundtrip():
    store = InMemoryInternalStore()
    did = uuid4()
    rs = RoomStatus(did, uuid4(), RoomStatusEnum.OPEN, datetime.utcnow(), uuid4())
    store.save_room_status(rs)
    assert store.get_room_status(did).status == RoomStatusEnum.OPEN


def test_internal_store_hold_by_token():
    store = InMemoryInternalStore()
    tid = uuid4()
    hold = DiagnosticHold(uuid4(), tid, uuid4(), uuid4(), [uuid4()], datetime.utcnow(), uuid4())
    store.save_hold(hold)
    assert store.get_hold_by_token(tid) is not None


def test_internal_store_order_status_defaults_none():
    store = InMemoryInternalStore()
    assert store.get_order_status(uuid4()) is None


def test_internal_store_order_status_set_and_get():
    store = InMemoryInternalStore()
    oid = uuid4()
    store.set_order_status(oid, DiagnosticOrderStatus.COMPLETED)
    assert store.get_order_status(oid) == DiagnosticOrderStatus.COMPLETED
```

- [ ] **Step 2: Run to verify failure**

```bash
pytest tests/queue_status/test_models_errors.py -v -k "internal_store"
```
Expected: `ImportError`

- [ ] **Step 3: Write `queue_status/internal_store.py`**

```python
from abc import ABC, abstractmethod
from copy import copy
from typing import Dict, List, Optional
from uuid import UUID

from .enums import DiagnosticOrderStatus, RoomStatusEnum
from .models import (
    AlertConfig, AlertRecord, RoomStatus,
    DiagnosticHold, DiagnosticCompletionNotification,
)


class InternalStore(ABC):
    @abstractmethod
    def get_or_create_alert_config(self, patient_id: UUID, session_id: UUID) -> AlertConfig: ...
    @abstractmethod
    def save_alert_config(self, config: AlertConfig) -> None: ...
    @abstractmethod
    def save_alert_record(self, record: AlertRecord) -> None: ...
    @abstractmethod
    def get_alert_records(self, session_id: UUID) -> List[AlertRecord]: ...
    @abstractmethod
    def get_room_status(self, doctor_id: UUID) -> Optional[RoomStatus]: ...
    @abstractmethod
    def save_room_status(self, status: RoomStatus) -> None: ...
    @abstractmethod
    def get_hold_by_token(self, token_id: UUID) -> Optional[DiagnosticHold]: ...
    @abstractmethod
    def save_hold(self, hold: DiagnosticHold) -> None: ...
    @abstractmethod
    def get_notification_by_token(self, token_id: UUID) -> Optional[DiagnosticCompletionNotification]: ...
    @abstractmethod
    def save_notification(self, notification: DiagnosticCompletionNotification) -> None: ...
    @abstractmethod
    def get_order_status(self, order_id: UUID) -> Optional[DiagnosticOrderStatus]: ...
    @abstractmethod
    def set_order_status(self, order_id: UUID, status: DiagnosticOrderStatus) -> None: ...


class InMemoryInternalStore(InternalStore):
    def __init__(self):
        self._alert_configs: Dict[tuple, AlertConfig] = {}
        self._alert_records: List[AlertRecord] = []
        self._room_statuses: Dict[UUID, RoomStatus] = {}
        self._holds: Dict[UUID, DiagnosticHold] = {}
        self._notifications: Dict[UUID, DiagnosticCompletionNotification] = {}
        self._order_statuses: Dict[UUID, DiagnosticOrderStatus] = {}

    def get_or_create_alert_config(self, patient_id: UUID, session_id: UUID) -> AlertConfig:
        key = (patient_id, session_id)
        if key not in self._alert_configs:
            self._alert_configs[key] = AlertConfig(patient_id=patient_id, session_id=session_id)
        return copy(self._alert_configs[key])

    def save_alert_config(self, config: AlertConfig) -> None:
        self._alert_configs[(config.patient_id, config.session_id)] = copy(config)

    def save_alert_record(self, record: AlertRecord) -> None:
        self._alert_records.append(record)

    def get_alert_records(self, session_id: UUID) -> List[AlertRecord]:
        return [r for r in self._alert_records if r.session_id == session_id]

    def get_room_status(self, doctor_id: UUID) -> Optional[RoomStatus]:
        return self._room_statuses.get(doctor_id)

    def save_room_status(self, status: RoomStatus) -> None:
        self._room_statuses[status.doctor_id] = copy(status)

    def get_hold_by_token(self, token_id: UUID) -> Optional[DiagnosticHold]:
        return self._holds.get(token_id)

    def save_hold(self, hold: DiagnosticHold) -> None:
        self._holds[hold.token_id] = hold

    def get_notification_by_token(self, token_id: UUID) -> Optional[DiagnosticCompletionNotification]:
        return self._notifications.get(token_id)

    def save_notification(self, notification: DiagnosticCompletionNotification) -> None:
        self._notifications[notification.token_id] = notification

    def get_order_status(self, order_id: UUID) -> Optional[DiagnosticOrderStatus]:
        return self._order_statuses.get(order_id)

    def set_order_status(self, order_id: UUID, status: DiagnosticOrderStatus) -> None:
        self._order_statuses[order_id] = status
```

- [ ] **Step 4: Run all tests**

```bash
pytest tests/queue_status/test_models_errors.py -v
```
Expected: all tests PASS

- [ ] **Step 5: Commit**

```bash
git add queue_status/internal_store.py tests/queue_status/test_models_errors.py
git commit -m "feat(queue-status): add InternalStore with alert config, hold, room status"
```

---

### Task 4: WaitCalculator

**Files:**
- Create: `queue_status/wait_calculator.py`
- Create: `tests/queue_status/test_wait_calculator.py`

**Interfaces:**
- Produces:
  - `compute_wait(booking_mode, tokens_ahead, avg_consultation_minutes, doctor_delay_mins, slot_time, now) -> int`
  - `compute_diagnostic_wait(queue_position, avg_turnaround_minutes) -> int`

- [ ] **Step 1: Write failing tests**

`tests/queue_status/test_wait_calculator.py`:
```python
import pytest
from datetime import datetime, time

from queue_status.enums import BookingMode
from queue_status.wait_calculator import compute_wait, compute_diagnostic_wait


def test_token_mode_basic():
    result = compute_wait(BookingMode.TOKEN, tokens_ahead=3,
                          avg_consultation_minutes=10, doctor_delay_mins=0)
    assert result == 30


def test_token_mode_with_delay():
    result = compute_wait(BookingMode.TOKEN, tokens_ahead=3,
                          avg_consultation_minutes=10, doctor_delay_mins=15)
    assert result == 45


def test_token_mode_never_negative():
    result = compute_wait(BookingMode.TOKEN, tokens_ahead=0,
                          avg_consultation_minutes=10, doctor_delay_mins=-5)
    assert result == 0


def test_slot_mode_future_slot():
    now = datetime(2026, 8, 17, 10, 0, 0)
    slot_time = time(10, 30)
    result = compute_wait(BookingMode.SLOT, tokens_ahead=0,
                          avg_consultation_minutes=0, doctor_delay_mins=0,
                          slot_time=slot_time, now=now)
    assert result == 30


def test_slot_mode_past_slot_returns_zero():
    now = datetime(2026, 8, 17, 11, 0, 0)
    slot_time = time(10, 0)
    result = compute_wait(BookingMode.SLOT, tokens_ahead=0,
                          avg_consultation_minutes=0, doctor_delay_mins=0,
                          slot_time=slot_time, now=now)
    assert result == 0


def test_slot_mode_with_delay():
    now = datetime(2026, 8, 17, 10, 0, 0)
    slot_time = time(10, 20)
    result = compute_wait(BookingMode.SLOT, tokens_ahead=0,
                          avg_consultation_minutes=0, doctor_delay_mins=10,
                          slot_time=slot_time, now=now)
    assert result == 30


def test_diagnostic_wait():
    assert compute_diagnostic_wait(queue_position=3, avg_turnaround_minutes=15) == 45


def test_diagnostic_wait_zero_position():
    assert compute_diagnostic_wait(queue_position=0, avg_turnaround_minutes=15) == 0
```

- [ ] **Step 2: Run to verify failure**

```bash
pytest tests/queue_status/test_wait_calculator.py -v
```
Expected: `ImportError`

- [ ] **Step 3: Write `queue_status/wait_calculator.py`**

```python
from datetime import datetime, time
from typing import Optional

from .enums import BookingMode


def compute_wait(
    booking_mode: BookingMode,
    tokens_ahead: int,
    avg_consultation_minutes: int,
    doctor_delay_mins: int,
    slot_time: Optional[time] = None,
    now: Optional[datetime] = None,
) -> int:
    if booking_mode == BookingMode.TOKEN:
        base_wait = tokens_ahead * avg_consultation_minutes
        return max(0, base_wait + doctor_delay_mins)

    # SLOT mode
    if now is None:
        now = datetime.utcnow()
    slot_dt = datetime.combine(now.date(), slot_time)
    diff_seconds = (slot_dt - now).total_seconds()
    base_wait = max(0.0, diff_seconds / 60)
    return max(0, int(base_wait) + doctor_delay_mins)


def compute_diagnostic_wait(queue_position: int, avg_turnaround_minutes: int) -> int:
    return queue_position * avg_turnaround_minutes
```

- [ ] **Step 4: Run tests**

```bash
pytest tests/queue_status/test_wait_calculator.py -v
```
Expected: all 8 tests PASS

- [ ] **Step 5: Commit**

```bash
git add queue_status/wait_calculator.py tests/queue_status/test_wait_calculator.py
git commit -m "feat(queue-status): add WaitCalculator pure functions"
```

---

### Task 5: AlertEvaluator

**Files:**
- Create: `queue_status/alert_evaluator.py`
- Create: `tests/queue_status/test_alert_evaluator.py`

**Interfaces:**
- Consumes: `QueueDataProvider`, `DelayStore`, `InternalStore`, `AlertNotifier`; `compute_wait`; `AlertType`, `AlertStatus`; `AlertRecord`, `AlertConfig`
- Produces: `AlertEvaluator.evaluate(session_id: UUID) -> None`

- [ ] **Step 1: Write failing tests**

`tests/queue_status/test_alert_evaluator.py`:
```python
import pytest
from uuid import uuid4
from datetime import datetime

from queue_status.enums import (
    BookingMode, TokenType, TokenStatus, AlertType, AlertStatus,
)
from queue_status.providers import (
    InMemoryQueueDataProvider, OPDPosition, SessionConfig, WaitingPatient,
)
from queue_status.notifiers import InMemoryAlertNotifier
from queue_status.delay_store import InMemoryDelayStore
from queue_status.internal_store import InMemoryInternalStore
from queue_status.alert_evaluator import AlertEvaluator


@pytest.fixture
def sid(): return uuid4()
@pytest.fixture
def pid(): return uuid4()
@pytest.fixture
def tid(): return uuid4()
@pytest.fixture
def did(): return uuid4()


def _make_evaluator(sid, pid, tid, did,
                    queue_position=2, delay_mins=0,
                    pos_threshold=3, time_threshold=30,
                    notifier_fail=False):
    provider = InMemoryQueueDataProvider()
    provider.set_session_config(SessionConfig(
        session_id=sid, doctor_id=did, doctor_name="Dr. A",
        started_at=datetime.utcnow(), avg_consultation_minutes=10,
        booking_mode=BookingMode.TOKEN,
    ))
    provider.set_waiting_patients(sid, [
        WaitingPatient(pid, tid, queue_position, TokenType.NORMAL, TokenStatus.WAITING)
    ])
    delay_store = InMemoryDelayStore()
    internal_store = InMemoryInternalStore()
    cfg = internal_store.get_or_create_alert_config(pid, sid)
    cfg.position_threshold = pos_threshold
    cfg.time_threshold_mins = time_threshold
    internal_store.save_alert_config(cfg)
    notifier = InMemoryAlertNotifier()
    notifier.should_fail = notifier_fail
    evaluator = AlertEvaluator(provider, delay_store, internal_store, notifier)
    return evaluator, notifier, internal_store


def test_position_alert_fires_when_at_threshold(sid, pid, tid, did):
    ev, notifier, store = _make_evaluator(sid, pid, tid, did,
                                          queue_position=3, pos_threshold=3)
    ev.evaluate(sid)
    assert any(a[1] == AlertType.POSITION for a in notifier.patient_alerts)


def test_position_alert_does_not_fire_above_threshold(sid, pid, tid, did):
    ev, notifier, _ = _make_evaluator(sid, pid, tid, did,
                                      queue_position=5, pos_threshold=3)
    ev.evaluate(sid)
    assert not any(a[1] == AlertType.POSITION for a in notifier.patient_alerts)


def test_time_alert_fires_when_below_threshold(sid, pid, tid, did):
    # 2 tokens × 10 min = 20 min wait < threshold of 30
    ev, notifier, _ = _make_evaluator(sid, pid, tid, did,
                                      queue_position=2, delay_mins=0, time_threshold=30)
    ev.evaluate(sid)
    assert any(a[1] == AlertType.TIME for a in notifier.patient_alerts)


def test_time_alert_does_not_fire_above_threshold(sid, pid, tid, did):
    # 5 tokens × 10 min = 50 min wait > threshold of 30
    ev, notifier, _ = _make_evaluator(sid, pid, tid, did,
                                      queue_position=5, delay_mins=0, time_threshold=30)
    ev.evaluate(sid)
    assert not any(a[1] == AlertType.TIME for a in notifier.patient_alerts)


def test_position_alert_fires_only_once(sid, pid, tid, did):
    ev, notifier, _ = _make_evaluator(sid, pid, tid, did,
                                      queue_position=2, pos_threshold=3)
    ev.evaluate(sid)
    ev.evaluate(sid)
    position_alerts = [a for a in notifier.patient_alerts if a[1] == AlertType.POSITION]
    assert len(position_alerts) == 1


def test_alert_record_saved_on_success(sid, pid, tid, did):
    ev, notifier, store = _make_evaluator(sid, pid, tid, did,
                                          queue_position=2, pos_threshold=3)
    ev.evaluate(sid)
    records = store.get_alert_records(sid)
    assert any(r.status == AlertStatus.SENT for r in records)


def test_notifier_failure_saved_as_failed_record(sid, pid, tid, did):
    ev, notifier, store = _make_evaluator(sid, pid, tid, did,
                                          queue_position=2, pos_threshold=3,
                                          notifier_fail=True)
    ev.evaluate(sid)  # must not raise
    records = store.get_alert_records(sid)
    assert any(r.status == AlertStatus.FAILED for r in records)
```

- [ ] **Step 2: Run to verify failure**

```bash
pytest tests/queue_status/test_alert_evaluator.py -v
```
Expected: `ImportError`

- [ ] **Step 3: Write `queue_status/alert_evaluator.py`**

```python
from datetime import datetime
from uuid import UUID, uuid4

from .enums import AlertType, AlertStatus, BookingMode
from .models import AlertRecord
from .providers import QueueDataProvider
from .delay_store import DelayStore
from .internal_store import InternalStore
from .notifiers import AlertNotifier
from .wait_calculator import compute_wait


class AlertEvaluator:
    def __init__(self, provider: QueueDataProvider, delay_store: DelayStore,
                 internal_store: InternalStore, notifier: AlertNotifier):
        self._provider = provider
        self._delay_store = delay_store
        self._store = internal_store
        self._notifier = notifier

    def evaluate(self, session_id: UUID) -> None:
        config = self._provider.get_session_config(session_id)
        if config is None:
            return
        delay_entry = self._delay_store.get(session_id)
        delay_mins = delay_entry.delay_minutes if delay_entry else 0

        for wp in self._provider.get_waiting_patients(session_id):
            wait = compute_wait(
                booking_mode=config.booking_mode,
                tokens_ahead=wp.queue_position,
                avg_consultation_minutes=config.avg_consultation_minutes,
                doctor_delay_mins=delay_mins,
            )
            cfg = self._store.get_or_create_alert_config(wp.patient_id, session_id)

            if not cfg.position_alert_fired and wp.queue_position <= cfg.position_threshold:
                self._fire(wp.patient_id, session_id, AlertType.POSITION,
                           {"position": wp.queue_position})
                cfg.position_alert_fired = True
                self._store.save_alert_config(cfg)

            if not cfg.time_alert_fired and wait <= cfg.time_threshold_mins:
                self._fire(wp.patient_id, session_id, AlertType.TIME,
                           {"wait_mins": wait})
                cfg.time_alert_fired = True
                self._store.save_alert_config(cfg)

    def _fire(self, patient_id: UUID, session_id: UUID,
              alert_type: AlertType, payload: dict) -> None:
        status = AlertStatus.SENT
        try:
            self._notifier.send_to_patient(patient_id, alert_type, payload)
        except Exception:
            status = AlertStatus.FAILED
        self._store.save_alert_record(AlertRecord(
            alert_id=uuid4(), patient_id=patient_id, session_id=session_id,
            alert_type=alert_type, triggered_at=datetime.utcnow(),
            status=status, payload=payload,
        ))
```

- [ ] **Step 4: Run tests**

```bash
pytest tests/queue_status/test_alert_evaluator.py -v
```
Expected: all 7 tests PASS

- [ ] **Step 5: Commit**

```bash
git add queue_status/alert_evaluator.py tests/queue_status/test_alert_evaluator.py
git commit -m "feat(queue-status): add AlertEvaluator with idempotent alert firing"
```

---

### Task 6: QueueStatusService — get_patient_status and update_room_status

**Files:**
- Create: `queue_status/service.py`
- Create: `tests/queue_status/test_service_status.py`

**Interfaces:**
- Consumes: all adapters + `AlertEvaluator`; `compute_wait`, `compute_diagnostic_wait`
- Produces:
  - `QueueStatusService.__init__(provider, writer, notifier, delay_store, internal_store)`
  - `QueueStatusService.get_patient_status(patient_id, hospital_id) -> PatientStatusView`
  - `QueueStatusService.update_room_status(doctor_id, hospital_id, new_status, attender_id) -> RoomStatus`

- [ ] **Step 1: Write failing tests**

`tests/queue_status/test_service_status.py`:
```python
import pytest
from uuid import uuid4
from datetime import datetime, time

from queue_status.enums import (
    BookingMode, TokenType, TokenStatus, RoomStatusEnum, DiagnosticOrderStatus,
)
from queue_status.errors import InvalidRoomStatus, DoctorNotFound
from queue_status.providers import (
    InMemoryQueueDataProvider, OPDPosition, SessionConfig, DiagOrder, WaitingPatient,
)
from queue_status.writers import InMemoryQueueStateWriter
from queue_status.notifiers import InMemoryAlertNotifier
from queue_status.delay_store import InMemoryDelayStore
from queue_status.internal_store import InMemoryInternalStore
from queue_status.service import QueueStatusService


@pytest.fixture
def sid(): return uuid4()
@pytest.fixture
def pid(): return uuid4()
@pytest.fixture
def did(): return uuid4()
@pytest.fixture
def hid(): return uuid4()
@pytest.fixture
def tid(): return uuid4()
@pytest.fixture
def att_id(): return uuid4()


@pytest.fixture
def svc(sid, pid, did, hid, tid):
    provider = InMemoryQueueDataProvider()
    provider.set_session_config(SessionConfig(
        session_id=sid, doctor_id=did, doctor_name="Dr. Priya",
        started_at=datetime.utcnow(), avg_consultation_minutes=10,
        booking_mode=BookingMode.TOKEN,
    ))
    provider.set_opd_position(pid, OPDPosition(
        session_id=sid, doctor_id=did, token_id=tid,
        token_number=3, token_type=TokenType.NORMAL,
        queue_position=2, tokens_ahead=2,
        booking_mode=BookingMode.TOKEN,
        token_status=TokenStatus.WAITING,
    ))
    provider.set_waiting_patients(sid, [
        WaitingPatient(pid, tid, 2, TokenType.NORMAL, TokenStatus.WAITING)
    ])
    provider.set_diagnostic_orders(pid, [
        DiagOrder(order_id=uuid4(), test_name="CBC", queue_position=3,
                  status=DiagnosticOrderStatus.WAITING, counter="Lab 1"),
    ])
    return QueueStatusService(
        provider=provider,
        writer=InMemoryQueueStateWriter(),
        notifier=InMemoryAlertNotifier(),
        delay_store=InMemoryDelayStore(),
        internal_store=InMemoryInternalStore(),
    )


def test_get_patient_status_returns_view(svc, pid, hid):
    view = svc.get_patient_status(pid, hid)
    assert view.patient_id == pid
    assert view.opd_queue is not None
    assert view.opd_queue.doctor_name == "Dr. Priya"


def test_get_patient_status_token_mode_fields(svc, pid, hid):
    view = svc.get_patient_status(pid, hid)
    assert view.opd_queue.token_number == 3
    assert view.opd_queue.queue_position == 2
    assert view.opd_queue.estimated_wait_mins == 20  # 2 × 10 min


def test_get_patient_status_no_opd_returns_none(svc, hid):
    view = svc.get_patient_status(uuid4(), hid)
    assert view.opd_queue is None


def test_get_patient_status_diagnostic_orders_included(svc, pid, hid):
    view = svc.get_patient_status(pid, hid)
    assert len(view.diagnostic_queues) == 1
    assert view.diagnostic_queues[0].test_name == "CBC"
    assert view.diagnostic_queues[0].estimated_wait_mins == 45  # 3 × 15


def test_get_patient_status_diagnostic_hold_shows_message(svc, pid, hid, sid, tid):
    svc._provider.set_opd_position(pid, OPDPosition(
        session_id=sid, doctor_id=uuid4(), token_id=tid,
        token_number=3, token_type=TokenType.NORMAL,
        queue_position=2, tokens_ahead=2,
        booking_mode=BookingMode.TOKEN,
        token_status=TokenStatus.DIAGNOSTIC_HOLD,
    ))
    view = svc.get_patient_status(pid, hid)
    assert view.opd_queue.hold_status == "DIAGNOSTIC_HOLD"
    assert view.opd_queue.queue_position is None
    assert "tests" in view.opd_queue.message.lower()


def test_room_status_is_open_by_default(svc, pid, hid, did):
    view = svc.get_patient_status(pid, hid)
    assert view.opd_queue.room_status == RoomStatusEnum.OPEN


def test_update_room_status_persists(svc, did, hid, att_id):
    rs = svc.update_room_status(did, hid, RoomStatusEnum.IN_CONSULTATION, att_id)
    assert rs.status == RoomStatusEnum.IN_CONSULTATION


def test_update_room_status_invalid_reflected_in_next_status_call(svc, pid, did, hid, att_id):
    svc.update_room_status(did, hid, RoomStatusEnum.BREAK, att_id)
    view = svc.get_patient_status(pid, hid)
    assert view.opd_queue.room_status == RoomStatusEnum.BREAK
```

- [ ] **Step 2: Run to verify failure**

```bash
pytest tests/queue_status/test_service_status.py -v
```
Expected: `ImportError`

- [ ] **Step 3: Write `queue_status/service.py`** (get_patient_status + update_room_status only; stubs for remaining methods)

```python
from datetime import datetime
from typing import Optional
from uuid import UUID, uuid4

from .alert_evaluator import AlertEvaluator
from .delay_store import DelayStore
from .enums import RoomStatusEnum, TokenStatus, DiagnosticOrderStatus, AlertType, AlertStatus
from .errors import (
    InvalidDelay, SessionNotFound, InvalidRoomStatus, DoctorNotFound,
    TokenNotHoldable, InvalidHoldRequest, AlreadyOnHold,
    OrderNotFound, TokenNotOnHold, DiagnosticsIncomplete, StaleQueueVersion,
)
from .internal_store import InternalStore
from .models import (
    OPDQueueView, PatientStatusView, DiagnosticQueueView,
    RoomStatus, DoctorDelay, AlertRecord,
    DiagnosticHold, DiagnosticCompletionNotification,
)
from .notifiers import AlertNotifier
from .providers import QueueDataProvider
from .wait_calculator import compute_wait, compute_diagnostic_wait
from .writers import QueueStateWriter


class QueueStatusService:
    def __init__(
        self,
        provider: QueueDataProvider,
        writer: QueueStateWriter,
        notifier: AlertNotifier,
        delay_store: DelayStore,
        internal_store: InternalStore,
    ):
        self._provider = provider
        self._writer = writer
        self._notifier = notifier
        self._delay_store = delay_store
        self._store = internal_store
        self._evaluator = AlertEvaluator(provider, delay_store, internal_store, notifier)

    def get_patient_status(self, patient_id: UUID, hospital_id: UUID) -> PatientStatusView:
        opd_pos = self._provider.get_opd_position(patient_id, hospital_id)
        opd_view = None

        if opd_pos is not None:
            delay_entry = self._delay_store.get(opd_pos.session_id)
            delay_mins = delay_entry.delay_minutes if delay_entry else 0
            room_st = self._store.get_room_status(opd_pos.doctor_id)
            room_status = room_st.status if room_st else RoomStatusEnum.OPEN
            alert_cfg = self._store.get_or_create_alert_config(patient_id, opd_pos.session_id)

            if opd_pos.token_status == TokenStatus.DIAGNOSTIC_HOLD:
                opd_view = OPDQueueView(
                    session_id=opd_pos.session_id,
                    doctor_id=opd_pos.doctor_id,
                    doctor_name=self._get_doctor_name(opd_pos.session_id),
                    room_status=room_status,
                    booking_mode=opd_pos.booking_mode,
                    estimated_wait_mins=0,
                    doctor_delay_mins=delay_mins,
                    alert_config=alert_cfg,
                    hold_status="DIAGNOSTIC_HOLD",
                    message="Please complete your tests. You will be called back when ready.",
                )
            else:
                session_cfg = self._provider.get_session_config(opd_pos.session_id)
                wait = compute_wait(
                    booking_mode=opd_pos.booking_mode,
                    tokens_ahead=opd_pos.tokens_ahead,
                    avg_consultation_minutes=session_cfg.avg_consultation_minutes if session_cfg else 10,
                    doctor_delay_mins=delay_mins,
                    slot_time=opd_pos.slot_time,
                )
                opd_view = OPDQueueView(
                    session_id=opd_pos.session_id,
                    doctor_id=opd_pos.doctor_id,
                    doctor_name=self._get_doctor_name(opd_pos.session_id),
                    room_status=room_status,
                    booking_mode=opd_pos.booking_mode,
                    estimated_wait_mins=wait,
                    doctor_delay_mins=delay_mins,
                    alert_config=alert_cfg,
                    token_number=opd_pos.token_number,
                    token_type=opd_pos.token_type,
                    queue_position=opd_pos.queue_position,
                    tokens_ahead=opd_pos.tokens_ahead,
                    slot_time=opd_pos.slot_time,
                )

        diag_orders = self._provider.get_diagnostic_orders(patient_id, hospital_id)
        diag_views = []
        for order in diag_orders:
            if order.status == DiagnosticOrderStatus.COMPLETED:
                continue
            test_cfg = self._provider.get_diagnostic_test_config(order.test_name)
            wait = compute_diagnostic_wait(order.queue_position, test_cfg.avg_turnaround_minutes)
            diag_views.append(DiagnosticQueueView(
                order_id=order.order_id,
                test_name=order.test_name,
                estimated_wait_mins=wait,
                status=order.status,
                counter=order.counter,
                queue_position=order.queue_position,
            ))

        return PatientStatusView(
            patient_id=patient_id,
            hospital_id=hospital_id,
            retrieved_at=datetime.utcnow(),
            opd_queue=opd_view,
            diagnostic_queues=diag_views,
        )

    def update_room_status(self, doctor_id: UUID, hospital_id: UUID,
                           new_status: RoomStatusEnum, attender_id: UUID) -> RoomStatus:
        rs = RoomStatus(
            doctor_id=doctor_id,
            hospital_id=hospital_id,
            status=new_status,
            updated_at=datetime.utcnow(),
            updated_by=attender_id,
        )
        self._store.save_room_status(rs)
        return rs

    def _get_doctor_name(self, session_id: UUID) -> str:
        cfg = self._provider.get_session_config(session_id)
        return cfg.doctor_name if cfg else "Unknown"

    # Stubs — implemented in Tasks 7–9
    def update_doctor_delay(self, session_id: UUID, delay_minutes: int, attender_id: UUID) -> int:
        raise NotImplementedError

    def send_to_diagnostics(self, token_id: UUID, order_ids: list, attender_id: UUID) -> None:
        raise NotImplementedError

    def mark_diagnostic_complete(self, order_id: UUID, staff_id: UUID) -> None:
        raise NotImplementedError

    def reinstate_diagnostic_patient(self, token_id: UUID, attender_id: UUID) -> None:
        raise NotImplementedError
```

- [ ] **Step 4: Run tests**

```bash
pytest tests/queue_status/test_service_status.py -v
```
Expected: all 9 tests PASS

- [ ] **Step 5: Commit**

```bash
git add queue_status/service.py tests/queue_status/test_service_status.py
git commit -m "feat(queue-status): implement get_patient_status and update_room_status"
```

---

### Task 7: QueueStatusService — update_doctor_delay

**Files:**
- Modify: `queue_status/service.py` (implement `update_doctor_delay`)
- Create: `tests/queue_status/test_service_delay_room.py`

**Interfaces:**
- Consumes: `DelayStore`, `AlertEvaluator.evaluate`; `InvalidDelay`, `SessionNotFound`; `DoctorDelay`
- Produces: `QueueStatusService.update_doctor_delay(session_id, delay_minutes, attender_id) -> int`

- [ ] **Step 1: Write failing tests**

`tests/queue_status/test_service_delay_room.py`:
```python
import pytest
from uuid import uuid4
from datetime import datetime

from queue_status.enums import BookingMode, TokenType, TokenStatus, AlertType
from queue_status.errors import InvalidDelay, SessionNotFound
from queue_status.providers import (
    InMemoryQueueDataProvider, SessionConfig, WaitingPatient,
)
from queue_status.writers import InMemoryQueueStateWriter
from queue_status.notifiers import InMemoryAlertNotifier
from queue_status.delay_store import InMemoryDelayStore
from queue_status.internal_store import InMemoryInternalStore
from queue_status.service import QueueStatusService


@pytest.fixture
def sid(): return uuid4()
@pytest.fixture
def pid(): return uuid4()
@pytest.fixture
def did(): return uuid4()
@pytest.fixture
def att_id(): return uuid4()


@pytest.fixture
def svc(sid, pid, did):
    provider = InMemoryQueueDataProvider()
    provider.set_session_config(SessionConfig(
        session_id=sid, doctor_id=did, doctor_name="Dr. A",
        started_at=datetime.utcnow(), avg_consultation_minutes=10,
        booking_mode=BookingMode.TOKEN,
    ))
    provider.set_waiting_patients(sid, [
        WaitingPatient(pid, uuid4(), 2, TokenType.NORMAL, TokenStatus.WAITING)
    ])
    return QueueStatusService(
        provider=provider,
        writer=InMemoryQueueStateWriter(),
        notifier=InMemoryAlertNotifier(),
        delay_store=InMemoryDelayStore(),
        internal_store=InMemoryInternalStore(),
    )


def test_update_delay_persists_value(svc, sid, att_id):
    result = svc.update_doctor_delay(sid, 20, att_id)
    assert result == 20
    stored = svc._delay_store.get(sid)
    assert stored.delay_minutes == 20


def test_update_delay_replaces_previous(svc, sid, att_id):
    svc.update_doctor_delay(sid, 10, att_id)
    svc.update_doctor_delay(sid, 25, att_id)
    stored = svc._delay_store.get(sid)
    assert stored.delay_minutes == 25


def test_update_delay_negative_raises(svc, sid, att_id):
    with pytest.raises(InvalidDelay):
        svc.update_doctor_delay(sid, -5, att_id)


def test_update_delay_zero_is_valid(svc, sid, att_id):
    result = svc.update_doctor_delay(sid, 0, att_id)
    assert result == 0


def test_update_delay_missing_session_raises(svc, att_id):
    with pytest.raises(SessionNotFound):
        svc.update_doctor_delay(uuid4(), 10, att_id)


def test_update_delay_triggers_alert_evaluation(svc, sid, pid, att_id):
    # patient at position 2, threshold=3 → position alert should fire
    cfg = svc._store.get_or_create_alert_config(pid, sid)
    cfg.position_threshold = 3
    svc._store.save_alert_config(cfg)
    svc.update_doctor_delay(sid, 5, att_id)
    alerts = [a for a in svc._notifier.patient_alerts if a[1] == AlertType.POSITION]
    assert len(alerts) == 1
```

- [ ] **Step 2: Run to verify failure**

```bash
pytest tests/queue_status/test_service_delay_room.py -v
```
Expected: `NotImplementedError` on `update_doctor_delay`

- [ ] **Step 3: Replace the `update_doctor_delay` stub in `queue_status/service.py`**

```python
    def update_doctor_delay(self, session_id: UUID, delay_minutes: int, attender_id: UUID) -> int:
        if delay_minutes < 0:
            raise InvalidDelay()
        config = self._provider.get_session_config(session_id)
        if config is None:
            raise SessionNotFound()
        delay = DoctorDelay(
            delay_id=uuid4(),
            session_id=session_id,
            doctor_id=config.doctor_id,
            delay_minutes=delay_minutes,
            entered_by=attender_id,
            entered_at=datetime.utcnow(),
        )
        self._delay_store.upsert(delay)
        self._evaluator.evaluate(session_id)
        return delay_minutes
```

- [ ] **Step 4: Run tests**

```bash
pytest tests/queue_status/test_service_delay_room.py -v
```
Expected: all 6 tests PASS

- [ ] **Step 5: Commit**

```bash
git add queue_status/service.py tests/queue_status/test_service_delay_room.py
git commit -m "feat(queue-status): implement update_doctor_delay with alert evaluation"
```

---

### Task 8: QueueStatusService — send_to_diagnostics and mark_diagnostic_complete

**Files:**
- Modify: `queue_status/service.py`
- Create: `tests/queue_status/test_service_diagnostic.py` (partial)

**Interfaces:**
- Produces:
  - `send_to_diagnostics(token_id, order_ids, attender_id) -> None`
  - `mark_diagnostic_complete(order_id, staff_id) -> None`

- [ ] **Step 1: Write failing tests**

`tests/queue_status/test_service_diagnostic.py`:
```python
import pytest
from uuid import uuid4
from datetime import datetime

from queue_status.enums import (
    BookingMode, TokenType, TokenStatus, AlertType, DiagnosticOrderStatus,
    NotificationStatus,
)
from queue_status.errors import (
    TokenNotHoldable, InvalidHoldRequest, AlreadyOnHold, OrderNotFound,
)
from queue_status.providers import (
    InMemoryQueueDataProvider, OPDPosition, SessionConfig, WaitingPatient,
)
from queue_status.writers import InMemoryQueueStateWriter
from queue_status.notifiers import InMemoryAlertNotifier
from queue_status.delay_store import InMemoryDelayStore
from queue_status.internal_store import InMemoryInternalStore
from queue_status.service import QueueStatusService


@pytest.fixture
def sid(): return uuid4()
@pytest.fixture
def pid(): return uuid4()
@pytest.fixture
def did(): return uuid4()
@pytest.fixture
def hid(): return uuid4()
@pytest.fixture
def tid(): return uuid4()
@pytest.fixture
def att_id(): return uuid4()
@pytest.fixture
def order1(): return uuid4()
@pytest.fixture
def order2(): return uuid4()


@pytest.fixture
def svc(sid, pid, did, hid, tid):
    provider = InMemoryQueueDataProvider()
    provider.set_session_config(SessionConfig(
        session_id=sid, doctor_id=did, doctor_name="Dr. A",
        started_at=datetime.utcnow(), avg_consultation_minutes=10,
        booking_mode=BookingMode.TOKEN,
    ))
    provider.set_opd_position(pid, OPDPosition(
        session_id=sid, doctor_id=did, token_id=tid,
        token_number=3, token_type=TokenType.NORMAL,
        queue_position=2, tokens_ahead=2,
        booking_mode=BookingMode.TOKEN,
        token_status=TokenStatus.WAITING,
    ))
    provider.set_waiting_patients(sid, [
        WaitingPatient(pid, tid, 2, TokenType.NORMAL, TokenStatus.WAITING)
    ])
    return QueueStatusService(
        provider=provider,
        writer=InMemoryQueueStateWriter(),
        notifier=InMemoryAlertNotifier(),
        delay_store=InMemoryDelayStore(),
        internal_store=InMemoryInternalStore(),
    )


# send_to_diagnostics tests
def test_send_to_diagnostics_sets_hold_status(svc, tid, att_id, order1, sid):
    svc.send_to_diagnostics(tid, [order1], att_id)
    assert svc._writer.get_token_status(tid) == TokenStatus.DIAGNOSTIC_HOLD


def test_send_to_diagnostics_decrements_queue_count(svc, tid, att_id, order1, sid):
    svc.send_to_diagnostics(tid, [order1], att_id)
    assert svc._writer.get_count(sid, TokenType.NORMAL) == -1  # delta=-1 applied


def test_send_to_diagnostics_creates_hold_record(svc, tid, att_id, order1):
    svc.send_to_diagnostics(tid, [order1], att_id)
    hold = svc._store.get_hold_by_token(tid)
    assert hold is not None
    assert order1 in hold.order_ids


def test_send_to_diagnostics_empty_orders_raises(svc, tid, att_id):
    with pytest.raises(InvalidHoldRequest):
        svc.send_to_diagnostics(tid, [], att_id)


def test_send_to_diagnostics_already_on_hold_raises(svc, tid, att_id, order1):
    svc.send_to_diagnostics(tid, [order1], att_id)
    # Simulate token already on hold
    from queue_status.models import DiagnosticHold
    with pytest.raises(AlreadyOnHold):
        svc.send_to_diagnostics(tid, [order1], att_id)


# mark_diagnostic_complete tests
def test_mark_complete_updates_order_status(svc, tid, att_id, order1, sid):
    svc.send_to_diagnostics(tid, [order1], att_id)
    svc.mark_diagnostic_complete(order1, att_id)
    assert svc._store.get_order_status(order1) == DiagnosticOrderStatus.COMPLETED


def test_mark_complete_all_orders_creates_notification(svc, tid, att_id, order1, sid):
    svc.send_to_diagnostics(tid, [order1], att_id)
    svc.mark_diagnostic_complete(order1, att_id)
    notif = svc._store.get_notification_by_token(tid)
    assert notif is not None
    assert notif.status == NotificationStatus.PENDING_REINSTATEMENT


def test_mark_complete_partial_does_not_notify(svc, tid, att_id, order1, order2, sid):
    svc.send_to_diagnostics(tid, [order1, order2], att_id)
    svc.mark_diagnostic_complete(order1, att_id)
    notif = svc._store.get_notification_by_token(tid)
    assert notif is None


def test_mark_complete_all_sends_attender_alert(svc, tid, att_id, order1, sid):
    svc.send_to_diagnostics(tid, [order1], att_id)
    svc.mark_diagnostic_complete(order1, att_id)
    attender_alerts = [a for a in svc._notifier.attender_alerts
                       if a[1] == AlertType.DIAGNOSTIC_COMPLETE]
    assert len(attender_alerts) == 1


def test_mark_complete_unknown_order_raises(svc, att_id):
    with pytest.raises(OrderNotFound):
        svc.mark_diagnostic_complete(uuid4(), att_id)
```

- [ ] **Step 2: Run to verify failure**

```bash
pytest tests/queue_status/test_service_diagnostic.py -v -k "send or mark"
```
Expected: `NotImplementedError`

- [ ] **Step 3: Replace stubs in `queue_status/service.py`**

```python
    def send_to_diagnostics(self, token_id: UUID, order_ids: list, attender_id: UUID) -> None:
        if not order_ids:
            raise InvalidHoldRequest()
        existing = self._store.get_hold_by_token(token_id)
        if existing is not None:
            raise AlreadyOnHold()

        # Resolve session and token type from provider
        # (in standalone: we track token status through writer; read initial type from provider)
        session_id, token_type = self._resolve_token_context(token_id)

        self._writer.set_token_status(token_id, TokenStatus.DIAGNOSTIC_HOLD)
        self._writer.adjust_queue_counts(session_id, token_type, delta=-1)
        self._writer.remove_from_buffer(session_id, token_id)
        self._writer.update_queue_version(session_id)

        hold = DiagnosticHold(
            hold_id=uuid4(),
            token_id=token_id,
            patient_id=self._resolve_patient_id(token_id),
            session_id=session_id,
            order_ids=list(order_ids),
            sent_at=datetime.utcnow(),
            attender_id=attender_id,
        )
        self._store.save_hold(hold)
        for oid in order_ids:
            self._store.set_order_status(oid, DiagnosticOrderStatus.WAITING)

    def mark_diagnostic_complete(self, order_id: UUID, staff_id: UUID) -> None:
        status = self._store.get_order_status(order_id)
        if status is None:
            raise OrderNotFound()
        self._store.set_order_status(order_id, DiagnosticOrderStatus.COMPLETED)

        hold = self._find_hold_for_order(order_id)
        if hold is None:
            return

        all_done = all(
            self._store.get_order_status(oid) == DiagnosticOrderStatus.COMPLETED
            for oid in hold.order_ids
        )
        if not all_done:
            return

        notif = DiagnosticCompletionNotification(
            notification_id=uuid4(),
            patient_id=hold.patient_id,
            token_id=hold.token_id,
            hold_id=hold.hold_id,
            completed_at=datetime.utcnow(),
            status=NotificationStatus.PENDING_REINSTATEMENT,
        )
        self._store.save_notification(notif)

        test_names = [str(oid) for oid in hold.order_ids]
        try:
            self._notifier.send_to_attender(
                hold.attender_id, AlertType.DIAGNOSTIC_COMPLETE,
                {"patient_id": str(hold.patient_id), "token_id": str(hold.token_id),
                 "test_names": test_names},
            )
        except Exception:
            pass  # attender alert is best-effort

    def _resolve_token_context(self, token_id: UUID):
        # In standalone mode: scan all sessions in provider for this token
        # Returns (session_id, token_type)
        for waiting_list in self._provider._waiting.values():
            for wp in waiting_list:
                if wp.token_id == token_id:
                    session_id = next(
                        (sid for sid, wps in self._provider._waiting.items()
                         if any(w.token_id == token_id for w in wps)),
                        None,
                    )
                    return session_id, wp.token_type
        # fallback: look in positions
        for pos in self._provider._positions.values():
            if pos.token_id == token_id:
                return pos.session_id, pos.token_type
        raise TokenNotHoldable("UNKNOWN")

    def _resolve_patient_id(self, token_id: UUID) -> UUID:
        for pid, pos in self._provider._positions.items():
            if pos.token_id == token_id:
                return pid
        return uuid4()

    def _find_hold_for_order(self, order_id: UUID):
        for hold in self._store._holds.values():
            if order_id in hold.order_ids:
                return hold
        return None
```

Also add these imports at top of service.py:
```python
from .enums import (... DiagnosticOrderStatus, NotificationStatus, ...)
from .models import (... DiagnosticHold, DiagnosticCompletionNotification, ...)
```

- [ ] **Step 4: Run tests**

```bash
pytest tests/queue_status/test_service_diagnostic.py -v
```
Expected: all 9 tests PASS

- [ ] **Step 5: Commit**

```bash
git add queue_status/service.py tests/queue_status/test_service_diagnostic.py
git commit -m "feat(queue-status): implement send_to_diagnostics and mark_diagnostic_complete"
```

---

### Task 9: QueueStatusService — reinstate_diagnostic_patient

**Files:**
- Modify: `queue_status/service.py`
- Modify: `tests/queue_status/test_service_diagnostic.py` (append)

**Interfaces:**
- Produces: `reinstate_diagnostic_patient(token_id, attender_id) -> None`

- [ ] **Step 1: Append failing tests to `tests/queue_status/test_service_diagnostic.py`**

```python
from queue_status.errors import TokenNotOnHold, DiagnosticsIncomplete, StaleQueueVersion
from queue_status.enums import NotificationStatus


def _setup_completed_hold(svc, tid, att_id, order1, sid, pid):
    """Helper: put token on hold, complete all orders."""
    svc.send_to_diagnostics(tid, [order1], att_id)
    svc.mark_diagnostic_complete(order1, att_id)


def test_reinstate_buffer_when_space_available(svc, tid, att_id, order1, sid, pid):
    _setup_completed_hold(svc, tid, att_id, order1, sid, pid)
    svc.reinstate_diagnostic_patient(tid, att_id)
    assert svc._writer.get_token_status(tid) == TokenStatus.BUFFER
    assert tid in svc._writer.get_buffer(sid)


def test_reinstate_restores_queue_count(svc, tid, att_id, order1, sid, pid):
    _setup_completed_hold(svc, tid, att_id, order1, sid, pid)
    svc.reinstate_diagnostic_patient(tid, att_id)
    assert svc._writer.get_count(sid, TokenType.NORMAL) == 0  # -1 + 1 = 0


def test_reinstate_sends_patient_reinstated_alert(svc, tid, att_id, order1, sid, pid):
    _setup_completed_hold(svc, tid, att_id, order1, sid, pid)
    svc.reinstate_diagnostic_patient(tid, att_id)
    alerts = [a for a in svc._notifier.patient_alerts if a[1] == AlertType.REINSTATED]
    assert len(alerts) == 1


def test_reinstate_marks_notification_reinstated(svc, tid, att_id, order1, sid, pid):
    _setup_completed_hold(svc, tid, att_id, order1, sid, pid)
    svc.reinstate_diagnostic_patient(tid, att_id)
    notif = svc._store.get_notification_by_token(tid)
    assert notif.status == NotificationStatus.REINSTATED


def test_reinstate_full_buffer_puts_back_in_waiting(svc, tid, att_id, order1, sid, pid):
    # Fill buffer to capacity (3)
    svc._writer.add_to_buffer(sid, uuid4())
    svc._writer.add_to_buffer(sid, uuid4())
    svc._writer.add_to_buffer(sid, uuid4())
    _setup_completed_hold(svc, tid, att_id, order1, sid, pid)
    svc.reinstate_diagnostic_patient(tid, att_id)
    assert svc._writer.get_token_status(tid) == TokenStatus.WAITING
    alerts = [a for a in svc._notifier.patient_alerts if a[1] == AlertType.BACK_IN_QUEUE]
    assert len(alerts) == 1


def test_reinstate_not_on_hold_raises(svc, att_id):
    with pytest.raises(TokenNotOnHold):
        svc.reinstate_diagnostic_patient(uuid4(), att_id)


def test_reinstate_incomplete_diagnostics_raises(svc, tid, att_id, order1, order2, sid, pid):
    svc.send_to_diagnostics(tid, [order1, order2], att_id)
    svc.mark_diagnostic_complete(order1, att_id)  # only one of two done
    with pytest.raises(DiagnosticsIncomplete):
        svc.reinstate_diagnostic_patient(tid, att_id)


def test_reinstate_stale_version_raises(svc, tid, att_id, order1, sid, pid):
    _setup_completed_hold(svc, tid, att_id, order1, sid, pid)
    svc._writer.version_fail = True
    with pytest.raises(StaleQueueVersion):
        svc.reinstate_diagnostic_patient(tid, att_id)
```

- [ ] **Step 2: Run to verify failure**

```bash
pytest tests/queue_status/test_service_diagnostic.py -v -k "reinstate"
```
Expected: `NotImplementedError`

- [ ] **Step 3: Replace `reinstate_diagnostic_patient` stub in `queue_status/service.py`**

```python
    def reinstate_diagnostic_patient(self, token_id: UUID, attender_id: UUID) -> None:
        hold = self._store.get_hold_by_token(token_id)
        if hold is None:
            raise TokenNotOnHold()

        notif = self._store.get_notification_by_token(token_id)
        if notif is None or notif.status != NotificationStatus.PENDING_REINSTATEMENT:
            raise DiagnosticsIncomplete()

        all_done = all(
            self._store.get_order_status(oid) == DiagnosticOrderStatus.COMPLETED
            for oid in hold.order_ids
        )
        if not all_done:
            raise DiagnosticsIncomplete()

        buffer_size = len(self._writer.get_buffer(hold.session_id))
        token_type = hold_token_type = self._get_hold_token_type(hold)

        if buffer_size < 3:
            self._writer.set_token_status(token_id, TokenStatus.BUFFER)
            self._writer.add_to_buffer(hold.session_id, token_id)
            self._writer.adjust_queue_counts(hold.session_id, hold_token_type, delta=+1)
            ok = self._writer.update_queue_version(hold.session_id)
            if not ok:
                raise StaleQueueVersion()
            try:
                self._notifier.send_to_patient(
                    hold.patient_id, AlertType.REINSTATED,
                    {"message": "Your diagnostics are complete. Please move to the waiting area."},
                )
            except Exception:
                pass
        else:
            self._writer.set_token_status(token_id, TokenStatus.WAITING)
            self._writer.adjust_queue_counts(hold.session_id, hold_token_type, delta=+1)
            ok = self._writer.update_queue_version(hold.session_id)
            if not ok:
                raise StaleQueueVersion()
            normal_count = self._writer.get_count(hold.session_id, hold_token_type)
            try:
                self._notifier.send_to_patient(
                    hold.patient_id, AlertType.BACK_IN_QUEUE,
                    {"queue_position": normal_count},
                )
            except Exception:
                pass

        notif.status = NotificationStatus.REINSTATED
        self._store.save_notification(notif)

    def _get_hold_token_type(self, hold):
        for pos in self._provider._positions.values():
            if pos.token_id == hold.token_id:
                return pos.token_type
        return TokenType.NORMAL
```

Also add missing imports at top of service.py:
```python
from .errors import (..., DiagnosticsIncomplete, StaleQueueVersion, TokenNotOnHold)
from .enums import (..., NotificationStatus)
```

- [ ] **Step 4: Run all tests**

```bash
pytest tests/queue_status/ -v
```
Expected: all tests PASS

- [ ] **Step 5: Commit**

```bash
git add queue_status/service.py tests/queue_status/test_service_diagnostic.py
git commit -m "feat(queue-status): implement reinstate_diagnostic_patient"
```

---

## Self-Review Checklist

**Spec coverage:**
- [x] get_patient_status — token mode (§4.1) → Task 6
- [x] get_patient_status — DIAGNOSTIC_HOLD view (§4.1) → Task 6
- [x] get_patient_status — diagnostic queue view (§4.1) → Task 6
- [x] update_doctor_delay + evaluate_alerts trigger (§4.2) → Task 7
- [x] update_room_status (§4.3) → Task 6
- [x] compute_wait TOKEN mode (§4.4) → Task 4
- [x] compute_wait SLOT mode (§4.4) → Task 4
- [x] compute_diagnostic_wait (§4.4) → Task 4
- [x] evaluate_alerts — position alert (§4.5) → Task 5
- [x] evaluate_alerts — time alert (§4.5) → Task 5
- [x] evaluate_alerts — fires once per session (§4.5) → Task 5
- [x] evaluate_alerts — notifier failure → FAILED record (§4.5) → Task 5
- [x] send_to_diagnostics (§4.6) → Task 8
- [x] mark_diagnostic_complete + attender alert (§4.7) → Task 8
- [x] reinstate to buffer when space (§4.8) → Task 9
- [x] reinstate to WAITING when buffer full (§4.8) → Task 9
- [x] reinstate — STALE_QUEUE_VERSION on conflict (§4.8) → Task 9
- [x] All error codes (§5) → Task 1 + each service task
- [x] Alert thresholds configurable (§6) → Task 5 AlertConfig
- [x] Four adapter interfaces (§2) → Task 2
- [x] InternalStore (holds, notifications, room status, alert records) → Task 3
