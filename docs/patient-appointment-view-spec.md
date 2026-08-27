# Patient Appointment View — Implementation Spec

**Status:** Ready to implement
**Scope:** Let a patient ask "what are my appointments?" and get a real answer; let CANCEL find the doctor itself instead of asking the patient to recall it.
**New files:** none
**New DB tables/columns:** none (reuses `requested_by_phone` / `relation_to_requester` added by the family-memory feature)

---

## 1. Problem

Patients have two tools: `appointment` (BOOK/CANCEL only, no "view" action) and `kg_retriever` (finds doctors, not bookings). The only tool that reads booking data, `query_data`, is restricted to `Role.DOCTOR` (`orchestrator/schemas.py`, `ROLE_PERMISSIONS`). Result:

- "What are my appointments?" → bot has no way to answer, asks for name and offers to book/cancel instead.
- "Cancel my appointment" → `cancel()` requires `doctor_id` to find the right token (`tools/appointment/booking.py:134`), so the bot interrogates the patient for the doctor's name instead of looking it up.

## 2. Design

Add one new **read-only** tool, `list_appointments`, kept separate from the `appointment` tool.

**Why separate, not a third `action` on `appointment`:** `orchestrator/core.py:163` forces a YES/NO confirmation step for every call to the tool literally named `"appointment"`, regardless of action. A LIST action on that same tool would get an unwanted "Please confirm: LIST appointment..." prompt. A separate tool name skips that gate with no changes to the gating logic, and avoids reshaping `IncomingPayload` (whose `doctor_id`/`department`/`patient_name` are required fields — the wrong shape for a listing call).

No new formatter needed: the orchestrator already lets the LLM turn a JSON tool result into prose for `kg_retriever` / `query_data` results (only `appointment`/BOOK gets a special WhatsApp-card formatter). `list_appointments`'s JSON result flows through the same generic path.

## 3. Files touched (all existing — no new files)

| # | File | Change |
|---|------|--------|
| 1 | `tools/appointment/database.py` | add `list_active_appointments()` |
| 2 | `tools/appointment/__init__.py` | add `list_appointments()` |
| 3 | `orchestrator/schemas.py` | add tool schema, register in tool lists + `ROLE_PERMISSIONS` |
| 4 | `orchestrator/core.py` | add one `elif` branch in `_execute_tool` |
| 5 | `prompts/system.py` | update CANCEL flow line, add a new "view appointments" flow line |

---

## 4. Exact changes

### 4.1 `tools/appointment/database.py`

Add after `find_active_token` (currently ends at line 205, right before `cancel_token`):

```python
def list_active_appointments(conn, requester_phone, hospital_id, patient_name=None):
    """All WAITING tokens booked by this requester, across their whole family."""
    conditions = ["p.requested_by_phone = %s", "p.hospital_id = %s", "t.status = 'WAITING'"]
    params = [requester_phone, str(hospital_id)]
    if patient_name:
        conditions.append("LOWER(p.name) = LOWER(%s)")
        params.append(patient_name.strip())

    sql = f"""
        SELECT p.name AS patient_name, p.relation_to_requester,
               d.doctor_id, d.name AS doctor_name, t.department,
               t.token_number, ds.date
        FROM tokens t
        JOIN patients p        ON t.patient_id = p.patient_id
        JOIN doctors d         ON t.doctor_id  = d.doctor_id
        JOIN doctor_sessions ds ON t.session_id = ds.session_id
        WHERE {" AND ".join(conditions)}
        ORDER BY ds.date ASC, t.token_number ASC
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, tuple(params))
        return cur.fetchall()
```

Same cursor/query pattern already used by every other function in this file — no new imports needed.

### 4.2 `tools/appointment/__init__.py`

Current file (24 lines):

```python
from models.appointment import IncomingPayload, BookingResponse, ErrorResult
from tools.appointment import booking, database


def handle_request(payload_dict: dict) -> dict:
    try:
        payload = IncomingPayload(**payload_dict)
    except Exception as e:
        return BookingResponse(
            action=payload_dict.get("action", "BOOK"),
            result=ErrorResult(
                status="ERROR",
                error_code="INVALID_PAYLOAD",
                message=f"Invalid payload: {e}",
            ),
        ).model_dump(mode="json")

    with database.get_connection() as conn:
        if payload.action == "BOOK":
            result = booking.book(conn, payload)
        else:
            result = booking.cancel(conn, payload)

    return BookingResponse(action=payload.action, result=result).model_dump(mode="json")
```

