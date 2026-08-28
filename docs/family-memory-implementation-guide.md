# 👨‍👩‍👧 Family Memory System — Implementation Guide

**Version:** 1.0
**Date:** 2026-08-25
**Status:** Ready for Implementation
**Repo:** `C:\hospital_appointment_management`

---

## 📖 Table of Contents

1. [Problem Statement](#-problem-statement)
2. [Solution Overview](#-solution-overview)
3. [Current Codebase Anatomy](#-current-codebase-anatomy)
4. [Database Changes](#-database-changes)
5. [Code Changes — File by File](#-code-changes--file-by-file)
6. [End-to-End Flow Walkthrough](#-end-to-end-flow-walkthrough)
7. [Summary of All Changes](#-summary-of-all-changes)

---

## 🎯 Problem Statement

### Current Behavior (Broken for Families)

The booking system assumes **1 phone number = 1 patient**. Every booking uses the WhatsApp sender's phone as the patient lookup key.

**Where the assumption lives (3 places):**

| Location | Line(s) | What it does |
|---|---|---|
| `orchestrator/core.py` | 158–162 | `_execute_tool()` **overwrites** `patient_phone` with `context.wa_message.from_number` — the LLM can never set a different phone |
| `tools/appointment/database.py` | 54–63 | `find_patient(phone, hospital_id)` — looks up patient **by phone only**, no name/relation filter |
| `orchestrator/schemas.py` | 12–34 | Tool schema has no `relation_to_booker` or `patient_phone` field — LLM can't say "this is for my wife" |

### Real-World Scenario That Breaks

```
📱 Ravi Kumar (9876543210) messages WhatsApp:

"Book appointment for my wife Priya"
   → System creates Priya with phone 9876543210
   → But find_patient(9876543210) returns Ravi's row (created first)
   → Priya never gets her own record

"Also book for my father Ramesh (phone: 9998887770)"
   → core.py line 161 overwrites patient_phone with 9876543210
   → Ramesh gets booked under Ravi's number anyway

"Cancel my wife's appointment"
   → find_patient(9876543210) returns Ravi
   → WRONG cancellation
```

### Root Cause Summary

1. **`core.py:161`** — Hard-overwrites `patient_phone`, blocking any family phone
2. **`database.py:54`** — `find_patient()` matches phone only, returns first match — non-deterministic with shared phones
3. **`schemas.py`** — No `relation_to_booker` field exposed to LLM — can't express "booking for someone else"

---

## 💡 Solution Overview

### Design Principles

| Principle | Implementation |
|---|---|
| One row per person | Each family member is a separate `patients` row |
| Track the booker | New `booked_by_phone` column = WhatsApp sender |
| Free-text relations | `relation_to_booker` is VARCHAR, not an enum ("wife", "father" — anything) |
| Multi-tenant safe | Every query filters by `hospital_id` |
| No new tables | Extend existing `patients` table only |
| No new Python modules | All changes go into existing files |

### What Changes

```
Database:     1 table modified (patients) — 2 new columns + indexes
New files:    1 SQL migration file
Modified:     5 existing Python files (exact lines specified below)
New modules:  0 (no tools/family_memory/ needed — fold into existing files)
```

---

## 🗂 Current Codebase Anatomy

### Relevant File Map

```
hospital_appointment_management/
├── models/
│   └── appointment.py          ← IncomingPayload, BookingConfirmation, etc.
├── tools/
│   └── appointment/
│       ├── __init__.py         ← handle_request() entry point
│       ├── booking.py          ← book() and cancel() business logic
│       └── database.py         ← SQL queries (find_patient, insert_patient, etc.)
├── orchestrator/
│   ├── core.py                 ← _execute_tool(), _describe_tool()
│   └── schemas.py              ← LLM tool schemas (what fields LLM can populate)
├── prompts/
│   └── system.py               ← PATIENT_SYSTEM_PROMPT (LLM behavior instructions)
└── migrations/                 ← NEW directory (doesn't exist yet)
    └── 0001_add_family_support.sql  ← NEW file
```

### Current Data Flow (Book Request)

```
WhatsApp Message
    ↓
orchestrator/core.py: _execute_tool()  [line 155-163]
    → Builds payload dict: {**tool_call.args, hospital_id, patient_phone}
    → ⚠️ BUG: patient_phone is ALWAYS set to wa_message.from_number
    ↓
tools/appointment/__init__.py: handle_request()  [line 5-24]
    → Validates payload via IncomingPayload model
    → Opens DB connection
    → Calls booking.book(conn, payload)
    ↓
tools/appointment/booking.py: book()  [line 34-92]
    → db.find_patient(phone, hospital_id)  ← ⚠️ phone-only lookup
    → If not found: db.insert_patient(...)
    → db.get_or_create_today_session(...)
    → db.insert_token(...)
    → Return BookingConfirmation
```

---

## 🗄 Database Changes

### What Exists Today

```sql
-- Current patients table (from Supabase)
CREATE TABLE patients (
    patient_id    UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    hospital_id   TEXT NOT NULL REFERENCES hospitals(hospital_id),
    name          VARCHAR(255) NOT NULL,
    phone         VARCHAR(20) NOT NULL,
    age           INTEGER,
    location      VARCHAR(255),
    diagnosis     VARCHAR(255),
    created_at    TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at    TIMESTAMP NOT NULL DEFAULT NOW()
);
```

### New Migration File

**Create:** `migrations/0001_add_family_support.sql`

```sql
-- ═══════════════════════════════════════════════════════════════
-- MIGRATION: Add Family Member Support to patients table
-- Date: 2026-08-25
-- ═══════════════════════════════════════════════════════════════

-- STEP 1: Add new columns
-- booked_by_phone: the WhatsApp number that initiated the booking
-- relation_to_booker: free-text relation ("self", "wife", "father", etc.)

ALTER TABLE patients
    ADD COLUMN IF NOT EXISTS booked_by_phone    VARCHAR(20),
    ADD COLUMN IF NOT EXISTS relation_to_booker  VARCHAR(50) NOT NULL DEFAULT 'self';

-- STEP 2: Backfill existing rows (all were self-bookings)

UPDATE patients
SET booked_by_phone = phone
WHERE booked_by_phone IS NULL;

-- STEP 3: Make booked_by_phone NOT NULL after backfill

ALTER TABLE patients
    ALTER COLUMN booked_by_phone SET NOT NULL;

-- STEP 4: Indexes for fast family lookups

CREATE INDEX IF NOT EXISTS idx_patients_booked_by_phone
    ON patients(hospital_id, booked_by_phone);

-- Case-insensitive name search within a family
CREATE INDEX IF NOT EXISTS idx_patients_family_name
    ON patients(hospital_id, booked_by_phone, (LOWER(name)));

-- STEP 5: Unique constraint to prevent duplicate family members
-- Same pattern as doctor_sessions UNIQUE(hospital_id, doctor_id, date)
-- Prevents webhook re-delivery from creating duplicate rows

CREATE UNIQUE INDEX IF NOT EXISTS uq_patients_family_member
    ON patients(hospital_id, booked_by_phone, (LOWER(name)));

-- STEP 6: Verify

SELECT column_name, data_type, column_default, is_nullable
FROM information_schema.columns
WHERE table_name = 'patients'
ORDER BY ordinal_position;
```

### After Migration — patients Table

```
patients (modified):
┌──────────────────────┬──────────────────────────────────────────────────┐
│ Column               │ Purpose                                          │
├──────────────────────┼──────────────────────────────────────────────────┤
│ patient_id (PK)      │ UUID, auto-generated                             │
│ hospital_id (FK)     │ Tenant scoping                                   │
│ name                 │ Patient's full name                              │
│ phone                │ Patient's own phone (may = booker's)             │
│ age                  │ Patient's age                                    │
│ location             │ Patient's area/city                              │
│ diagnosis            │ Symptoms/diagnosis                               │
│ booked_by_phone  🆕  │ WhatsApp sender's phone (who initiated booking)  │
│ relation_to_booker🆕 │ Free text: "self", "wife", "father", etc.        │
│ created_at           │ Auto                                             │
│ updated_at           │ Auto (via existing trigger)                      │
└──────────────────────┴──────────────────────────────────────────────────┘

UNIQUE constraint: (hospital_id, booked_by_phone, LOWER(name))
```

---

## 🔧 Code Changes — File by File

### File 1: `models/appointment.py`

**Path:** `c:\hospital_appointment_management\models\appointment.py`
**Total lines:** 44 → ~56

#### Changes

| Lines | What | Change |
|---|---|---|
| 6–16 | `IncomingPayload` | Add `booker_phone`, `relation_to_booker`; make `patient_phone` optional |
| 19–27 | `BookingConfirmation` | Add `patient_name`, `relation_to_booker` |
| 30–32 | `CancellationResult` | Add `cancelled_for` |

#### Full Replacement File

```python
from datetime import datetime
from typing import Literal, Optional, Union
from pydantic import BaseModel


class IncomingPayload(BaseModel):
    action:              Literal["BOOK", "CANCEL"]
    hospital_id:         str
    doctor_id:           str
    department:          str
    patient_name:        str
    patient_phone:       Optional[str] = None       # 🆕 None = same as booker
    patient_age:         Optional[int] = None
    patient_location:    Optional[str] = None
    symptoms:            Optional[str] = None
    date:                Optional[str] = None        # YYYY-MM-DD (kept as-is)
    booker_phone:        str                         # 🆕 REQUIRED — set by core.py
    relation_to_booker:  str = "self"                # 🆕 free-text relation


class BookingConfirmation(BaseModel):
    status:              Literal["CONFIRMED"]
    token_number:        int
    patient_name:        str                         # 🆕 who the booking is for
    relation_to_booker:  str                         # 🆕 their relation to sender
    doctor_name:         str
    department:          str
    hospital_name:       str
    hospital_address:    Optional[str]   = None
    fee:                 Optional[float] = None
    estimated_time:      datetime


class CancellationResult(BaseModel):
    status:        Literal["CANCELLED", "PATIENT_NOT_FOUND", "NO_ACTIVE_BOOKING"]
    message:       str
    cancelled_for: Optional[str] = None              # 🆕 whose booking was cancelled


class ErrorResult(BaseModel):
    status:     Literal["ERROR"]
    error_code: str
    message:    str


class BookingResponse(BaseModel):
    action: Literal["BOOK", "CANCEL"]
    result: Union[BookingConfirmation, CancellationResult, ErrorResult]
```

**Key decisions:**
- `patient_phone` becomes `Optional[str] = None` — if the LLM doesn't provide it, `core.py` fills it with the sender's number (same as today's behavior for self-bookings)
- `booker_phone` is required but set by `core.py`, not the LLM — ensures it's always the actual WhatsApp sender
- `date` field is **kept** (the earlier guide draft accidentally dropped it)

---

### File 2: `tools/appointment/database.py`

**Path:** `c:\hospital_appointment_management\tools\appointment\database.py`
**Total lines:** 177 → ~210

#### Changes

| Lines | What | Change |
|---|---|---|
| 54–63 | `find_patient()` | **Replace** with `find_family_member()` — matches by booker_phone + hospital_id + name (+ optional relation) |
| 66–74 | `insert_patient()` | **Replace** with `insert_family_member()` — adds booker_phone, relation, uses ON CONFLICT for idempotency |
| (new) | `touch_family_member()` | **Add** after line 74 — updates `updated_at` for sorting by recent usage |

#### What to Replace

**Replace lines 54–74** (the `find_patient` and `insert_patient` functions) with:

```python
def find_family_member(conn, booker_phone, hospital_id, patient_name, relation=None):
    """
    Find an existing family member.

    Match criteria (all case-insensitive):
      - Same booker_phone (WhatsApp sender)
      - Same hospital_id (tenant scoping)
      - Same name (case-insensitive exact match)
      - Optional: same relation

    Returns single patient dict or None.
    """
    conditions = [
        "booked_by_phone = %s",
        "hospital_id = %s",
        "LOWER(name) = LOWER(%s)",
    ]
    params = [booker_phone, str(hospital_id), patient_name.strip()]

    if relation and relation.strip().lower() != "self":
        conditions.append("LOWER(relation_to_booker) = LOWER(%s)")
        params.append(relation.strip())

    sql = f"""
        SELECT patient_id, hospital_id, name, phone, age, location, diagnosis,
               booked_by_phone, relation_to_booker
        FROM patients
        WHERE {" AND ".join(conditions)}
        ORDER BY updated_at DESC
        LIMIT 1
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, tuple(params))
        return cur.fetchone()


def insert_family_member(conn, hospital_id, booker_phone, name, phone,
                         relation, age, location, diagnosis):
    """
    Register a new family member in the patients table.

    Uses ON CONFLICT to handle webhook re-delivery — if a matching row
    already exists (same hospital + booker + name), returns None so we
    can fall back to find_family_member().
    """
    sql = """
        INSERT INTO patients (hospital_id, name, phone, age, location, diagnosis,
                              booked_by_phone, relation_to_booker)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (hospital_id, booked_by_phone, LOWER(name)) DO NOTHING
        RETURNING patient_id, hospital_id, name, phone, age, location, diagnosis,
                  booked_by_phone, relation_to_booker
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, (
            str(hospital_id), name.strip(), phone, age, location, diagnosis,
            booker_phone.strip(), (relation or "self").strip().lower(),
        ))
        row = cur.fetchone()
        if row is None:
            # Lost the race to a concurrent insert — read what won
            return find_family_member(conn, booker_phone, hospital_id, name, relation)
        return row


def touch_family_member(conn, patient_id):
    """Update updated_at so recently-booked family members appear first."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE patients SET updated_at = NOW() WHERE patient_id = %s",
            (str(patient_id),),
        )
```

**Everything else in this file stays untouched:** `get_connection`, `get_hospital`, `get_doctor`, `get_or_create_today_session`, `insert_token`, `count_patients_ahead`, `find_active_token`, `cancel_token`.

---

### File 3: `tools/appointment/booking.py`

**Path:** `c:\hospital_appointment_management\tools\appointment\booking.py`
**Total lines:** 117 → ~125

#### Changes

| Lines | What | Change |
|---|---|---|
| 48–58 | `book()` patient lookup | Replace `find_patient`/`insert_patient` with `find_family_member`/`insert_family_member` |
| 68–69 | `book()` duplicate msg | Include patient name in duplicate error |
| 78 (after) | `book()` | Add `touch_family_member()` call |
| 83–92 | `book()` return | Add `patient_name` and `relation_to_booker` to BookingConfirmation |
| 104–107 | `cancel()` patient lookup | Replace `find_patient` with `find_family_member` |
| 115–116 | `cancel()` return | Add `cancelled_for` to CancellationResult |

#### Full Replacement File

```python
from datetime import datetime, date, timedelta
import tools.appointment.database as db
from models.appointment import BookingConfirmation, CancellationResult, ErrorResult

SUPPORTED_BOOKING_MODE = "TOKEN"


def _check_supported_mode(hospital) -> ErrorResult | None:
    if hospital["booking_mode"] != SUPPORTED_BOOKING_MODE:
        return ErrorResult(
            status="ERROR",
            error_code="UNSUPPORTED_BOOKING_MODE",
            message=(
                f"This module only supports {SUPPORTED_BOOKING_MODE} mode; "
                f"hospital is configured for {hospital['booking_mode']}."
            ),
        )
    return None


def calculate_eta(session, doctor, patients_ahead):
    if session["started_at"] is not None:
        anchor_time = session["started_at"]
    else:
        session_date = session["date"] if session.get("date") else date.today()
        if isinstance(session_date, str):
            from datetime import date as date_cls
            session_date = date_cls.fromisoformat(session_date)
        anchor_time = datetime.combine(session_date, doctor["avg_checkin_time"])
    wait_minutes = patients_ahead * doctor["avg_consultation_minutes"]
    return anchor_time + timedelta(minutes=wait_minutes)


def book(conn, payload):
    hospital = db.get_hospital(conn, payload.hospital_id)
    if not hospital:
        return ErrorResult(status="ERROR", error_code="HOSPITAL_NOT_FOUND",
                           message=f"Hospital {payload.hospital_id} not found.")
    mode_error = _check_supported_mode(hospital)
    if mode_error:
        return mode_error

    doctor = db.get_doctor(conn, payload.doctor_id, payload.hospital_id)
    if not doctor:
        return ErrorResult(status="ERROR", error_code="DOCTOR_NOT_FOUND",
                           message=f"Doctor {payload.doctor_id} not found or inactive.")

    # ─── Family-aware patient lookup ────────────────────────── 🆕
    patient = db.find_family_member(
        conn, payload.booker_phone, payload.hospital_id,
        payload.patient_name, payload.relation_to_booker,
    )
    if not patient:
        patient = db.insert_family_member(
            conn,
            hospital_id=payload.hospital_id,
            booker_phone=payload.booker_phone,
            name=payload.patient_name,
            phone=payload.patient_phone or payload.booker_phone,
            relation=payload.relation_to_booker,
            age=payload.patient_age,
            location=payload.patient_location,
            diagnosis=payload.symptoms,
        )

    session = db.get_or_create_today_session(
        conn, payload.doctor_id, payload.hospital_id, payload.date
    )

    existing_token = db.find_active_token(
        conn, patient["patient_id"], payload.doctor_id, payload.date
    )
    if existing_token:
        return ErrorResult(
            status="ERROR", error_code="DUPLICATE_BOOKING",
            message=(                                              # 🆕 includes name
                f"{patient['name']} already has token "
                f"#{existing_token['token_number']} with this doctor."
            ),
        )

    token = db.insert_token(
        conn,
        session_id=session["session_id"],
        patient_id=patient["patient_id"],
        doctor_id=payload.doctor_id,
        hospital_id=payload.hospital_id,
        department=payload.department,
    )

    db.touch_family_member(conn, patient["patient_id"])            # 🆕

    patients_ahead = db.count_patients_ahead(conn, session["session_id"], token["token_number"])
    estimated_time = calculate_eta(session, doctor, patients_ahead)

    return BookingConfirmation(
        status="CONFIRMED",
        token_number=token["token_number"],
        patient_name=patient["name"],                              # 🆕
        relation_to_booker=patient["relation_to_booker"],          # 🆕
        doctor_name=doctor["name"],
        department=payload.department,
        hospital_name=hospital["name"],
        hospital_address=hospital.get("address"),
        fee=doctor.get("fee"),
        estimated_time=estimated_time,
    )


def cancel(conn, payload):
    hospital = db.get_hospital(conn, payload.hospital_id)
    if not hospital:
        return ErrorResult(status="ERROR", error_code="HOSPITAL_NOT_FOUND",
                           message=f"Hospital {payload.hospital_id} not found.")
    mode_error = _check_supported_mode(hospital)
    if mode_error:
        return mode_error

    # ─── Family-aware patient lookup ────────────────────────── 🆕
    patient = db.find_family_member(
        conn, payload.booker_phone, payload.hospital_id,
        payload.patient_name, payload.relation_to_booker,
    )
    if not patient:
        return CancellationResult(
            status="PATIENT_NOT_FOUND",
            message=f"No record found for '{payload.patient_name}'.",
        )

    active = db.find_active_token(conn, patient["patient_id"], payload.doctor_id, payload.date)
    if not active:
        return CancellationResult(status="NO_ACTIVE_BOOKING",
                                  message=f"No active booking found for {patient['name']}.")

    db.cancel_token(conn, active["token_id"])
    return CancellationResult(
        status="CANCELLED",
        message=f"Token #{active['token_number']} for {patient['name']} has been cancelled.",
        cancelled_for=patient["name"],                             # 🆕
    )
```

---

### File 4: `orchestrator/schemas.py`

**Path:** `c:\hospital_appointment_management\orchestrator\schemas.py`
**Lines to modify:** 14–34 (inside `_appointment_schema["function"]["parameters"]["properties"]`)

#### Add Two Properties (after line 32, before line 33)

```python
                "relation_to_booker": {
                    "type": "string",
                    "description": (
                        "Who the appointment is for, relative to the WhatsApp sender. "
                        "'self' if booking for themselves, otherwise the relation in the "
                        "patient's own words (e.g. 'wife', 'father', 'son', 'brother-in-law'). "
                        "Default: 'self'."
                    ),
                },
                "patient_phone": {
                    "type": "string",
                    "description": (
                        "Only set this if the family member has their OWN separate phone number "
                        "(e.g. an elderly parent with their own phone). Omit entirely if they share "
                        "the WhatsApp sender's number."
                    ),
                },
```

#### Full `_appointment_schema` After Edit

```python
_appointment_schema = {
    "type": "function",
    "function": {
        "name": "appointment",
        "description": (
            "Book or cancel a token (queue-based) appointment for the patient. "
            "Always call kg_retriever first to get the doctor_id and department. "
            "Ask for patient_name before calling this tool if not already known."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action":           {"type": "string", "enum": ["BOOK", "CANCEL"],
                                     "description": "BOOK to book a new token, CANCEL to cancel existing"},
                "doctor_id":        {"type": "string",
                                     "description": "Doctor's ID from kg_retriever results (sql_id field)"},
                "department":       {"type": "string",
                                     "description": "Doctor's department or specialization"},
                "patient_name":     {"type": "string",
                                     "description": "Patient's full name"},
                "patient_age":      {"type": "integer",
                                     "description": "Patient's age (optional)"},
                "patient_location": {"type": "string",
                                     "description": "Patient's city or location (optional)"},
                "doctor_name":      {"type": "string",
                                     "description": "Doctor's display name as shown to the patient (optional, used in confirmation message)"},
                "symptoms":         {"type": "string",
                                     "description": "Patient's symptoms (optional)"},
                "date":             {"type": "string",
                                     "description": "Appointment date in YYYY-MM-DD format (optional, defaults to today)"},
                "relation_to_booker": {                              # 🆕
                    "type": "string",
                    "description": (
                        "Who the appointment is for, relative to the WhatsApp sender. "
                        "'self' if booking for themselves, otherwise the relation in the "
                        "patient's own words (e.g. 'wife', 'father', 'son', 'brother-in-law'). "
                        "Default: 'self'."
                    ),
                },
                "patient_phone": {                                   # 🆕
                    "type": "string",
                    "description": (
                        "Only set this if the family member has their OWN separate phone "
                        "number (e.g. an elderly parent with their own phone). Omit "
                        "entirely if they share the WhatsApp sender's number."
                    ),
                },
            },
            "required": ["action", "doctor_id", "department", "patient_name"],
            # Note: relation_to_booker and patient_phone are intentionally NOT required
        },
    },
}
```

**No other changes to this file.** `_kg_retriever_schema`, `_query_data_schema`, tool lists, and `ROLE_PERMISSIONS` all stay the same.

---

### File 5: `orchestrator/core.py`

**Path:** `c:\hospital_appointment_management\orchestrator\core.py`

#### Change 1: `_execute_tool()` — Lines 155–163

**THE BUG FIX.** Stop overwriting `patient_phone`. Add `booker_phone`.

**Current (broken):**
```python
    def _execute_tool(self, tool_call, context: OrchestratorContext) -> dict:
        if tool_call.tool_name == "appointment":
            from tools.appointment import handle_request
            payload = {
                **tool_call.args,
                "hospital_id":   context.wa_message.hospital_id,
                "patient_phone": context.wa_message.from_number,
            }
            return handle_request(payload)
```

**New (fixed):**
```python
    def _execute_tool(self, tool_call, context: OrchestratorContext) -> dict:
        if tool_call.tool_name == "appointment":
            from tools.appointment import handle_request
            payload = {
                **tool_call.args,
                "hospital_id":   context.wa_message.hospital_id,
                "booker_phone":  context.wa_message.from_number,                         # 🆕
                "patient_phone": tool_call.args.get("patient_phone")                     # 🆕
                                 or context.wa_message.from_number,
            }
            return handle_request(payload)
```

**Why:**
- `booker_phone` is always the WhatsApp sender (set server-side, can't be spoofed)
- `patient_phone` uses the LLM-supplied value if present (family member's own phone), otherwise defaults to the sender's number (same as today)

#### Change 2: `_describe_tool()` — Lines 201–213

Add the relation to the pre-booking confirmation message so users see "Book appointment with Dr. X for Priya (wife)" before confirming.

**Current:**
```python
    def _describe_tool(self, tool_call) -> str:
        action = tool_call.args.get("action", "BOOK").upper()
        doctor = self._doctor_display_name(tool_call.args)
        dept   = tool_call.args.get("department", "")
        name   = tool_call.args.get("patient_name", "")
        date   = tool_call.args.get("date", "today")
        desc   = f"{action} appointment with {doctor}"
        if dept:
            desc += f" ({dept})"
        if name:
            desc += f" for {name}"
        desc += f" on {date}"
        return desc
```

**New:**
```python
    def _describe_tool(self, tool_call) -> str:
        action   = tool_call.args.get("action", "BOOK").upper()
        doctor   = self._doctor_display_name(tool_call.args)
        dept     = tool_call.args.get("department", "")
        name     = tool_call.args.get("patient_name", "")
        relation = tool_call.args.get("relation_to_booker", "self")  # 🆕
        date     = tool_call.args.get("date", "today")
        desc     = f"{action} appointment with {doctor}"
        if dept:
            desc += f" ({dept})"
        if name:
            suffix = f" ({relation})" if relation and relation != "self" else ""  # 🆕
            desc += f" for {name}{suffix}"
        desc += f" on {date}"
        return desc
```

**Effect:** Confirmation message changes from:
- Before: `"Please confirm: BOOK appointment with Dr. Ajit Yadav for Priya on today"`
- After: `"Please confirm: BOOK appointment with Dr. Ajit Yadav for Priya (wife) on today"`

---

### File 6: `prompts/system.py`

**Path:** `c:\hospital_appointment_management\prompts\system.py`
**Lines to modify:** 27–31 (the "IF patient books appointment" section)

#### Current (lines 27–31):
```python
    "IF patient books appointment:\n"
    "→ If patient gives a partial or ambiguous doctor name (e.g. 'Ajit', 'the senior one'), confirm the full name first: 'I believe you mean Dr. Ajit Yadav — is that correct?' — only proceed after confirmation.\n"
    "→ Collect: name, preferred doctor, date/time (ONLY these)\n"
    "→ Use appointment tool\n"
    "→ Confirm booking\n\n"
```

#### New:
```python
    "IF patient books appointment:\n"
    "→ If patient gives a partial or ambiguous doctor name (e.g. 'Ajit', 'the senior one'), confirm the full name first: 'I believe you mean Dr. Ajit Yadav — is that correct?' — only proceed after confirmation.\n"
    "→ Collect: name, preferred doctor, date/time (ONLY these)\n"
    "→ If unclear whether booking for self or someone else, ask once: "
    "'Is this appointment for you, or for a family member?' "
    "If for someone else, capture their name (patient_name), the relation "
    "in their own words (relation_to_booker: 'wife', 'father', 'son', etc.), "
    "and ask for that person's own phone only if they have a separate one "
    "(patient_phone) — otherwise leave it unset.\n"
    "→ Use appointment tool\n"
    "→ Confirm booking\n\n"
```

---

## 🔄 End-to-End Flow Walkthrough

### Scenario: Ravi books for himself, then his wife, then cancels wife's booking

#### Step 1: Ravi Books for Himself

**WhatsApp message:** `"Book appointment with Dr. Ajit Yadav for me"`

**LLM generates tool call:**
```json
{
  "action": "BOOK",
  "doctor_id": "dr-ajit-yadav--chn",
  "department": "General Medicine",
  "patient_name": "Ravi Kumar",
  "relation_to_booker": "self"
}
```

**`core.py:_execute_tool()` builds payload:**
```json
{
  "action": "BOOK",
  "doctor_id": "dr-ajit-yadav--chn",
  "department": "General Medicine",
  "patient_name": "Ravi Kumar",
  "relation_to_booker": "self",
  "hospital_id": "glngs-chn",
  "booker_phone": "9876543210",
  "patient_phone": "9876543210"
}
```

**`booking.py:book()` executes:**
```
1. find_family_member(booker=9876543210, hospital=glngs-chn, name=Ravi Kumar, relation=self)
   → Not found (first booking)

2. insert_family_member(booker=9876543210, name=Ravi Kumar, phone=9876543210, relation=self)
   → INSERT INTO patients (...) → patient_id = uuid-001

3. get_or_create_today_session → session for Dr. Ajit today

4. find_active_token(uuid-001, dr-ajit...) → None

5. insert_token → Token #1

6. touch_family_member(uuid-001)

7. Return BookingConfirmation(patient_name="Ravi Kumar", relation="self", token=1)
```

**Database:**
```
patients:
└─ uuid-001: Ravi Kumar | phone: 9876543210 | booked_by: 9876543210 | relation: self
```

---

#### Step 2: Ravi Books for His Wife (Same Phone)

**WhatsApp message:** `"Also book for my wife Priya"`

**LLM generates tool call:**
```json
{
  "action": "BOOK",
  "doctor_id": "dr-ajit-yadav--chn",
  "department": "General Medicine",
  "patient_name": "Priya Kumar",
  "relation_to_booker": "wife"
}
```

**`core.py` builds payload:** (no `patient_phone` from LLM → defaults to sender's)
```json
{
  "booker_phone": "9876543210",
  "patient_phone": "9876543210",
  "patient_name": "Priya Kumar",
  "relation_to_booker": "wife",
  ...
}
```

**`booking.py:book()` executes:**
```
1. find_family_member(booker=9876543210, hospital=glngs-chn, name=Priya Kumar, relation=wife)
   → Not found

2. insert_family_member(name=Priya Kumar, phone=9876543210, relation=wife)
   → patient_id = uuid-002  ← DIFFERENT patient row, SAME phone!

3. find_active_token(uuid-002, dr-ajit...) → None (Priya has no token)

4. insert_token → Token #2 for Priya

5. Return BookingConfirmation(patient_name="Priya Kumar", relation="wife", token=2)
```

**Database:**
```
patients:
├─ uuid-001: Ravi Kumar  | phone: 9876543210 | booked_by: 9876543210 | relation: self
└─ uuid-002: Priya Kumar | phone: 9876543210 | booked_by: 9876543210 | relation: wife
                           ↑ Same phone, DIFFERENT patient ✅

tokens:
├─ Token #1 → patient_id = uuid-001 (Ravi)
└─ Token #2 → patient_id = uuid-002 (Priya)
```

---

#### Step 3: Ravi Cancels ONLY Wife's Booking

**WhatsApp message:** `"Cancel my wife's appointment"`

**LLM generates tool call:**
```json
{
  "action": "CANCEL",
  "doctor_id": "dr-ajit-yadav--chn",
  "department": "General Medicine",
  "patient_name": "Priya Kumar",
  "relation_to_booker": "wife"
}
```

**`booking.py:cancel()` executes:**
```
1. find_family_member(booker=9876543210, name=Priya Kumar, relation=wife)
   → Found uuid-002 (Priya)  ← NOT Ravi ✅

2. find_active_token(uuid-002, dr-ajit...) → Token #2

3. cancel_token(token-2)

4. Return CancellationResult(cancelled_for="Priya Kumar")
```

**Ravi's Token #1 is UNTOUCHED ✅**

---

#### Step 4: Ravi Re-books Wife (Reuses Patient Record)

**WhatsApp message:** `"Book Priya again for the same doctor"`

**`booking.py:book()` executes:**
```
1. find_family_member(booker=9876543210, name=Priya Kumar, relation=wife)
   → Found uuid-002 (existing record!)  ← REUSES, no duplicate ✅

2. find_active_token(uuid-002, dr-ajit...) → None (was cancelled)

3. insert_token → Token #3 for Priya

4. touch_family_member(uuid-002)  → updated_at refreshed
```

---

## 📋 Summary of All Changes

### Files to Create (1 file)

| File | Purpose |
|---|---|
| `migrations/0001_add_family_support.sql` | SQL migration — 2 new columns, indexes, unique constraint |

### Files to Modify (5 files)

| File | Lines Changed | What Changes |
|---|---|---|
| `models/appointment.py` | L6–16, L19–27, L30–32 | Add `booker_phone`, `relation_to_booker`, `patient_name` to models |
| `tools/appointment/database.py` | L54–74 (replace) | Replace `find_patient`/`insert_patient` with `find_family_member`/`insert_family_member`/`touch_family_member` |
| `tools/appointment/booking.py` | L48–58, L68–69, L78+, L83–92, L104–107, L115–116 | Use new family-aware DB functions, include name/relation in responses |
| `orchestrator/schemas.py` | L14–34 (add properties) | Expose `relation_to_booker` and `patient_phone` to LLM |
| `orchestrator/core.py` | L155–163, L201–213 | Fix `patient_phone` override bug; add relation to confirmation message |
| `prompts/system.py` | L27–31 | Tell LLM to ask "for you or someone else?" and capture relation |

### Database Changes (1 table)

| Table | Change | Details |
|---|---|---|
| `patients` | ADD COLUMN | `booked_by_phone VARCHAR(20) NOT NULL` |
| `patients` | ADD COLUMN | `relation_to_booker VARCHAR(50) NOT NULL DEFAULT 'self'` |
| `patients` | ADD INDEX | `idx_patients_booked_by_phone (hospital_id, booked_by_phone)` |
| `patients` | ADD INDEX | `idx_patients_family_name (hospital_id, booked_by_phone, LOWER(name))` |
| `patients` | ADD UNIQUE | `uq_patients_family_member (hospital_id, booked_by_phone, LOWER(name))` |
| `patients` | BACKFILL | `UPDATE patients SET booked_by_phone = phone WHERE booked_by_phone IS NULL` |

### What Does NOT Change

- `tools/appointment/__init__.py` — entry point (`handle_request`) is unchanged
- `tools/appointment/database.py` — all other functions unchanged: `get_connection`, `get_hospital`, `get_doctor`, `get_or_create_today_session`, `insert_token`, `count_patients_ahead`, `find_active_token`, `cancel_token`
- `models/session.py` — session models unchanged
- `orchestrator/session.py` — `InMemoryRepository` unchanged
- `orchestrator/llm.py` — LLM adapter unchanged
- All other tools (`kg_retriever`, `query_data`) — unchanged
- `whatsapp.py` — webhook service unchanged
- `interface/` — test interface unchanged

---

## ✅ Implementation Checklist

### Pre-Implementation
- [ ] Backup Supabase database (Settings → Backups)
- [ ] Review this document end-to-end

### Database (Supabase SQL Editor)
- [ ] Run `migrations/0001_add_family_support.sql`
- [ ] Verify columns: `SELECT column_name FROM information_schema.columns WHERE table_name='patients'`
- [ ] Verify index: `SELECT indexname FROM pg_indexes WHERE tablename='patients'`

### Code Changes (in order)
- [ ] Update `models/appointment.py`
- [ ] Update `tools/appointment/database.py`
- [ ] Update `tools/appointment/booking.py`
- [ ] Update `orchestrator/schemas.py`
- [ ] Update `orchestrator/core.py`
- [ ] Update `prompts/system.py`

### Testing
- [ ] Restart uvicorn: `uvicorn interface.app:app --reload --port 8001`
- [ ] Test self-booking (should work exactly as before)
- [ ] Test family booking ("Book for my wife Priya")
- [ ] Test cancel specific family member ("Cancel my wife's appointment")
- [ ] Test re-booking same person (should reuse patient record)
- [ ] Verify in Supabase: `SELECT name, phone, booked_by_phone, relation_to_booker FROM patients`

---

*Document generated 2026-08-25. Maps to commit `9ff509f` (post-merge with origin/main).*
