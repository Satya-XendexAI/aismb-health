# Appointment Booking System — Design Spec

**Date:** 2026-08-17
**Scope:** Backend LLD only — no UI/channel integration
**Status:** Approved

---

## 1. Overview

The appointment booking system supports two operating modes, configured per hospital at setup time. A hospital runs entirely in one mode — there is no per-doctor or runtime switching.

| Mode | Description |
|---|---|
| `TOKEN` | Walk-in queue. Patients receive a token and are called in order based on a configurable normal:tatkal ratio. |
| `SLOT` | Pre-configured fixed slots per doctor per day. Patients select and book an available slot. |

---

## 2. Architecture

```
┌──────────────────────────────────────────────────────────┐
│                    AppointmentEngine                      │
│                                                          │
│  + book(request)                                         │
│  + cancel(appointment_id)                                │
│  + modify(appointment_id, new_target)                    │
│  + advance_buffer(doctor_id)   ← doctor calls next       │
│                                                          │
│  [ loads BookingStrategy from HospitalConfig at startup ]│
└────────────────────┬─────────────────────────────────────┘
                     │ uses
          ┌──────────┴──────────┐
          │                     │
   ┌──────▼──────┐      ┌───────▼──────┐
   │TokenStrategy│      │ SlotStrategy  │
   │             │      │               │
   │ + book()    │      │ + book()      │
   │ + cancel()  │      │ + cancel()    │
   │ + next_3()  │      │ + free_slot() │
   └─────────────┘      └───────────────┘

Shared (owned by AppointmentEngine):
  - PatientValidator     — confirm patient exists in hospital
  - DoctorValidator      — confirm doctor is active / has open session
  - AuditLogger          — every state change logged
  - BufferNotifier       — signals next 3 tokens to move to waiting area (TOKEN mode only; no-op in SLOT mode)
```

**HospitalConfig** — one row per hospital with `booking_mode` and `tatkal_ratio`. Engine reads this once at startup and injects the correct strategy. `tatkal_ratio` is ignored when `booking_mode = SLOT`.

**Modify behaviour by mode:**
- `TOKEN` mode: `modify()` is **not supported** — returns `OPERATION_NOT_SUPPORTED`. Patients must cancel and walk in for a new token.
- `SLOT` mode: `modify()` is an atomic cancel + rebook (Section 4.7).

---

## 3. Data Models

### 3.1 Shared Entities

```
Hospital
  hospital_id       UUID        PK
  name              str
  booking_mode      Enum        TOKEN | SLOT
  tatkal_ratio      int         default 2 (serve N normal → 1 tatkal)

Patient
  patient_id        UUID        PK
  hospital_id       UUID        FK → Hospital
  name              str
  phone             str
  dob               Date

Doctor
  doctor_id         UUID        PK
  hospital_id       UUID        FK → Hospital
  name              str
  specialization    str
  is_active         bool

DoctorSession                        -- tracks a doctor's active consultation day
  session_id        UUID        PK
  doctor_id         UUID        FK → Doctor
  hospital_id       UUID        FK → Hospital
  date              Date
  started_at        datetime
  ended_at          datetime?
  status            Enum        OPEN | CLOSED

AuditLog
  log_id            UUID        PK
  entity_type       str         "TOKEN" | "APPOINTMENT"
  entity_id         UUID
  action            str         BOOKED | CANCELLED | MODIFIED | CALLED | SESSION_CLOSED
  actor_id          UUID        patient_id for patient actions; doctor_id for session/advance actions; system UUID for automated cancellations (SESSION_CLOSED)
  timestamp         datetime
  metadata          JSON
```

### 3.2 Token-Based Entities

```
Token
  token_id          UUID        PK
  hospital_id       UUID        FK → Hospital
  doctor_id         UUID        FK → Doctor
  patient_id        UUID        FK → Patient
  session_id        UUID        FK → DoctorSession
  token_number      int         sequential per session (DB sequence)
  token_type        Enum        NORMAL | TATKAL
  status            Enum        WAITING | BUFFER | SERVING | COMPLETED | CANCELLED
  issued_at         datetime
  called_at         datetime?
  served_at         datetime?

TokenQueue                           -- queue state per session
  queue_id          UUID        PK
  session_id        UUID        FK → DoctorSession (unique)
  serving_token_id  UUID?       FK → Token (currently inside)
  buffer_token_ids  UUID[]      next 3 pre-notified tokens
  tatkal_count      int         pending TATKAL tokens
  normal_count      int         pending NORMAL tokens
  consecutive_normal_served int tracks position in current ratio cycle
  version           int         optimistic lock counter
  last_updated      datetime
```

