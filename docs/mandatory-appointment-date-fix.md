# Mandatory Appointment Date — Implementation Plan

**Problem:** `date` is optional everywhere in the booking path today — if the patient never mentions one, the system silently defaults to `CURRENT_DATE` (`tools/appointment/database.py`'s `get_or_create_today_session`/`find_active_token`) without ever asking. A patient could end up booked for "today" when they meant some other day, with no chance to correct it.

**Fix:** Same three-layer pattern used for every other fix in this codebase — prompt makes the LLM *ask*, schema makes the field *mandatory*, backend makes it *impossible to book with no date or a fake one*, regardless of whether the LLM complies.

**Scope:** Only the `BOOK` action. `CANCEL` is untouched — defaulting to "today's" existing booking when cancelling is reasonable; this fix is specifically about not assuming a date when *creating* a new booking.

**Files touched:** 3 existing files, no new files, no schema/DB changes, no new imports (`date` is already imported in `booking.py`).

---

## 1. `orchestrator/schemas.py` — make `date` a required tool argument

Line 122, inside `_appointment_schema`:

```python
# before
"required": ["action", "doctor_id", "department", "patient_name"],

# after
"required": ["action", "doctor_id", "department", "patient_name", "date"],
```

Forces the LLM to populate `date` in every `appointment` tool call. This alone doesn't guarantee a *real* date or that the patient was actually asked — that's what layers 2 and 3 are for.

---

## 2. `prompts/system.py` — tell the LLM to always ask, never guess, and how to react to errors

Line 39, inside the `"IF patient books appointment:"` block:

```python
# before
"→ Collect: name, preferred doctor, date/time (ONLY these)\n"

# after
"→ Collect: name, preferred doctor, date (ONLY these). ALWAYS ask which date "
"they want — 'today', 'tomorrow', a specific date, or a specific weekday all "
"count; convert whatever they say into YYYY-MM-DD yourself using today's date "
"above. Never assume today without asking. If what they say is vague (e.g. "
"'sometime next week', 'soon'), don't guess a date — ask them to be specific.\n"
```

Add two more lines in the same block, alongside the existing `INVALID_PHONE`/`NAME_MISMATCH` error-handling instructions:

```python
"If the appointment tool returns error_code DATE_REQUIRED, ask the patient which "
"date they want before calling the tool again.\n"
"If it returns error_code INVALID_DATE, that date doesn't exist on the calendar — "
"ask the patient to confirm the correct date.\n"
```

This is the layer that actually produces the *asking* and *judgment* behavior — nothing here is a fixed list of phrases; the LLM resolves whatever the patient says using the date already injected into its system prompt (`f"\n\nToday's date is {date.today().isoformat()}."`, already present in `orchestrator/core.py`).

---

## 3. `tools/appointment/booking.py` — the deterministic backstop, inside `book()` only

Insert right after the doctor-not-found check (before the phone/patient-resolution logic). Two checks, not one — presence *and* validity, reusing `date` from the existing `from datetime import datetime, date, timedelta` import at the top of this file (no new import needed):

```python
if not payload.date or not payload.date.strip():
    return ErrorResult(status="ERROR", error_code="DATE_REQUIRED",
                       message="No appointment date was given.")
try:
    date.fromisoformat(payload.date.strip())
except ValueError:
    return ErrorResult(status="ERROR", error_code="INVALID_DATE",
                       message=f"'{payload.date}' is not a valid calendar date.")
```

`date.fromisoformat` is Python's own built-in ISO-8601 parser — it already rejects malformed strings *and* real-but-impossible dates (`"2026-02-30"` raises `ValueError: day is out of range for month` since February never has 30 days), with zero hardcoded date logic of any kind. This closes the gap the review flagged: `if not payload.date` alone only checked "is something present," not "is this an actual date."

`cancel()` is not touched — it keeps defaulting to today when no date is given.

---

## Why this shape

- **No hardcoding anywhere.** No fixed list of acceptable phrasings, no manual date-string parsing, no calendar math written by hand — natural-language resolution is entirely the LLM's job (guided by the prompt), and format/calendar validity is entirely `date.fromisoformat`'s job (a standard library function, not bespoke logic).
- **Each layer catches a different failure**, per the review: prompt failing to ask → schema still requires the field → LLM filling in a lazy default anyway → backend rejects a missing date → LLM hallucinating a malformed or impossible date → backend rejects that too via `INVALID_DATE`.
- **Ambiguous dates are a prompt problem, not a backend one.** By the time a call reaches `book()`, the date must already be a concrete `YYYY-MM-DD` — there's nothing for code to validate about "sometime next week" because that should never arrive at the tool at all; the prompt instruction is what stops the LLM from guessing in the first place.
- **Reuses the existing pattern exactly** — same `ErrorResult(status="ERROR", error_code=..., message=...)` shape as `HOSPITAL_NOT_FOUND`, `DOCTOR_NOT_FOUND`, `DUPLICATE_BOOKING`, `INVALID_PHONE`, `NAME_MISMATCH`. Plain factual messages, no scripted replies — the LLM phrases the actual question dynamically, in whatever language the patient is using.

---

## Test matrix

| Scenario | Expected |
|---|---|
| Book, no date mentioned | ❌ `DATE_REQUIRED` |
| Book, "today" | ✅ books today |
| Book, "tomorrow" | ✅ books tomorrow's date |
| Book, explicit date ("on September 10") | ✅ books the requested date |
| Book, ambiguous ("sometime next week") | ❌ LLM asks for a specific date, tool not called |
| Book, impossible date ("February 30") | ❌ `INVALID_DATE`, no token created |
| Direct `book()` call with `date=None` | ❌ `DATE_REQUIRED` |
| Direct `book()` call with `date="2026-13-40"` | ❌ `INVALID_DATE` |
| Cancel, no date mentioned | ✅ unaffected, existing today-default behavior |
| Cancel, explicit date | ✅ unaffected |
| Existing duplicate booking, no date given | ❌ `DATE_REQUIRED` (fires before the duplicate check even runs) |
| Existing duplicate booking, valid date given | ❌ `DUPLICATE_BOOKING` (confirms the new check doesn't shadow the existing one) |
