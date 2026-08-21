# Appointment Tool Design — Hospital WhatsApp Assistant

**Date:** 2026-08-21
**Status:** Documented (tool already built; integration with orchestrator pending)

---

## Overview

`appoint_tool/` is a self-contained Python module that books and cancels patient tokens against a PostgreSQL database. It is called by the WhatsApp orchestrator as the real implementation behind the currently-mocked `appointment` tool.

Booking mode supported: **TOKEN** (queue-based). Time-slot booking is not implemented.

---

## File Responsibilities

| File | Responsibility |
|---|---|
| `config.py` | Reads DB credentials from `.env` (host, port, name, user, password) |
| `models.py` | Pydantic input/output contracts — `IncomingPayload`, `BookingResponse` |
| `database.py` | All PostgreSQL access via `psycopg2` — no business logic |
| `booking.py` | Book and cancel business logic — no direct DB access |
| `app.py` | Single public entry point: `handle_request(payload_dict) -> dict` |

---

## Database Schema

### `hospitals`
| Column | Type | Notes |
|---|---|---|
| `hospital_id` | TEXT | e.g. `"glngs-chn"` |
| `name` | TEXT | Display name |
| `booking_mode` | TEXT | `"TOKEN"` (only supported value) |
| `address` | TEXT | Optional |
| `city` | TEXT | |

### `doctors`
| Column | Type | Notes |
|---|---|---|
| `doctor_id` | TEXT | e.g. `"dr-selvakumar-k-chn"` |
| `hospital_id` | TEXT | FK → hospitals |
| `name` | TEXT | |
| `is_active` | BOOL | Only active doctors can be booked |
| `avg_checkin_time` | TIME | Doctor's typical arrival time |
| `avg_consultation_minutes` | INT | Average minutes per patient |
| `fee` | NUMERIC | Consultation fee |

### `patients`
| Column | Type | Notes |
|---|---|---|
| `patient_id` | SERIAL | Auto-generated |
| `hospital_id` | TEXT | FK → hospitals |
| `name` | TEXT | |
| `phone` | TEXT | Unique per hospital |
| `age` | INT | Optional |
| `location` | TEXT | Optional |
| `diagnosis` | TEXT | Stores symptoms from booking |

### `doctor_sessions`
| Column | Type | Notes |
|---|---|---|
| `session_id` | SERIAL | |
| `doctor_id` | TEXT | FK → doctors |
| `hospital_id` | TEXT | FK → hospitals |
| `date` | DATE | One session per doctor per day |
| `status` | TEXT | `"OPEN"` |
| `started_at` | TIMESTAMP | Actual check-in time; NULL until doctor arrives |

Unique constraint: `(hospital_id, doctor_id, date)`

### `tokens`
| Column | Type | Notes |
|---|---|---|
| `token_id` | SERIAL | |
| `session_id` | INT | FK → doctor_sessions |
| `patient_id` | INT | FK → patients |
| `doctor_id` | TEXT | |
| `hospital_id` | TEXT | |
| `department` | TEXT | |
| `token_number` | INT | Sequential within session |
| `status` | TEXT | `WAITING` → `CANCELLED` (or `DONE` when seen) |

---

## Input / Output Contracts

### Input — `IncomingPayload`
```python
action:           Literal["BOOK", "CANCEL"]
hospital_id:      str          # e.g. "glngs-chn"
doctor_id:        str          # e.g. "dr-selvakumar-k-chn"
department:       str          # required for BOOK
patient_name:     str
patient_phone:    str          # WhatsApp number → used as patient identity key
patient_age:      Optional[int]
patient_location: Optional[str]
symptoms:         Optional[str]   # stored as patients.diagnosis
```

### Output — `BookingResponse`
```python
action: Literal["BOOK", "CANCEL"]
result: BookingConfirmation | CancellationResult | ErrorResult
```

