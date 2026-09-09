# Slot-Based Appointment Booking — End-to-End Implementation Guide

Token-mode booking is done and shipped. This is the equivalent plan for slot-mode — booking, cancel, reschedule, and how it plugs into `memory_tool`/`list_appointments` — using `book()`/`cancel()` as thin dispatchers over mode-specific functions, per the agreed design.

---

## 1. Token vs Slot, side by side

| | **TOKEN mode** (`glngs-chn`) | **SLOT mode** (`mit-lbn`) |
|---|---|---|
| Storage | `doctor_sessions` (one row/doctor/day) + `tokens` (queue entries) | `slots` (pre-generated fixed windows) + `appointments` |
| What's reserved | A queue position | An exact `start_time`–`end_time` window |
| Availability | Always "yes," you get the next number | Only if that specific slot's `status = 'AVAILABLE'` |
| Session creation | `get_or_create_today_session()` creates one on demand | Slots are pre-generated ahead of time (`migrations/0005`-style) — nothing created on the fly |
| Duplicate check | `find_active_token()` — same patient+doctor+day already `WAITING` | Same idea, checks `appointments.status = 'BOOKED'` instead |
| The reservation write | `insert_token()`, `token_number` assigned | `UPDATE slots SET status='BOOKED'` + `INSERT INTO appointments` |
| Race protection | `SAVEPOINT` + `UniqueViolation` on `uq_tokens_waiting_patient_session` | Same pattern, `SELECT ... FOR UPDATE` on the `slots` row |
| Confirmation shows | `token_number` + computed `estimated_time` (queue-position ETA math) | `appointment_id` + the slot's own fixed time — no ETA math, the slot *is* the time |
| Reschedule | **Not supported** — prompt already says cancel + rebook | **Supported directly** — swap `slot_id` in one atomic step |
| Identity/family logic | `find_family_member`/`insert_family_member`, `NAME_MISMATCH` | **Identical, reused as-is** — this doesn't change per mode |

---

## 2. End-to-end message flow (booking)

```
Patient: "Book me with Dr. Mallindra Swamy tomorrow"
      │
      ▼
kg_retriever (unchanged) → resolves doctor_id
      │
      ▼
LLM calls list_available_slots(doctor_id, date)   ◄── NEW tool, slot-mode only
      │
      ▼
Bot shows: "10:00, 10:10, 10:20, 10:30, 10:40"
      │
      ▼
Patient: "10:20"
      │
      ▼
LLM calls appointment tool, action=BOOK, slot_id=<the 10:20 slot's id>
      │
      ▼
orchestrator._execute_tool → tools.appointment.handle_request(payload)
      │
      ▼
booking.book(conn, payload)              ◄── dispatcher (existing function, new top)
      │
      ├── hospital.booking_mode == "TOKEN" → book_token()   (today's book() body, renamed)
      └── hospital.booking_mode == "SLOT"  → book_slot()    ◄── NEW
                │
                ▼
        shared validation (unchanged, reused):
        get_hospital → get_doctor → DATE_REQUIRED/INVALID_DATE →
        INVALID_PHONE → find_family_member/insert_family_member → NAME_MISMATCH
                │
                ▼
        slot-specific:
        lock slot FOR UPDATE → verify AVAILABLE → mark BOOKED →
        insert appointments row → return confirmation
```

---

## 3. Files touched, and exactly what changes

### 3a. `models/appointment.py` — widen the action type, add slot fields

```python
# before
action: Literal["BOOK", "CANCEL"]

# after
action:   Literal["BOOK", "CANCEL", "RESCHEDULE"]
slot_id:  Optional[str] = None   # which slot to book/reschedule into
```