**Token number:** Both NORMAL and TATKAL share one sequential counter per session (T-001, T-002...). Token type is stored separately. Serving order is determined by type + ratio rule, not by token number.

### 3.3 Slot-Based Entities

```
Slot
  slot_id           UUID        PK
  hospital_id       UUID        FK → Hospital
  doctor_id         UUID        FK → Doctor
  date              Date
  start_time        Time        e.g. 09:00
  end_time          Time        e.g. 09:20
  status            Enum        AVAILABLE | BOOKED | BLOCKED
  created_by        UUID        admin who configured it

Appointment
  appointment_id    UUID        PK
  hospital_id       UUID        FK → Hospital
  patient_id        UUID        FK → Patient
  doctor_id         UUID        FK → Doctor
  slot_id           UUID        FK → Slot
  status            Enum        SCHEDULED | COMPLETED | CANCELLED
  booked_at         datetime
  cancelled_at      datetime?
  cancellation_reason str?

Unique constraint: (patient_id, doctor_id, date) WHERE status = SCHEDULED
  -- prevents a patient booking the same doctor twice on the same day
```

---

## 4. Core Flows

### 4.1 Token System — Book Token

```
Input:  hospital_id, doctor_id, patient_id, token_type

1. PatientValidator  → confirm patient exists in hospital
2. DoctorValidator   → confirm DoctorSession is OPEN today
3. TokenStrategy
     a. Fetch active TokenQueue for session
     b. Assign token_number via nextval('token_seq_{session_id}')
     c. Insert Token (status = WAITING)
     d. Increment tatkal_count or normal_count on TokenQueue
4. AuditLogger       → log BOOKED
5. Return token_number, estimated_wait_position

estimated_wait_position:
  TATKAL: tatkal_count (number of tatkal patients already waiting ahead)
  NORMAL: normal_count + ceil(tatkal_count / tatkal_ratio)
          -- accounts for tatkal insertions that will interrupt the normal flow
```

### 4.2 Token System — Cancel Token

```
Input:  token_id, patient_id

1. Fetch Token → validate patient_id matches
2. Validate status = WAITING (BUFFER/SERVING/COMPLETED cannot be cancelled)
3. Mark Token.status = CANCELLED
4. Decrement tatkal_count or normal_count on TokenQueue
5. AuditLogger → log CANCELLED
6. Return confirmation

Note: No queue renumbering. The gap is absorbed at the next advance_buffer call.
      Patient must walk in for a new token.
```

### 4.3 Token System — Advance Buffer (Doctor calls next)

```
Input:  session_id, doctor_id

1. Mark current serving_token → COMPLETED, set served_at = now
2. Apply serving order rule:
     next_token = pick_next(TokenQueue, tatkal_ratio)
3. Mark next_token → SERVING, set called_at = now
4. Resolve new buffer (next 3 after serving):
     Peek queue using same ratio rule, skip CANCELLED tokens
     Mark peeked tokens → BUFFER
5. BufferNotifier → signal the 3 buffer tokens to move to waiting area
6. Update TokenQueue (serving_token_id, buffer_token_ids,
                      consecutive_normal_served, version++)
7. AuditLogger → log CALLED for serving token

pick_next() rule:
  if consecutive_normal_served < tatkal_ratio AND normal queue not empty:
      serve next NORMAL, consecutive_normal_served += 1
  elif tatkal queue not empty:
      serve next TATKAL, consecutive_normal_served = 0
  else:
      serve whichever queue has WAITING patients (fallback)
      if both empty: return QUEUE_EMPTY signal
```

### 4.4 Slot System — View Available Slots

```
Input:  hospital_id, doctor_id, date

1. DoctorValidator → confirm doctor belongs to hospital
2. Query Slots WHERE doctor_id = ? AND date = ? AND status = AVAILABLE
3. Return list ordered by start_time
```

### 4.5 Slot System — Book Slot

```
Input:  hospital_id, doctor_id, patient_id, slot_id

1. PatientValidator → confirm patient exists
2. DoctorValidator  → confirm doctor is active
3. SlotStrategy
     a. Check duplicate: no SCHEDULED appointment for (patient_id, doctor_id, date)
     b. SELECT slot FOR UPDATE    ← row-level lock
     c. Validate slot.status = AVAILABLE
     d. UPDATE slot.status = BOOKED
     e. INSERT Appointment (status = SCHEDULED)
4. AuditLogger → log BOOKED
5. Return appointment_id, slot start_time
```

### 4.6 Slot System — Cancel

```
Input:  appointment_id, patient_id, cancellation_reason

1. Fetch Appointment → validate patient_id matches
2. Validate status = SCHEDULED
3. Mark Appointment.status = CANCELLED, set cancelled_at, reason
4. Mark Slot.status = AVAILABLE        ← freed back to pool
5. AuditLogger → log CANCELLED
6. Return confirmation (patient may rebook any available slot)
```

