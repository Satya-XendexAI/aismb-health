# Appointment Booking System — Design Spec

**Date:** 2026-08-17 (revised 2026-08-18)
**Scope:** Backend LLD only — no UI/channel integration
**Status:** Approved

---

## 1. Overview

The appointment booking system supports two operating modes, configured per hospital at setup time. A hospital runs entirely in one mode — there is no per-doctor or runtime switching.

| Mode | Description |
|---|---|
| `TOKEN` | Walk-in queue. Patients receive a sequential token and wait in FIFO order. |
| `SLOT` | Pre-configured fixed time slots per doctor per day. Patients select and book an available slot. |

All state is held in-memory. No database or external persistence.

---

## 2. Architecture

```
┌──────────────────────────────────────────────────────────┐
│                    AppointmentEngine                      │
│                                                          │
│  + book(request) → Token | Appointment                   │
│  + cancel(booking_id, patient_id) → confirmation         │
│  + view_slots(hospital_id, doctor_id, date) → List[Slot] │
│  + get_queue_position(token_id) → int                    │
│                                                          │
│  [ selects strategy from HospitalConfig at construction ]│
└────────────────────┬─────────────────────────────────────┘
                     │ uses
          ┌──────────┴──────────┐
          │                     │
   ┌──────▼──────┐      ┌───────▼──────┐
   │TokenStrategy│      │ SlotStrategy  │
   │             │      │               │
   │ + book()    │      │ + book()      │
   │ + cancel()  │      │ + cancel()    │
   │ + position()│      │ + view()      │
   └──────┬──────┘      └───────┬───────┘
          └──────────┬──────────┘
                     ▼
               InternalStore
               (in-memory; owned by engine)
```

**HospitalConfig** — holds `hospital_id`, `name`, and `booking_mode`. Engine reads this at construction and injects the correct strategy. No DB row — passed in directly.

---

## 3. Data Models

### 3.1 Config (passed at construction)

```
HospitalConfig
  hospital_id       UUID
  name              str
  booking_mode      Enum        TOKEN | SLOT
```

### 3.2 Shared Entities (in InternalStore)

```
Patient
  patient_id        UUID        PK
  hospital_id       UUID
  name              str
  phone             str

Doctor
  doctor_id         UUID        PK
  hospital_id       UUID
  name              str
  specialization    str
  is_active         bool

DoctorSession                        -- tracks a doctor's active consultation day
  session_id        UUID        PK
  doctor_id         UUID
  hospital_id       UUID
  date              Date
  status            Enum        OPEN | CLOSED
  started_at        datetime
  ended_at          datetime?
```

### 3.3 Token-Mode Entities

```
Token
  token_id          UUID        PK
  session_id        UUID        FK → DoctorSession
  patient_id        UUID
  doctor_id         UUID
  hospital_id       UUID
  token_number      int         sequential; assigned by in-memory counter per session
  status            Enum        WAITING | SERVING | COMPLETED | CANCELLED
  issued_at         datetime
  called_at         datetime?
  served_at         datetime?
```

**Token counter:** An in-memory integer counter, one per session, starting at 1 and incrementing on each `book()` call. Counter is reset when the session closes.

**Queue position:** Count of WAITING tokens with `token_number < this token's token_number` in the same session.

### 3.4 Slot-Mode Entities

```
Slot
  slot_id           UUID        PK
  hospital_id       UUID
  doctor_id         UUID
  date              Date
  start_time        Time        e.g. 09:00
  end_time          Time        e.g. 09:20
  status            Enum        AVAILABLE | BOOKED | BLOCKED

Appointment
  appointment_id    UUID        PK
  hospital_id       UUID
  patient_id        UUID
  doctor_id         UUID
  slot_id           UUID        FK → Slot
  status            Enum        SCHEDULED | CANCELLED
  booked_at         datetime
  cancelled_at      datetime?
```

**Uniqueness rule:** One `SCHEDULED` appointment per `(patient_id, doctor_id, date)` — enforced in-memory before booking.

---

## 4. Core Flows

### 4.1 TOKEN — Book

```
Input:  hospital_id, doctor_id, patient_id

1. Validate patient exists in InternalStore
   → raise PatientNotFound if missing

2. Validate DoctorSession is OPEN today for this doctor + hospital
   → raise NoActiveSession if not found

3. Assign token_number = session_counter[session_id]++

4. Create Token(status = WAITING, issued_at = now)
   InternalStore.save_token(token)

5. Return token_number,
          queue_position = count of WAITING tokens with token_number < this token's
```

### 4.2 TOKEN — Cancel