New response model, alongside the existing `BookingConfirmation` (kept as-is for token mode — not reused, since the fields genuinely differ per the comparison table above):
```python
class SlotBookingConfirmation(BaseModel):
    status:               Literal["CONFIRMED"]
    appointment_id:       str
    patient_name:         str
    relation_to_requester: str
    doctor_name:          str
    department:           str
    hospital_name:        str
    slot_date:            str
    slot_time:            str
    fee:                  Optional[float] = None
    was_rescheduled:      bool = False   # book_slot() leaves default; reschedule_slot() sets True —
                                          # so the LLM says "moved to" not "booked for" (§5)
```
`BookingResponse.result`'s `Union` gains `SlotBookingConfirmation`; `BookingResponse.action` gains `"RESCHEDULE"`. New error codes used below: `SLOT_DOCTOR_MISMATCH` (slot belongs to a different doctor than expected — §3e, §5) and `AMBIGUOUS_APPOINTMENT` (reschedule matched more than one active appointment — §5); `ErrorResult` gains an optional `candidates` field to carry the disambiguation list for the latter.

### 3b. `orchestrator/schemas.py` — new tool, updated action enum

New schema, same shape/precedent as `_list_appointments_schema`:
```python
_list_available_slots_schema = {
    "type": "function",
    "function": {
        "name": "list_available_slots",
        "description": "List a doctor's next available time slots on a given date. Slot-mode hospitals only — call this before booking so the patient can pick a time.",
        "parameters": {
            "type": "object",
            "properties": {
                "doctor_id": {"type": "string"},
                "date":      {"type": "string", "description": "YYYY-MM-DD"},
            },
            "required": ["doctor_id", "date"],
        },
    },
}
```
`_appointment_schema`'s `action` enum: `["BOOK", "CANCEL", "RESCHEDULE"]`, plus a `slot_id` property (optional — only present for slot-mode calls). `PATIENT_TOOLS`/`PATIENT_TOOLS_WARMUP` both gain `_list_available_slots_schema` (same reasoning as `list_appointments` — read-only, no reason to gate behind booking-intent detection). `ROLE_PERMISSIONS` gains `"list_available_slots": {Role.PATIENT}`.

### 3c. `orchestrator/core.py` — one new branch in `_execute_tool`

```python
elif name == "list_available_slots":
    from tools.appointment import list_available_slots
    return list_available_slots(
        hospital_id=context.wa_message.hospital_id,
        doctor_id=tool_call.args["doctor_id"],
        date=tool_call.args["date"],
    )
```

### 3d. `tools/appointment/database.py` — new slot-mode functions

```python
def find_available_slots(conn, doctor_id, hospital_id, date, limit=5):
    """Next N AVAILABLE slots for a doctor on a given date — future-only, blocker fix #3."""
    sql = """
        SELECT slot_id, date, start_time, end_time
        FROM slots
        WHERE doctor_id = %s AND hospital_id = %s AND date = %s
          AND status = 'AVAILABLE'
          AND (date > CURRENT_DATE OR (date = CURRENT_DATE AND start_time > CURRENT_TIME))
        ORDER BY start_time
        LIMIT %s
    """
    # ... RealDictCursor, same pattern as every other function in this file ...

def lock_slot(conn, slot_id, hospital_id):
    """SELECT * FROM slots WHERE slot_id=%s AND hospital_id=%s FOR UPDATE — must run
    inside the same transaction as the booking write. The hospital_id filter is the
    tenant boundary: without it, a slot_id from one hospital could be locked/booked
    against a different hospital's request. Chain of checks this enables, cheapest
    first: hospital match (this filter) → doctor match (caller checks slot["doctor_id"],
    §3e/§5) → status == AVAILABLE (caller checks) → book."""
    ...

def mark_slot_booked(conn, slot_id):
    ...

def mark_slot_available(conn, slot_id):
    """UPDATE slots SET status='AVAILABLE' WHERE slot_id=%s AND status='BOOKED' — blocker fix #2.
    The status='BOOKED' guard stops a stale/racing call from freeing a slot that's
    already been cancelled or reassigned by another path; check rowcount==1 and
    treat 0 as a no-op, not an error (the slot may have already been freed)."""
    ...

def insert_appointment(conn, hospital_id, patient_id, doctor_id, slot_id, department, appointment_date):
    ...

def find_active_appointments(conn, patient_id, doctor_id, date=None):
    """Slot-mode equivalent of find_active_token — same shape, queries appointments+slots.
    Renamed from find_active_appointment (singular) to make plain that reschedule
    needs the *set* of matches, not just one — see §5's AMBIGUOUS_APPOINTMENT rule."""
    ...

def cancel_appointment_row(conn, appointment_id):
    ...
```
All following the exact same `RealDictCursor` / parameterized-query pattern already used by every function in this file — no new style introduced.

