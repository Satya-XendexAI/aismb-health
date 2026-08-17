# Live OPD / Diagnostic Queue Status — Design Spec

**Date:** 2026-08-17
**Scope:** Backend LLD only — no UI/channel integration. Developed independently; external integrations (AppointmentEngine, HIS) wired in later via adapters.
**Status:** Approved

---

## 1. Overview

The Live Queue Status feature gives patients real-time visibility into their OPD token position and diagnostic test progress. It supports two alert types (position-based and time-based), attender-driven doctor delay updates, room/counter status tracking, and a diagnostic hold/re-admission flow that bridges diagnostic queues back to the OPD queue.

**Consumers:** Patients only (personal queue view).

**Key design principle:** All external dependencies (appointment engine, HIS, push notification provider) are behind interface adapters. The `QueueStatusService` is fully testable with in-memory stubs. Real adapters are wired in during integration.

---

## 2. Architecture

```
┌──────────────────────────────────────────────────────────────┐
│                   QueueStatusService                          │
│                                                              │
│  + get_patient_status(patient_id, hospital_id)               │
│  + update_doctor_delay(session_id, delay_minutes, actor_id)  │
│  + update_room_status(doctor_id, hospital_id, status, actor) │
│  + evaluate_alerts(session_id)                               │
│  + send_to_diagnostics(token_id, order_ids, attender_id)     │
│  + mark_diagnostic_complete(order_id, staff_id)              │
│  + reinstate_diagnostic_patient(token_id, attender_id)       │
└──────────────────────────┬───────────────────────────────────┘
                           │
         ┌─────────────────┼─────────────────┐
         │                 │                 │
 ┌───────▼────────┐ ┌──────▼──────┐  ┌──────▼──────┐
 │QueueDataProvider│ │AlertNotifier│  │ DelayStore  │
 │  (interface)   │ │ (interface) │  │ (interface) │
 │                │ │             │  │             │
 │Stub: in-memory │ │Stub: records│  │Stub: dict   │
 │Real: Appt      │ │ in list     │  │Real: DB     │
 │Engine + HIS    │ │Real: FCM/   │  │             │
 │adapters        │ │SMS/WS       │  │             │
 └────────────────┘ └─────────────┘  └─────────────┘
```

**Four external boundaries, all behind interfaces:**

| Interface | Purpose | Stub | Future Real |
|---|---|---|---|
| `QueueDataProvider` | Read token positions, slot times, diagnostic orders, session config | In-memory fixture | AppointmentEngine + HIS adapter |
| `QueueStateWriter` | Mutate token status and queue counts (hold, reinstate) | In-memory state | AppointmentEngine write adapter |
| `AlertNotifier` | Fire alerts to patient and attender | In-memory list | FCM / APNs / SMS |
| `DelayStore` | Persist attender-entered delays per session | In-memory dict | DB table |

**`evaluate_alerts(session_id)`** is the single trigger point — called after every state change (advance buffer, skip, delay update). Idempotent — fires each alert type exactly once per patient per session.

---

## 3. Data Models

### 3.1 Patient Status View (read model — returned to patient)

```
PatientStatusView
  patient_id:           UUID
  hospital_id:          UUID
  opd_queue:            OPDQueueView | None
  diagnostic_queues:    List[DiagnosticQueueView]
  retrieved_at:         datetime


OPDQueueView
  session_id:           UUID
  doctor_id:            UUID
  doctor_name:          str
  room_status:          Enum[OPEN, IN_CONSULTATION, BREAK, CLOSED]
  booking_mode:         Enum[TOKEN, SLOT]

  # Token mode fields (None when booking_mode = SLOT)
  token_number:         int | None
  token_type:           Enum[NORMAL, TATKAL] | None
  queue_position:       int | None          -- patients ahead in queue
  tokens_ahead:         int | None

  # Slot mode fields (None when booking_mode = TOKEN)
  slot_time:            time | None
  slot_status:          Enum[SCHEDULED, UPCOMING, MISSED] | None

  # Always present
  estimated_wait_mins:  int                 -- 0 if patient is next or overdue
  doctor_delay_mins:    int                 -- 0 if no delay entered today
  hold_status:          str | None          -- "DIAGNOSTIC_HOLD" or None
  alert_config:         AlertConfig

  # Shown only when token is in DIAGNOSTIC_HOLD
  message:              str | None


DiagnosticQueueView
  order_id:             UUID
  test_name:            str                 -- e.g. "CBC", "X-Ray Chest"
  counter:              str | None          -- e.g. "Lab Counter 2"
  queue_position:       int | None
  estimated_wait_mins:  int
  status:               Enum[WAITING, IN_PROGRESS, COMPLETED]


AlertConfig                                 -- per patient per session
  position_threshold:   int                 -- default: 3
  time_threshold_mins:  int                 -- default: 10
  position_alert_fired: bool
  time_alert_fired:     bool
```