```
Input:  token_id, patient_id

1. InternalStore.get_token(token_id)
   → raise TokenNotFound if missing

2. Validate token.patient_id == patient_id
   → raise Unauthorised if mismatch

3. Validate token.status == WAITING
   → raise TokenNotCancellable if SERVING | COMPLETED | CANCELLED

4. token.status = CANCELLED
   InternalStore.save_token(token)

5. Return confirmation
```

### 4.3 TOKEN — Get Queue Position

```
Input:  token_id

1. InternalStore.get_token(token_id)
   → raise TokenNotFound if missing

2. Validate token.status == WAITING
   → raise TokenNotActive if not WAITING

3. position = count of tokens in same session where
              status == WAITING AND token_number < token.token_number

4. Return position   (0 = next to be served)
```

### 4.4 SLOT — View Available Slots

```
Input:  hospital_id, doctor_id, date

1. Validate doctor belongs to hospital
   → raise DoctorNotFound if missing

2. Return all Slots where
     doctor_id == ? AND date == ? AND status == AVAILABLE
   ordered by start_time asc
```

### 4.5 SLOT — Book

```
Input:  hospital_id, doctor_id, patient_id, slot_id

1. Validate patient exists
   → raise PatientNotFound if missing

2. Validate doctor is active
   → raise DoctorNotFound if missing or inactive

3. Check no SCHEDULED appointment for (patient_id, doctor_id, date)
   → raise DuplicateBooking if exists

4. InternalStore.get_slot(slot_id)
   → raise SlotNotFound if missing
   → raise SlotUnavailable if status != AVAILABLE

5. slot.status = BOOKED
   Create Appointment(status = SCHEDULED, booked_at = now)
   InternalStore.save_slot(slot)
   InternalStore.save_appointment(appointment)

6. Return appointment_id, slot.start_time
```

### 4.6 SLOT — Cancel

```
Input:  appointment_id, patient_id

1. InternalStore.get_appointment(appointment_id)
   → raise AppointmentNotFound if missing

2. Validate appointment.patient_id == patient_id
   → raise Unauthorised if mismatch

3. Validate appointment.status == SCHEDULED
   → raise AppointmentNotCancellable if already CANCELLED

4. appointment.status = CANCELLED
   appointment.cancelled_at = now
   slot.status = AVAILABLE
   InternalStore.save_appointment(appointment)
   InternalStore.save_slot(slot)

5. Return confirmation
```

---

## 5. Session Lifecycle (TOKEN mode)

```
Doctor opens day:
  → Create DoctorSession(status = OPEN, started_at = now)
  → Initialise session_counter[session_id] = 1
  → InternalStore.save_session(session)

Doctor closes day:
  → session.status = CLOSED, ended_at = now
  → All WAITING tokens for this session → CANCELLED
  → SERVING token completes naturally (not force-cancelled)
  → session_counter[session_id] removed
  → InternalStore.save_session(session)
```

---

## 6. Error Cases

### TOKEN mode

| Scenario | Error Code | Behaviour |
|---|---|---|
| Patient not in store | `PATIENT_NOT_FOUND` | Reject booking |
| No OPEN doctor session today | `NO_ACTIVE_SESSION` | Reject booking |
| Token not found | `TOKEN_NOT_FOUND` | Reject cancel / position check |
| Patient ID mismatch on token | `UNAUTHORISED` | Reject cancel |
| Token status is SERVING / COMPLETED | `TOKEN_NOT_CANCELLABLE` | Reject cancel |
| Doctor closes session mid-queue | `SESSION_CLOSED` | All WAITING tokens → CANCELLED |

### SLOT mode

| Scenario | Error Code | Behaviour |
|---|---|---|
| Patient not in store | `PATIENT_NOT_FOUND` | Reject booking |
| Doctor not found or inactive | `DOCTOR_NOT_FOUND` | Reject booking / view |
| Slot not found | `SLOT_NOT_FOUND` | Reject booking |
| Slot already BOOKED or BLOCKED | `SLOT_UNAVAILABLE` | Reject booking |
| Duplicate booking (same doctor + date) | `DUPLICATE_BOOKING` | Reject — one appointment per patient per doctor per day |
| Appointment not found | `APPOINTMENT_NOT_FOUND` | Reject cancel |
| Patient ID mismatch on appointment | `UNAUTHORISED` | Reject cancel |
| Appointment already CANCELLED | `APPOINTMENT_NOT_CANCELLABLE` | Reject cancel |

---

## 7. Design Patterns

| Pattern | Application |
|---|---|
| Strategy | `BookingStrategy` interface — `TokenStrategy` and `SlotStrategy` swap in based on `HospitalConfig.booking_mode` |
| In-memory store | All state in `InternalStore`; no DB or persistence layer |
| FIFO ordering | Token queue is pure sequential — token_number determines order |
| Fail-fast validation | Patient, doctor, and slot checks run before any state mutation |