### 3e. `tools/appointment/booking.py` — the dispatchers

```python
def book(conn, payload):
    hospital = db.get_hospital(conn, payload.hospital_id)
    if not hospital:
        return ErrorResult(status="ERROR", error_code="HOSPITAL_NOT_FOUND", ...)
    if hospital["booking_mode"] == "SLOT":
        return book_slot(conn, payload, hospital)
    return book_token(conn, payload, hospital)     # today's book() body, renamed + hospital passed in


def cancel(conn, payload):
    hospital = db.get_hospital(conn, payload.hospital_id)
    if not hospital:
        return ErrorResult(status="ERROR", error_code="HOSPITAL_NOT_FOUND", ...)
    if hospital["booking_mode"] == "SLOT":
        return cancel_slot(conn, payload, hospital)
    return cancel_token(conn, payload, hospital)


def reschedule(conn, payload):
    """No token-mode equivalent — reschedule is slot-only, matching the
    existing prompt instruction that token-mode has no direct reschedule."""
    hospital = db.get_hospital(conn, payload.hospital_id)
    if not hospital:
        return ErrorResult(status="ERROR", error_code="HOSPITAL_NOT_FOUND", ...)
    if hospital["booking_mode"] != "SLOT":
        return ErrorResult(status="ERROR", error_code="RESCHEDULE_NOT_SUPPORTED",
                           message="This hospital doesn't support direct rescheduling — cancel and book a new appointment instead.")
    return reschedule_slot(conn, payload, hospital)
```

`book_token()`/`cancel_token()` are the current `book()`/`cancel()` bodies, unchanged in logic — just renamed and taking `hospital` as a parameter instead of looking it up again internally (the dispatcher already did that lookup once).

`book_slot()` — the new logic:
```python
def book_slot(conn, payload, hospital):
    doctor = db.get_doctor(conn, payload.doctor_id, payload.hospital_id)
    if not doctor:
        return ErrorResult(status="ERROR", error_code="DOCTOR_NOT_FOUND", ...)

    # same shared checks as book_token(): DATE_REQUIRED/INVALID_DATE, INVALID_PHONE,
    # find_family_member/insert_family_member, NAME_MISMATCH — verbatim reused

    if not payload.slot_id:
        return ErrorResult(status="ERROR", error_code="SLOT_REQUIRED",
                           message="No slot was selected.")

    existing = db.find_active_appointments(conn, patient["patient_id"], payload.doctor_id, payload.date)
    if existing:
        return ErrorResult(status="ERROR", error_code="DUPLICATE_BOOKING", ...)

    with conn.cursor() as cur:
        cur.execute("SAVEPOINT before_book_slot")
    slot = db.lock_slot(conn, payload.slot_id, payload.hospital_id)     # hospital-scoped
    if not slot or slot["status"] != "AVAILABLE":
        with conn.cursor() as cur:
            cur.execute("ROLLBACK TO SAVEPOINT before_book_slot")
        return ErrorResult(status="ERROR", error_code="SLOT_UNAVAILABLE",
                           message="That slot is no longer available.")
    if slot["doctor_id"] != payload.doctor_id:                    # blocker fix #1
        with conn.cursor() as cur:
            cur.execute("ROLLBACK TO SAVEPOINT before_book_slot")
        return ErrorResult(status="ERROR", error_code="SLOT_DOCTOR_MISMATCH",
                           message="That slot doesn't belong to the selected doctor.")

    db.mark_slot_booked(conn, payload.slot_id)
    appt = db.insert_appointment(conn, payload.hospital_id, patient["patient_id"],
                                  payload.doctor_id, payload.slot_id, payload.department, slot["date"])
    db.touch_family_member(conn, patient["patient_id"])

    return SlotBookingConfirmation(
        status="CONFIRMED", appointment_id=str(appt["appointment_id"]),
        patient_name=patient["name"], relation_to_requester=patient["relation_to_requester"],
        doctor_name=doctor["name"], department=payload.department, hospital_name=hospital["name"],
        slot_date=str(slot["date"]), slot_time=str(slot["start_time"]), fee=doctor.get("fee"),
    )
```
Note this reuses `find_family_member`/`insert_family_member`/`_normalize_phone`/the date checks exactly as they exist today — same functions, same imports, zero duplication of the identity/validation logic. The `SAVEPOINT` pattern mirrors `book_token()`'s existing race-condition handling exactly, just guarding a slot-status check instead of a unique-index violation. The doctor-mismatch check exists because `slot_id` and `doctor_id` arrive as two independent LLM-supplied arguments — nothing upstream guarantees they refer to the same doctor, so the DB layer verifies it itself rather than trusting the tool call.