Add this function at the end (after line 24):

```python
def list_appointments(hospital_id: str, requester_phone: str, patient_name: str | None = None) -> dict:
    with database.get_connection() as conn:
        rows = database.list_active_appointments(conn, requester_phone, hospital_id, patient_name)
    return {"appointments": rows}
```

### 4.3 `orchestrator/schemas.py`

Insert a new schema block after `_appointment_schema` (which currently ends at line 41):

```python
_list_appointments_schema = {
    "type": "function",
    "function": {
        "name": "list_appointments",
        "description": (
            "List the patient's active/upcoming bookings (across their whole family, "
            "since one WhatsApp number can book for several people). Use this whenever "
            "asked 'what are my appointments' — also use it BEFORE a cancel request, to "
            "find the doctor_id yourself instead of asking the patient to recall it."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "patient_name": {"type": "string",
                    "description": "Optional — only set this to filter to one specific family member, e.g. 'just my wife's appointment'."},
            },
            "required": [],
        },
    },
}
```

Replace lines 76-84 (the tool lists and `ROLE_PERMISSIONS`):

```python
PATIENT_TOOLS        = [_appointment_schema, _list_appointments_schema, _kg_retriever_schema]
PATIENT_TOOLS_WARMUP = [_list_appointments_schema, _kg_retriever_schema]   # appointment stripped, listing isn't
DOCTOR_TOOLS         = [_kg_retriever_schema, _query_data_schema]

ROLE_PERMISSIONS = {
    "appointment":       {Role.PATIENT},
    "list_appointments": {Role.PATIENT},
    "query_data":        {Role.DOCTOR},
    "kg_retriever":       {Role.PATIENT, Role.DOCTOR},
}
```

`list_appointments` is read-only with no side effects, so it's safe to expose even during warmup turns — unlike `appointment`, it doesn't need to be gated behind booking-intent detection.

### 4.4 `orchestrator/core.py`

In `_execute_tool` (starts at line 167), add a new branch after the `appointment` branch (after line 177, before the `kg_retriever` `elif` at line 178):

```python
        elif tool_call.tool_name == "list_appointments":
            from tools.appointment import list_appointments
            return list_appointments(
                hospital_id=context.wa_message.hospital_id,
                requester_phone=context.wa_message.from_number,
                patient_name=tool_call.args.get("patient_name"),
            )
```

No other changes to `core.py` — `_gate` (line 159-165) only special-cases the tool literally named `"appointment"`, so `list_appointments` already returns `GateStatus.OK` with zero changes there.

### 4.5 `prompts/system.py`

Replace lines 40-41:

```python
    "IF patient cancels/reschedules:\n"
    "→ Proceed directly (no name/symptoms needed)\n\n"
```

with:

```python
    "IF patient cancels/reschedules:\n"
    "→ Call list_appointments FIRST to find their doctor_id — never ask the patient to recall which doctor. "
    "If they have more than one active booking, ask which one (by doctor name).\n\n"
    "IF patient asks about their appointments (e.g. 'what are my appointments', 'my bookings'):\n"
    "→ Call list_appointments. Show each one clearly (patient name if not self, doctor, department, date, token).\n\n"
```

---

## 5. What stays untouched

- `models/appointment.py` — no changes.
- `tools/appointment/booking.py` (`book()`, `cancel()`) — no changes.
- Database schema / migrations — no changes.
- `orchestrator/llm.py`, `interface/app.py`, `whatsapp.py` — no changes.

## 6. Verification checklist

- [ ] "What are my appointments?" as the very first message returns an actual list (or "you have no upcoming appointments"), not a request for the patient's name.
- [ ] A patient with 2+ family members booked returns all of them, each labeled with `relation_to_requester`.
- [ ] "Cancel my appointment" with exactly one active booking cancels it without asking which doctor.
- [ ] "Cancel my appointment" with two+ active bookings asks which doctor, using names pulled from `list_appointments`, not from the patient.
- [ ] A patient with zero bookings gets a clear "nothing to cancel" message instead of a crash or empty confusion.
- [ ] Doctor role (`Role.DOCTOR`) sessions are unaffected — `list_appointments` isn't in `DOCTOR_TOOLS`.