**`BookingConfirmation`** (BOOK success):
```python
status:           "CONFIRMED"
token_number:     int
doctor_name:      str
department:       str
hospital_name:    str
hospital_address: Optional[str]
fee:              Optional[float]
estimated_time:   datetime
```

**`CancellationResult`** (CANCEL):
```python
status:  "CANCELLED" | "PATIENT_NOT_FOUND" | "NO_ACTIVE_BOOKING"
message: str
```

**`ErrorResult`** (any failure):
```python
status:     "ERROR"
error_code: str    # HOSPITAL_NOT_FOUND | DOCTOR_NOT_FOUND | DUPLICATE_BOOKING
                   # UNSUPPORTED_BOOKING_MODE | INVALID_PAYLOAD
message:    str
```

---

## Book Flow (TOKEN mode)

```
BOOK request
  │
  ├─ 1. Validate hospital exists → ERROR: HOSPITAL_NOT_FOUND
  ├─ 2. Validate booking_mode == TOKEN → ERROR: UNSUPPORTED_BOOKING_MODE
  ├─ 3. Validate doctor active at hospital → ERROR: DOCTOR_NOT_FOUND
  ├─ 4. Find patient by phone; create if new
  ├─ 5. Get or create today's doctor_session (INSERT ... ON CONFLICT DO NOTHING)
  ├─ 6. Check for duplicate active token → ERROR: DUPLICATE_BOOKING
  ├─ 7. Insert token (SELECT FOR UPDATE on session to prevent race conditions)
  ├─ 8. Calculate ETA:
  │      if session.started_at → anchor = actual check-in time
  │      else                  → anchor = today + doctor.avg_checkin_time
  │      estimated_time = anchor + (patients_ahead × avg_consultation_minutes)
  └─ 9. Return BookingConfirmation
```

---

## Cancel Flow (TOKEN mode)

```
CANCEL request
  │
  ├─ 1. Validate hospital + mode
  ├─ 2. Find patient by phone → PATIENT_NOT_FOUND
  ├─ 3. Find active WAITING token for doctor today → NO_ACTIVE_BOOKING
  └─ 4. UPDATE token status = CANCELLED → return CANCELLED
```

---

## Concurrency Handling

`insert_token` acquires a row-level lock on the session (`SELECT FOR UPDATE`) before reading the max token number and inserting. This prevents two simultaneous bookings getting the same token number.

---

## Orchestrator Integration

### Current state
`orchestrator.py` mocks the appointment tool:
```python
if tool_call.tool_name == "appointment":
    return {"status": "ok", "result": "Appointment tool executed (dummy)"}
```

### Integration gap — tool schema mismatch
The current LLM tool schema for `appointment` only has `action`, `doctor_name`, `date`. The `appoint_tool` needs `doctor_id`, `department`, `patient_phone`, and `patient_name` — not doctor name or date.

Fields the orchestrator already has from session context (injected at execution time, not collected by LLM):
- `hospital_id` — from `wa_message.hospital_id`
- `patient_phone` — from `wa_message.from_number`

Fields the LLM must collect from the patient before calling the tool:
- `action` — BOOK or CANCEL
- `doctor_id` — resolved from `kg_retriever` results (the fused doctors list contains `sql_id`)
- `department` — from `kg_retriever` results (doctor's specialization)
- `patient_name` — asked by assistant if not known
- `patient_age` (optional)
- `patient_location` (optional)
- `symptoms` (optional — already collected if `kg_retriever` was used first)

### Required changes to wire up (out of scope for this spec)
1. Update `appointment` tool schema in `orchestrator.py` to match `IncomingPayload`
2. Replace mock in `_execute_tool` with `appoint_tool.app.handle_request()`
3. Inject `hospital_id` and `patient_phone` from session context before calling
4. Add `appoint_tool/` directory to Python path or convert to importable package

---

## What Is Not Changing
- `appoint_tool/` code is complete and not modified by this spec
- Time-slot booking mode is out of scope
- No API layer (FastAPI/Flask) — called directly as a Python function
