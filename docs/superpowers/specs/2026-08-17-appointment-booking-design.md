# Appointment Booking System — Design Spec

**Date:** 2026-08-17 (revised 2026-08-18 ×2)
**Scope:** Backend LLD only — no UI/channel integration
**Status:** Approved

---

## 1. Overview

The appointment booking system supports two operating modes, configured per hospital at setup time. A hospital runs entirely in one mode — there is no per-doctor or runtime switching.

| Mode | Description |
|---|---|
| `TOKEN` | Walk-in queue. Patients receive a sequential token and wait in FIFO order. |
| `SLOT` | Pre-configured fixed time slots per doctor per day. Patients select and book an available slot. |

All state is persisted in a SQL database via a `Repository` layer. Strategies interact with the DB only through `Repository` — no raw SQL in business logic.

---

## 2. Architecture

```
┌──────────────────────────────────────────────────────────┐
│                    AppointmentEngine                      │
│                                                          │
│  + book(request) → Token | Appointment                   │
│  + cancel(booking_id, patient_id) → confirmation         │
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
   └──────┬──────┘      └───────┬───────┘
          └──────────┬──────────┘
                     ▼
               Repository
               (SQL DB — Postgres)
```

**HospitalConfig** — stored as a `hospitals` table row. Engine reads `booking_mode` at startup via `Repository.get_hospital(hospital_id)` and injects the correct strategy.

---

## 3. Data Models

### 3.1 DB Tables — Shared

```
hospitals
  hospital_id       UUID        PK
  name              str
  booking_mode      Enum        TOKEN | SLOT

patients
  patient_id        UUID        PK
  hospital_id       UUID        FK → hospitals
  name              str
  phone             str

doctors
  doctor_id         UUID        PK
  hospital_id       UUID        FK → hospitals
  name              str
  specialization    str
  is_active         bool

doctor_sessions                      -- one row per doctor per working day
  session_id        UUID        PK
  doctor_id         UUID        FK → doctors
  hospital_id       UUID        FK → hospitals
  date              Date
  status            Enum        OPEN | CLOSED
  started_at        datetime
  ended_at          datetime?

  UNIQUE (doctor_id, date)           -- one session per doctor per day
```

### 3.2 DB Tables — TOKEN mode

```
tokens
  token_id          UUID        PK
  session_id        UUID        FK → doctor_sessions
  patient_id        UUID        FK → patients
  doctor_id         UUID        FK → doctors
  hospital_id       UUID        FK → hospitals
  token_number      int         assigned via DB sequence per session (see §6)
  status            Enum        WAITING | SERVING | COMPLETED | CANCELLED
  issued_at         datetime
  called_at         datetime?
  served_at         datetime?
```

**Token number:** Assigned using a per-session DB sequence (`token_seq_{session_id}`), guaranteeing uniqueness and strict ordering with no gaps from concurrent inserts.

**Queue position:** `SELECT COUNT(*) FROM tokens WHERE session_id = ? AND status = 'WAITING' AND token_number < ?`

### 3.3 DB Tables — SLOT mode

```
slots
  slot_id           UUID        PK
  hospital_id       UUID        FK → hospitals
  doctor_id         UUID        FK → doctors
  date              Date
  start_time        Time        e.g. 09:00
  end_time          Time        e.g. 09:20
  status            Enum        AVAILABLE | BOOKED | BLOCKED

appointments
  appointment_id    UUID        PK
  hospital_id       UUID        FK → hospitals
  patient_id        UUID        FK → patients
  doctor_id         UUID        FK → doctors
  slot_id           UUID        FK → slots
  status            Enum        SCHEDULED | CANCELLED
  booked_at         datetime
  cancelled_at      datetime?

  UNIQUE (patient_id, doctor_id, date) WHERE status = 'SCHEDULED'
    -- DB partial unique index; prevents same patient booking same doctor twice on same day
```

---

## 4. Core Flows

### 4.1 TOKEN — Book

```
Input:  hospital_id, doctor_id, patient_id

1. SELECT patient FROM patients WHERE patient_id = ? AND hospital_id = ?
   → raise PatientNotFound if missing

2. SELECT session FROM doctor_sessions WHERE doctor_id = ? AND hospital_id = ?
     AND date = today AND status = 'OPEN'
   → raise NoActiveSession if not found

3. token_number = nextval('token_seq_{session_id}')   ← DB sequence, atomic

4. INSERT tokens(token_number, status = WAITING, issued_at = now)

5. Return token_number
```

### 4.2 TOKEN — Cancel

```
Input:  token_id, patient_id

1. SELECT * FROM tokens WHERE token_id = ?
   → raise TokenNotFound if missing

2. Validate token.patient_id == patient_id
   → raise Unauthorised if mismatch

3. Validate token.status == WAITING
   → raise TokenNotCancellable if SERVING | COMPLETED | CANCELLED

4. UPDATE tokens SET status = 'CANCELLED' WHERE token_id = ?

5. Return confirmation
```

### 4.3 SLOT — Book