### 4.7 Slot System — Modify (atomic cancel + rebook)

```
Input:  appointment_id, patient_id, new_slot_id

1. Fetch current Appointment → validate patient_id matches
2. Validate current Appointment.status = SCHEDULED
3. BEGIN TRANSACTION
     a. SELECT new_slot FOR UPDATE
     b. Validate new_slot.status = AVAILABLE
     c. UPDATE old Slot.status = AVAILABLE
     d. UPDATE current Appointment.status = CANCELLED
     e. UPDATE new_slot.status = BOOKED
     f. INSERT new Appointment (status = SCHEDULED)
4. COMMIT
5. AuditLogger → log MODIFIED (old_appointment_id → new_appointment_id)
6. Return new appointment_id, new slot start_time

On failure: ROLLBACK — old appointment remains SCHEDULED, old slot stays BOOKED
```

---

## 5. Error Cases

### Token System

| Scenario | Error Code | Behaviour |
|---|---|---|
| No active doctor session | `NO_ACTIVE_SESSION` | Reject booking |
| Token status is BUFFER/SERVING/COMPLETED | `TOKEN_NOT_CANCELLABLE` | Reject cancel — too late |
| Token already CANCELLED | `TOKEN_NOT_CANCELLABLE` | Reject cancel |
| Doctor session closes mid-queue | `SESSION_CLOSED` | All WAITING + BUFFER tokens → CANCELLED |
| Both queues empty on advance_buffer | `QUEUE_EMPTY` | Signal to doctor — no more patients |

### Slot System

| Scenario | Error Code | Behaviour |
|---|---|---|
| Slot already BOOKED (race condition) | `SLOT_UNAVAILABLE` | Row lock prevents double book — client re-fetches |
| Slot is BLOCKED by admin | `SLOT_BLOCKED` | Reject booking |
| New slot taken during modify | `SLOT_UNAVAILABLE` | Rollback — old appointment preserved |
| Appointment already CANCELLED | `APPOINTMENT_NOT_MODIFIABLE` | Reject modify |
| Duplicate booking (same doctor+date) | `DUPLICATE_BOOKING` | Reject — unique constraint on (patient_id, doctor_id, date) |

---

## 6. Concurrency Rules

### Token Counter — DB Sequence
```sql
-- Created when DoctorSession is OPENED
CREATE SEQUENCE token_seq_{session_id} START 1 INCREMENT 1;

-- Used on every token insert
INSERT INTO tokens (..., token_number)
VALUES (..., nextval('token_seq_{session_id}'));

-- Dropped when DoctorSession is CLOSED
DROP SEQUENCE token_seq_{session_id};
```
Guarantees no two tokens in the same session share a number.

### Slot Booking — Row-Level Lock
```sql
BEGIN;
  SELECT * FROM slots WHERE slot_id = ? FOR UPDATE;
  -- concurrent bookings queue behind this lock
  UPDATE slots SET status = 'BOOKED' WHERE slot_id = ?;
  INSERT INTO appointments (...);
COMMIT;
-- Lock held < 10ms (no external calls inside transaction)
```

### TokenQueue State — Optimistic Lock
```sql
UPDATE token_queues
SET serving_token_id = ?,
    buffer_token_ids = ?,
    consecutive_normal_served = ?,
    version = version + 1
WHERE queue_id = ? AND version = ?;
-- version mismatch → retry once → error
-- prevents two simultaneous advance_buffer calls corrupting queue state
```

---

## 7. Session Lifecycle (Token System)

```
Doctor opens day:
  → DoctorSession created (status = OPEN)
  → TokenQueue initialised (empty, consecutive_normal_served = 0)
  → DB sequence created for session

Doctor closes day:
  → DoctorSession.status = CLOSED, ended_at = now
  → All WAITING tokens → CANCELLED (reason: SESSION_CLOSED)
  → All BUFFER tokens → CANCELLED (reason: SESSION_CLOSED)
  → SERVING token completes naturally (not force-cancelled)
  → DB sequence dropped
```

---

## 8. Design Patterns

| Pattern | Application |
|---|---|
| Strategy | `BookingStrategy` interface — `TokenStrategy` and `SlotStrategy` swap in based on `HospitalConfig.booking_mode` |
| Optimistic Lock | `TokenQueue.version` guards against concurrent advance_buffer writes |
| Pessimistic Lock | `SELECT FOR UPDATE` on Slot prevents race condition in slot booking |
| Atomic Transaction | Modify flow wraps cancel + rebook in a single DB transaction |
| Audit Log | Every state transition recorded in `AuditLog` with actor and timestamp |