### 3.2 Internal State Models

```
DoctorDelay
  delay_id:             UUID        PK
  session_id:           UUID        FK → DoctorSession
  doctor_id:            UUID        FK → Doctor
  delay_minutes:        int         >= 0; replaces previous value on update
  entered_by:           UUID        attender_id
  entered_at:           datetime

RoomStatus
  doctor_id:            UUID        PK (one row per doctor)
  hospital_id:          UUID
  status:               Enum[OPEN, IN_CONSULTATION, BREAK, CLOSED]
  updated_at:           datetime
  updated_by:           UUID        attender_id

AlertRecord                         -- append-only audit of all fired alerts
  alert_id:             UUID        PK
  patient_id:           UUID
  session_id:           UUID
  alert_type:           Enum[POSITION, TIME, REINSTATED, BACK_IN_QUEUE, DIAGNOSTIC_COMPLETE]
  triggered_at:         datetime
  status:               Enum[SENT, FAILED]
  payload:              dict

DiagnosticHold
  hold_id:              UUID        PK
  token_id:             UUID        FK → Token (OPD token on hold)
  patient_id:           UUID
  session_id:           UUID
  order_ids:            List[UUID]  -- all diagnostic orders linked to this hold
  sent_at:              datetime
  attender_id:          UUID

DiagnosticCompletionNotification
  notification_id:      UUID        PK
  patient_id:           UUID
  token_id:             UUID
  hold_id:              UUID        FK → DiagnosticHold
  completed_at:         datetime
  status:               Enum[PENDING_REINSTATEMENT, REINSTATED]
```

### 3.3 Token Status Extension

The appointment booking system's `TokenStatus` enum is extended with two new values:

```
WAITING | BUFFER | SERVING | COMPLETED | CANCELLED | SKIPPED | DIAGNOSTIC_HOLD
```

`DIAGNOSTIC_HOLD` — patient's OPD token is paused while they complete diagnostic tests. Token is removed from active queue counts and not served until attender reinstates it.

### 3.4 QueueDataProvider Interface Contract

```
QueueDataProvider
  + get_opd_position(patient_id, hospital_id)
      → OPDPosition(
            session_id, doctor_id, token_id, token_number,
            token_type, queue_position, tokens_ahead,
            booking_mode, slot_time, slot_status, token_status
        )
      → None if patient has no active OPD token

  + get_diagnostic_orders(patient_id, hospital_id)
      → List[DiagOrder(order_id, test_name, counter, queue_position, status)]

  + get_session_config(session_id)
      → SessionConfig(
            doctor_id, doctor_name, started_at,
            avg_consultation_minutes, booking_mode, slot_duration_minutes
        )

  + get_waiting_patients(session_id)
      → List[WaitingPatient(patient_id, token_id, queue_position, token_type)]

  + get_diagnostic_test_config(test_name) → DiagnosticTestConfig
      → DiagnosticTestConfig(test_name, avg_turnaround_minutes)
         default avg_turnaround_minutes = 15 if not configured
```

### 3.5 QueueStateWriter Interface Contract

```
QueueStateWriter
  + set_token_status(token_id, new_status: TokenStatus) → None
  + adjust_queue_counts(session_id, token_type, delta: int) → None
      delta = +1 (restore) or -1 (remove); clamped to >= 0
  + remove_from_buffer(session_id, token_id) → None
  + add_to_buffer(session_id, token_id) → None
  + update_queue_version(session_id) → bool
      returns False on version conflict → caller raises STALE_QUEUE_VERSION
```

### 3.6 AlertNotifier Interface Contract