```
Input:  hospital_id, doctor_id, patient_id, slot_id

1. SELECT * FROM patients WHERE patient_id = ? AND hospital_id = ?
   → raise PatientNotFound if missing

2. SELECT * FROM doctors WHERE doctor_id = ? AND hospital_id = ? AND is_active = true
   → raise DoctorNotFound if missing or inactive

3. BEGIN TRANSACTION
     a. SELECT * FROM slots WHERE slot_id = ? FOR UPDATE   ← row lock
        → raise SlotNotFound if missing
        → raise SlotUnavailable if status != 'AVAILABLE'

     b. Check no SCHEDULED appointment for (patient_id, doctor_id, date):
        SELECT COUNT(*) FROM appointments
        WHERE patient_id = ? AND doctor_id = ? AND date = ?
          AND status = 'SCHEDULED'
        → raise DuplicateBooking if count > 0
        (DB partial unique index is the safety net; this check is a fast-fail)

     c. UPDATE slots SET status = 'BOOKED' WHERE slot_id = ?
     d. INSERT INTO appointments(status = 'SCHEDULED', booked_at = now)
4. COMMIT

5. Return appointment_id, slot.start_time
```

### 4.4 SLOT — Cancel

```
Input:  appointment_id, patient_id

1. SELECT * FROM appointments WHERE appointment_id = ?
   → raise AppointmentNotFound if missing

2. Validate appointment.patient_id == patient_id
   → raise Unauthorised if mismatch

3. Validate appointment.status == SCHEDULED
   → raise AppointmentNotCancellable if already CANCELLED

4. BEGIN TRANSACTION
     UPDATE appointments SET status = 'CANCELLED', cancelled_at = now
       WHERE appointment_id = ?
     UPDATE slots SET status = 'AVAILABLE' WHERE slot_id = appointment.slot_id
   COMMIT

5. Return confirmation
```

---

## 5. Error Cases

### TOKEN mode

| Scenario | Error Code | Behaviour |
|---|---|---|
| Patient not in store | `PATIENT_NOT_FOUND` | Reject booking |
| No OPEN doctor session today | `NO_ACTIVE_SESSION` | Reject booking |
| Token not found | `TOKEN_NOT_FOUND` | Reject cancel |
| Patient ID mismatch on token | `UNAUTHORISED` | Reject cancel |
| Token status is SERVING / COMPLETED | `TOKEN_NOT_CANCELLABLE` | Reject cancel |
| Doctor closes session mid-queue | `SESSION_CLOSED` | All WAITING tokens → CANCELLED |

### SLOT mode

| Scenario | Error Code | Behaviour |
|---|---|---|
| Patient not in store | `PATIENT_NOT_FOUND` | Reject booking |
| Doctor not found or inactive | `DOCTOR_NOT_FOUND` | Reject booking |
| Slot not found | `SLOT_NOT_FOUND` | Reject booking |
| Slot already BOOKED or BLOCKED | `SLOT_UNAVAILABLE` | Reject booking |
| Duplicate booking (same doctor + date) | `DUPLICATE_BOOKING` | Reject — one appointment per patient per doctor per day |
| Appointment not found | `APPOINTMENT_NOT_FOUND` | Reject cancel |
| Patient ID mismatch on appointment | `UNAUTHORISED` | Reject cancel |
| Appointment already CANCELLED | `APPOINTMENT_NOT_CANCELLABLE` | Reject cancel |

---

## 6. Concurrency

### Token numbering — DB sequence
```sql
-- Created when doctor opens the day
CREATE SEQUENCE token_seq_{session_id} START 1 INCREMENT 1;

-- Used on every token insert (atomic, no duplicates)
INSERT INTO tokens (..., token_number)
VALUES (..., nextval('token_seq_{session_id}'));

-- Dropped when doctor closes the day
DROP SEQUENCE token_seq_{session_id};
```

### Slot booking — row-level lock
```sql
BEGIN;
  SELECT * FROM slots WHERE slot_id = ? FOR UPDATE;
  -- concurrent bookings queue behind this lock (<10ms, no external calls inside txn)
  UPDATE slots SET status = 'BOOKED' WHERE slot_id = ?;
  INSERT INTO appointments (...);
COMMIT;
```

### Slot cancel — atomic transaction
```sql
BEGIN;
  UPDATE appointments SET status = 'CANCELLED', cancelled_at = now WHERE appointment_id = ?;
  UPDATE slots SET status = 'AVAILABLE' WHERE slot_id = ?;
COMMIT;
-- If either update fails, both roll back — slot never freed without appointment cancelled
```

---

## 7. Design Patterns

| Pattern | Application |
|---|---|
| Strategy | `BookingStrategy` interface — `TokenStrategy` and `SlotStrategy` swap in based on `HospitalConfig.booking_mode` |
| Repository | All DB access goes through `Repository`; no raw SQL in strategy or engine classes |
| DB sequence | Per-session `token_seq_{session_id}` guarantees atomic, gap-free token numbering |
| Pessimistic lock | `SELECT FOR UPDATE` on slot prevents double-booking under concurrent requests |
| Atomic transaction | Slot cancel wraps appointment + slot updates in one transaction |
| FIFO ordering | Token queue is pure sequential — `token_number` determines order |
| Fail-fast validation | Patient, doctor, and slot checks run before any state mutation |