### 3f. `tools/appointment/__init__.py` — dispatch on the new action, expose the new tool entry point

```python
if payload.action == "BOOK":
    result = booking.book(conn, payload)
elif payload.action == "CANCEL":
    result = booking.cancel(conn, payload)
elif payload.action == "RESCHEDULE":
    result = booking.reschedule(conn, payload)
else:
    return ErrorResult(status="ERROR", error_code="UNKNOWN_ACTION", message=f"Unrecognized action: {payload.action}")

def list_available_slots(hospital_id, doctor_id, date):
    with database.get_connection() as conn:
        return {"slots": database.find_available_slots(conn, doctor_id, hospital_id, date)}
```
Explicit `elif "RESCHEDULE"` + a final `else` (not a bare `else` catching `RESCHEDULE`) — keeps the dispatch future-safe if a 4th action is ever added; a bare `else` would silently route an unrecognized action into `reschedule()` instead of failing loudly.

### 3g. `prompts/system.py` — new flow blocks

```python
"IF hospital is SLOT-mode and patient wants to book:\n"
"→ After resolving the doctor, call list_available_slots(doctor_id, date) — show the "
"returned times, ask which one, then call the appointment tool with action=BOOK and "
"the chosen slot_id.\n"
"→ If list_available_slots returns none, tell the patient no slots are open that day "
"and offer to check a different date.\n\n"

"IF hospital is SLOT-mode and patient wants to reschedule:\n"
"→ Call list_available_slots for the new date, show options excluding their current slot, "
"then call the appointment tool with action=RESCHEDULE and the new slot_id — one call, "
"no need to cancel first (unlike TOKEN-mode).\n"
"→ If the result's was_rescheduled is true, phrase the reply as \"moved to <time>\", "
"never \"booked for <time>\" — it's the same appointment, not a new one.\n\n"
```
(The model already knows a hospital's mode implicitly, since `list_available_slots`/`RESCHEDULE` simply won't apply/won't be offered for a `TOKEN` hospital in practice — no explicit "is this hospital SLOT mode" check needs to live in the prompt logic itself, since the tool results themselves will guide behavior; phrased this way mainly for clarity to whoever reads the prompt.)

---

## 4. Cancel — and how the freed slot becomes visible to other patients

```
Patient: "Cancel my appointment with Dr. Mallindra Swamy"
      │
      ▼
LLM calls list_appointments (unchanged) → finds doctor_id, existing booking
      │
      ▼
appointment tool, action=CANCEL
      │
      ▼
booking.cancel() → hospital.booking_mode == SLOT → cancel_slot()
      │
      ├── UPDATE appointments SET status='CANCELLED' WHERE appointment_id=...
      └── UPDATE slots        SET status='AVAILABLE' WHERE slot_id=... AND status='BOOKED'
                (same transaction — both happen or neither does)
```

`cancel_slot()` reads `slot_id` off the **appointment row it's cancelling** (`appointment["slot_id"]`, from `find_active_appointments`/`find_active_appointment` by `appointment_id`), never from a separate LLM-supplied argument — so there's no path where the slot freed isn't the one that was actually booked for that appointment. Same `status='BOOKED'` guard as `mark_slot_available()` elsewhere (blocker fix #2), so a double-cancel or a race with a reschedule can't flip an already-freed or already-reassigned slot.

**How another patient sees it as free — no push, no notification needed.** `list_available_slots` always runs `WHERE status = 'AVAILABLE'` live, at query time. The moment `cancel_slot()` commits, that row's `status` is `AVAILABLE` in the database — the *next* person who calls `list_available_slots` for that doctor/date simply sees it, because they're reading current state, not a cached snapshot. This is exactly the same "pull, not push" pattern the rest of this system already relies on (e.g. `list_appointments` for token bookings) — nothing new architecturally, just confirming it applies here too.

---

## 5. Reschedule — cancel the old slot, show the remaining/other slots, book the new one, in one call

```
Patient: "Move my appointment to Wednesday"
      │
      ▼
LLM calls list_appointments → finds current appointment_id + doctor_id
      │
      ▼
LLM calls list_available_slots(doctor_id, "2026-09-09")
      │
      ▼
Bot shows Wednesday's open slots (naturally excludes anything already BOOKED —
including, incidentally, the patient's own current slot on a DIFFERENT day, since
this query is date-scoped; if rescheduling within the SAME day, their own current
slot simply won't appear in the AVAILABLE list because it's still BOOKED to them
at that point in the flow)
      │
      ▼
Patient picks a new time
      │
      ▼
appointment tool, action=RESCHEDULE, slot_id=<new slot>
      │
      ▼
booking.reschedule() → reschedule_slot():   ◄── all steps below run inside ONE transaction
      │
      ├── find ALL active appointments for patient+doctor (find_active_appointments)
      │     ├── zero  → NO_ACTIVE_BOOKING
      │     ├── one   → proceed (the normal case)
      │     └── 2+    → AMBIGUOUS_APPOINTMENT, list candidates, ask which one   ◄── blocker fix #4
      ├── SAVEPOINT
      ├── lock NEW slot FOR UPDATE, hospital-scoped, verify AVAILABLE           ◄── tenant boundary
      ├── verify NEW slot's doctor_id == current appointment's doctor_id        ◄── defense-in-depth
      ├── mark OLD slot AVAILABLE          ◄── freed, visible to others immediately
      ├── mark NEW slot BOOKED
      └── UPDATE appointments SET slot_id=<new>, appointment_date=<new date> WHERE appointment_id=...
                (swap in place — NOT cancel+recreate, this is what makes slot-mode
                reschedule "clean" versus token-mode's cancel+rebook)
```

**Transaction boundary, explicit.** The four writes above (free old slot, book new slot, update appointment, plus the row-lock itself) must commit as one atomic unit — same connection, same transaction, `SAVEPOINT`/`ROLLBACK TO SAVEPOINT` for the early-exit validation failures, and the *caller* (`tools/appointment/__init__.py`'s `handle_request`, which already opens the connection/transaction for `book()`/`cancel()` today) does the final `COMMIT`/`ROLLBACK`. If the appointment-row update fails after the slot flips have already run in-transaction, the whole transaction rolls back and *both* slot-status changes are undone with it — Postgres guarantees this as long as nothing between the first write and the final commit escapes the transaction (no autocommit, no separate connection). This is exactly the failure mode to avoid: old slot freed + new slot booked + appointment row never updated, which would silently lose the patient's appointment. `reschedule_slot()` must not open or commit its own transaction — it runs inside whatever transaction `book()`/`cancel()`/`reschedule()` already share via `handle_request`.

**Blocker fix #4, why it's the primary safety mechanism (not the doctor check):** a patient can have more than one active appointment with the *same* doctor (e.g. two family members both booked, or a re-booked slot on a different day) — same-doctor validation alone doesn't disambiguate which one they mean to move. `find_active_appointments` must therefore return the full set, and `reschedule_slot()` must require **exactly one** match before touching any row; two or more is a hard stop with a new error code, `AMBIGUOUS_APPOINTMENT`, carrying the candidate list (date/time per appointment) so the LLM can ask the patient to pick one and retry with an explicit `appointment_id`. The doctor-mismatch check on the new slot (mirroring `book_slot()`'s) stays in as a second, independent guard — it catches a different failure mode (wrong `slot_id` argument), not the ambiguity case.

`reschedule_slot()` sketch:
```python
def reschedule_slot(conn, payload, hospital):
    doctor  = db.get_doctor(conn, payload.doctor_id, payload.hospital_id)
    patient = db.find_family_member(conn, payload.requester_phone, payload.hospital_id,
                                     payload.patient_name, payload.relation_to_requester)
    if not patient:
        return CancellationResult(status="PATIENT_NOT_FOUND", ...)

    matches = db.find_active_appointments(conn, patient["patient_id"], payload.doctor_id)
    if not matches:
        return CancellationResult(status="NO_ACTIVE_BOOKING", ...)
    if len(matches) > 1:
        return ErrorResult(status="ERROR", error_code="AMBIGUOUS_APPOINTMENT",
                           candidates=[{"appointment_id": m["appointment_id"],
                                        "date": str(m["date"]), "time": str(m["start_time"])} for m in matches],
                           message="You have more than one active appointment with this doctor — which one?")
    current = matches[0]

    with conn.cursor() as cur:
        cur.execute("SAVEPOINT before_reschedule")
    new_slot = db.lock_slot(conn, payload.slot_id, payload.hospital_id)     # hospital-scoped
    if not new_slot or new_slot["status"] != "AVAILABLE":
        with conn.cursor() as cur:
            cur.execute("ROLLBACK TO SAVEPOINT before_reschedule")
        return ErrorResult(status="ERROR", error_code="SLOT_UNAVAILABLE", ...)
    if new_slot["doctor_id"] != current["doctor_id"]:              # defense-in-depth, see above
        with conn.cursor() as cur:
            cur.execute("ROLLBACK TO SAVEPOINT before_reschedule")
        return ErrorResult(status="ERROR", error_code="SLOT_DOCTOR_MISMATCH", ...)

    db.mark_slot_available(conn, current["slot_id"])
    db.mark_slot_booked(conn, payload.slot_id)
    db.update_appointment_slot(conn, current["appointment_id"], payload.slot_id, new_slot["date"])

    return SlotBookingConfirmation(status="CONFIRMED", appointment_id=str(current["appointment_id"]),
                                    ..., slot_date=str(new_slot["date"]), slot_time=str(new_slot["start_time"]),
                                    was_rescheduled=True)
```
Same identity-resolution reuse as `book_slot()`, same `SAVEPOINT` pattern, one new small DB function (`update_appointment_slot`). `was_rescheduled=True` here (vs. the default `False` from `book_slot()`) is the unambiguous signal §3g's prompt block uses to phrase the reply as "moved to" rather than "booked for" — no reliance on the LLM inferring intent from which tool was called.

---

## 6. `memory_tool` and `list_appointments` — the gap that needs closing

Checked the actual current queries: **both are token-mode only today.**

- `tools/memory_tool.py`'s `_fetch_raw()` JOINs `patients` → `tokens` → `doctor_sessions` → `doctors`. A patient with a SLOT-mode booking would show up in the patient/family part of the context, but their upcoming appointment simply wouldn't appear — the query never touches `appointments`/`slots` at all.
- `tools/appointment/database.py`'s `list_active_appointments()` (backing the `list_appointments` tool) has the exact same gap.

**Fix for both:** add a second query shaped like the first but joining `appointments`/`slots` instead of `tokens`/`doctor_sessions`, and `UNION ALL` the two result sets before returning. Column shapes need light reconciling — slot-mode rows have no `token_number` (there's no queue position), so that column is `NULL` for slot-mode rows in the union; `token_status` maps to `appointments.status` (`SCHEDULED`/`CANCELLED`) instead of `tokens.status` (`WAITING`/etc.) — the calling code (`_build_output()` in `memory_tool.py`, the formatting in `list_appointments`) already treats these as opaque display strings, so this doesn't need special-casing beyond the query itself.

**Before writing the `UNION ALL`, verify against the actual current query** (don't assume compatibility): same column count, same column order, matching Postgres types per column (e.g. `time` vs `text` for the time field), and explicit `NULL AS token_number` (typed, not a bare `NULL`, which Postgres can reject in a `UNION` without a cast) on the slot-mode side. This is implementation-time verification against real code, same discipline as everywhere else in this spec — not a design change.

This matters concretely: once a hospital like `mit-lbn` is live, a patient asking "what are my appointments" or the bot's own memory-preload on session start needs to see slot bookings too, not just token ones — otherwise the bot would look like it "forgot" about a real, active appointment.

---

## 7. Deferred to code review — not blockers

Two items left, kept out of the design above deliberately since neither blocks a correct first implementation (the third item from the previous round — booking vs. reschedule reading identically to the LLM — is now resolved by `was_rescheduled`, §3a/§3g):

- **`RESCHEDULE_NOT_SUPPORTED` message wording** (§3e's `reschedule()` dispatcher, token-mode branch). The current message is fine as an error string; double-check at implementation time that the LLM-facing phrasing matches how the prompt already tells token-mode patients to cancel + rebook (§1's table), so the two don't contradict each other.
- **`end_time` exposure timing.** `find_available_slots` (§3d) already selects `end_time` — it's available in the DB layer now. Whether to surface it to the patient (e.g. "10:20–10:30") or keep showing just the start time is a prompt/UX choice, not a schema gap — no code change needed either way, just a decision at prompt-writing time.

---

## 8. Summary of every file touched

| File | Change |
|---|---|
| `models/appointment.py` | Widen `action` enum, add `slot_id`, add `SlotBookingConfirmation` |
| `orchestrator/schemas.py` | New `list_available_slots` tool, `action` enum + `slot_id` on `appointment`, `ROLE_PERMISSIONS` entry |
| `orchestrator/core.py` | One new branch in `_execute_tool` |
| `tools/appointment/database.py` | 7 new functions (slots CRUD + `find_active_appointments`, plural — returns all matches so reschedule can detect ambiguity), `UNION` extension to `list_active_appointments` |
| `tools/appointment/booking.py` | `book()`/`cancel()` become dispatchers; `book_token()`/`cancel_token()` = today's logic renamed; `book_slot()`/`cancel_slot()`/`reschedule_slot()` new |
| `tools/appointment/__init__.py` | Dispatch `RESCHEDULE`, expose `list_available_slots()` |
| `tools/memory_tool.py` | `UNION` extension to `_fetch_raw()` |
| `prompts/system.py` | 2 new flow blocks (slot booking, slot reschedule) |

No changes to: `interface/app.py`, `whatsapp.py`, the identity/phone/date validation logic itself, or anything token-mode-specific — all of that is reused exactly as it exists today.

---

## 9. Implementation order

Models → schemas → DB functions → booking dispatchers → `book_slot` → `cancel_slot` → `reschedule_slot` → `list_appointments`/`memory_tool` union → prompts → tests. Each stage is usable/testable before the next starts (e.g. the DB functions can be exercised directly against `mit-lbn`'s live slots before any orchestrator wiring exists). `book_token()`/`cancel_token()` are a rename-and-extract of the current `book()`/`cancel()` bodies — their logic does not change; a diff on those two functions should show line movement, not line changes.

Every function above takes `hospital_id`/`doctor_id`/`slot_id`/`date` as arguments sourced from the payload or DB lookups — none of the code in §3–§5 hardcodes a specific hospital, doctor, or slot anywhere. The only hardcoded values in this whole feature are one-time data (a hospital's shift hours, seeded via migration — not app code, see `migrations/0004`/`0005`) and the `config/doctors.json` roster, both of which are inherent onboarding data, not logic.