```
AlertNotifier
  + send_to_patient(patient_id, alert_type, payload: dict) → None
  + send_to_attender(attender_id, alert_type, payload: dict) → None

  alert_type values:
    POSITION           -- patient nearing turn (position threshold crossed)
    TIME               -- patient nearing turn (time threshold crossed)
    REINSTATED         -- patient called back to waiting area after diagnostics
    BACK_IN_QUEUE      -- patient re-queued (buffer was full on reinstatement)
    DIAGNOSTIC_COMPLETE -- attender notified that patient's tests are all done
```

---

## 4. Core Flows

### 4.1 Get Patient Status

```
get_patient_status(patient_id, hospital_id) → PatientStatusView

1. QueueDataProvider.get_opd_position(patient_id, hospital_id)
     If found and token_status != DIAGNOSTIC_HOLD:
       a. Fetch DoctorDelay for session → doctor_delay_mins (0 if none)
       b. Fetch RoomStatus for doctor_id → room_status
       c. Compute estimated_wait_mins (§4.4)
       d. Fetch or create AlertConfig(position_threshold=3, time_threshold=10)
       e. Build OPDQueueView (hold_status = None, message = None)
     If found and token_status = DIAGNOSTIC_HOLD:
       a. Build OPDQueueView:
            queue_position = None, estimated_wait_mins = None
            hold_status = "DIAGNOSTIC_HOLD"
            message = "Please complete your tests. You will be called back when ready."
     If not found:
       opd_queue = None

2. QueueDataProvider.get_diagnostic_orders(patient_id, hospital_id)
     For each WAITING or IN_PROGRESS order:
       estimated_wait_mins = queue_position × avg_lab_turnaround_minutes
     Build List[DiagnosticQueueView]

3. Return PatientStatusView(opd_queue, diagnostic_queues, retrieved_at=now)
```

### 4.2 Update Doctor Delay

```
update_doctor_delay(session_id, delay_minutes, attender_id)

1. Validate delay_minutes >= 0                       → INVALID_DELAY if not
2. Fetch session via QueueDataProvider               → SESSION_NOT_FOUND if missing
3. Upsert DoctorDelay:
     delay_minutes = new value (replaces previous)
     entered_by = attender_id, entered_at = now
4. evaluate_alerts(session_id)
   ← delay change may push patients below time threshold
5. Return updated delay_minutes
```

### 4.3 Update Room Status

```
update_room_status(doctor_id, hospital_id, new_status, attender_id)

1. Validate new_status ∈ {OPEN, IN_CONSULTATION, BREAK, CLOSED}   → INVALID_ROOM_STATUS
2. Validate doctor belongs to hospital                             → DOCTOR_NOT_FOUND
3. Upsert RoomStatus: status = new_status, updated_by, updated_at = now
4. Return updated RoomStatus
   (no alert evaluation — room status is display-only)
```

### 4.4 Estimated Wait Time Calculation

```
compute_wait(patient, session_config, doctor_delay_mins) → int (minutes)

TOKEN mode:
  base_wait = tokens_ahead × session_config.avg_consultation_minutes
  estimated_wait = max(0, base_wait + doctor_delay_mins)

SLOT mode:
  now = current time
  base_wait = max(0, (slot_time - now).total_seconds() / 60)
  estimated_wait = max(0, base_wait + doctor_delay_mins)
  If slot_time is in the past → estimated_wait = 0 (patient is overdue)

DIAGNOSTIC queue:
  config = QueueDataProvider.get_diagnostic_test_config(test_name)
  estimated_wait = queue_position × config.avg_turnaround_minutes
  (no delay adjustment for diagnostics)

Rule: never return negative — floor at 0.
```

### 4.5 Evaluate Alerts

```
evaluate_alerts(session_id)

1. QueueDataProvider.get_waiting_patients(session_id)
   (returns only WAITING and BUFFER tokens — excludes DIAGNOSTIC_HOLD)

2. Fetch DoctorDelay for session → doctor_delay_mins
3. Fetch SessionConfig

4. For each waiting patient:
     a. compute estimated_wait_mins
     b. Fetch AlertConfig for (patient_id, session_id)

     c. POSITION alert:
          if queue_position <= position_threshold
          AND position_alert_fired = False:
            AlertNotifier.send_to_patient(POSITION, {position: queue_position})
            Save AlertRecord (status = SENT or FAILED)
            Mark position_alert_fired = True

     d. TIME alert:
          if estimated_wait_mins <= time_threshold_mins
          AND time_alert_fired = False:
            AlertNotifier.send_to_patient(TIME, {wait_mins: estimated_wait_mins})
            Save AlertRecord
            Mark time_alert_fired = True

Note: Each alert fires exactly once per patient per session.
      AlertNotifier failure does not block flow — logged as FAILED in AlertRecord.
      evaluate_alerts is idempotent — safe to call multiple times.
```

### 4.6 Send Patient to Diagnostics

```
send_to_diagnostics(token_id, order_ids: List[UUID], attender_id)

1. Fetch Token → validate status ∈ {WAITING, BUFFER}  → TOKEN_NOT_HOLDABLE
2. Validate len(order_ids) > 0                         → INVALID_HOLD_REQUEST
3. Validate no existing DiagnosticHold for token       → ALREADY_ON_HOLD
4. QueueStateWriter.set_token_status(token_id, DIAGNOSTIC_HOLD)
5. QueueStateWriter.adjust_queue_counts(session_id, token_type, delta=-1)
6. QueueStateWriter.remove_from_buffer(session_id, token_id)
   ← if token was in buffer; no-op if it wasn't
7. QueueStateWriter.update_queue_version(session_id)
8. Create DiagnosticHold(token_id, order_ids, sent_at=now, attender_id)
9. AuditLogger → log SENT_TO_DIAGNOSTIC
8. Return confirmation
```

### 4.7 Mark Diagnostic Complete

```
mark_diagnostic_complete(order_id, staff_id)

1. Fetch DiagnosticOrder → validate exists            → ORDER_NOT_FOUND
2. Mark DiagnosticOrder.status = COMPLETED, completed_at = now
3. Fetch DiagnosticHold that contains this order_id
4. Check if ALL order_ids in hold are COMPLETED:
     If yes:
       Create DiagnosticCompletionNotification(
         patient_id, token_id, completed_at=now,
         status=PENDING_REINSTATEMENT
       )
       AlertNotifier.send_to_attender(attender_id, DIAGNOSTIC_COMPLETE, {
         patient_id, token_id, test_names: [...]
       })
     If no: no notification yet — waiting for remaining orders
5. AuditLogger → log DIAGNOSTIC_COMPLETED
```

### 4.8 Reinstate Diagnostic Patient to OPD Buffer

```
reinstate_diagnostic_patient(token_id, attender_id)

1. Fetch Token → validate status = DIAGNOSTIC_HOLD    → TOKEN_NOT_ON_HOLD
2. Fetch DiagnosticCompletionNotification
     validate status = PENDING_REINSTATEMENT
     validate all linked orders are COMPLETED          → DIAGNOSTICS_INCOMPLETE
3. Fetch current buffer size via QueueDataProvider.get_waiting_patients(session_id)
4. If buffer size < 3:
     QueueStateWriter.set_token_status(token_id, BUFFER)
     QueueStateWriter.add_to_buffer(session_id, token_id)
     QueueStateWriter.adjust_queue_counts(session_id, token_type, delta=+1)
     ok = QueueStateWriter.update_queue_version(session_id)
     if not ok: retry once → STALE_QUEUE_VERSION
     AlertNotifier.send_to_patient(REINSTATED, {
       message: "Your diagnostics are complete. Please move to the waiting area."
     })
   Else (buffer full):
     QueueStateWriter.set_token_status(token_id, WAITING)
     QueueStateWriter.adjust_queue_counts(session_id, token_type, delta=+1)
     ok = QueueStateWriter.update_queue_version(session_id)
     if not ok: retry once → STALE_QUEUE_VERSION
     -- estimated_position = current normal_count (appended to back of normal queue)
     AlertNotifier.send_to_patient(BACK_IN_QUEUE, {
       queue_position: current_normal_count
     })
5. Mark DiagnosticCompletionNotification.status = REINSTATED
6. AuditLogger → log REINSTATED_FROM_DIAGNOSTIC, actor = attender_id
```

---

## 5. Error Cases

### Queue Status Lookup

| Scenario | Error Code | Behaviour |
|---|---|---|
| Patient not found | — | Return empty `PatientStatusView` — not an error |
| No active session for doctor | — | `opd_queue = None` |
| Token in COMPLETED / CANCELLED | — | `opd_queue = None` |

### Doctor Delay Update

| Scenario | Error Code | Behaviour |
|---|---|---|
| `delay_minutes` < 0 | `INVALID_DELAY` | Reject |
| Session not found | `SESSION_NOT_FOUND` | Reject |
| Attender not from same hospital | `UNAUTHORISED` | Reject |

### Room Status Update

| Scenario | Error Code | Behaviour |
|---|---|---|
| Invalid status value | `INVALID_ROOM_STATUS` | Reject |
| Doctor not found in hospital | `DOCTOR_NOT_FOUND` | Reject |

### Diagnostic Hold

| Scenario | Error Code | Behaviour |
|---|---|---|
| Token not WAITING or BUFFER | `TOKEN_NOT_HOLDABLE` | Reject |
| order_ids list empty | `INVALID_HOLD_REQUEST` | Reject |
| Token already in DIAGNOSTIC_HOLD | `ALREADY_ON_HOLD` | Reject |
| Order not found | `ORDER_NOT_FOUND` | Reject |
| Token not in DIAGNOSTIC_HOLD on reinstate | `TOKEN_NOT_ON_HOLD` | Reject |
| Not all orders completed on reinstate | `DIAGNOSTICS_INCOMPLETE` | Reject |
| TokenQueue version conflict on reinstate | `STALE_QUEUE_VERSION` | Retry once, then error |

### Alert Evaluation

| Scenario | Behaviour |
|---|---|
| Both alerts already fired | No-op — idempotent |
| `AlertNotifier.send()` fails | Log FAILED in AlertRecord; do not block main flow |
| Patient token cancelled before alert fires | Skip patient in evaluation loop |

---

## 6. Alert Configuration

| Alert | Trigger | Default Threshold | Configurable |
|---|---|---|---|
| POSITION | `queue_position <= N` | N = 3 | Per patient per session |
| TIME | `estimated_wait_mins <= T` | T = 10 minutes | Per patient per session |
| REINSTATED | Patient returned from diagnostics to buffer | N/A | Not configurable |
| BACK_IN_QUEUE | Patient returned from diagnostics to wait queue | N/A | Not configurable |
| DIAGNOSTIC_COMPLETE | All orders complete — sent to attender only | N/A | Not configurable |

Each alert fires **exactly once per patient per session** (tracked via `position_alert_fired` and `time_alert_fired` flags on `AlertConfig`).

---

## 7. Design Patterns

| Pattern | Application |
|---|---|
| Adapter | `QueueDataProvider`, `AlertNotifier`, `DelayStore` — all swappable without touching `QueueStatusService` |
| Idempotency | `evaluate_alerts` is safe to call multiple times — fired flags prevent double alerts |
| Append-only audit | `AlertRecord` is never updated — SENT/FAILED is written once at fire time |
| Optimistic lock | `TokenQueue.version` guards reinstatement writes (inherited from appointment system) |
| Null object | Empty `PatientStatusView` returned when patient has no queue — never raises errors on lookup |

---

## 8. Integration Points (Future)

| Current (Stub) | Future Real Adapter |
|---|---|
| `InMemoryQueueDataProvider` | `AppointmentEngineAdapter` — reads `Token`, `TokenQueue`, `Slot`, `Appointment` from appointment service |
| `InMemoryQueueDataProvider` (diagnostics) | `HISAdapter` — reads lab orders, diagnostic queue positions from HIS |
| `InMemoryQueueStateWriter` | `AppointmentEngineWriteAdapter` — calls `TokenRepository`, `TokenQueueRepository` in appointment service |
| `InMemoryAlertNotifier` | `FCMNotifier` / `SMSNotifier` / `WebSocketNotifier` |
| `InMemoryDelayStore` | `PostgresDelayStore` — persists `DoctorDelay` to DB |
| `DIAGNOSTIC_HOLD` token status | Requires appointment system to accept this new status value in `TokenStatus` enum |
| `DiagnosticTestConfig` defaults (15 min) | Per-test-type config table in HIS or hospital admin settings |
