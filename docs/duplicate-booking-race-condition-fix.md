# Duplicate-Booking Race Condition — Implementation Guide

**Problem:** Two near-simultaneous booking requests (double-tap, WhatsApp webhook retry, two k8s pods) for the same patient + doctor + day can both pass the existing `find_active_token()` pre-check before either `INSERT` commits, creating two `WAITING` tokens. Verified live against the real DB: no constraint currently prevents this.

**Fix:** Add a database-level partial unique index as a safety net behind the existing check, and translate its violation into the same `DUPLICATE_BOOKING` error the app already returns.

**Files touched:** 1 new migration file, 1 existing file edited (`tools/appointment/booking.py`). Nothing else changes — `database.py`, `models/appointment.py`, `orchestrator/*` are untouched.

---

## 1. Database — new migration file

**New file:** `migrations/0002_add_unique_waiting_token_index.sql`

```sql
-- Prevents two WAITING tokens for the same patient in the same doctor
-- session (session_id already encodes hospital+doctor+date, since
-- doctor_sessions has UNIQUE(hospital_id, doctor_id, date)).
CREATE UNIQUE INDEX IF NOT EXISTS uq_tokens_waiting_patient_session
ON tokens (patient_id, session_id)
WHERE status = 'WAITING';
```

No new column, no data migration — verified live that zero existing rows violate this today, so it applies cleanly.

Run it once against the DB:
```
psql "$DATABASE_URL" -f migrations/0002_add_unique_waiting_token_index.sql
```
(or however your existing migrations are applied — `migrations/0001_add_family_support.sql` is the precedent for this pattern in the repo.)

---

## 2. `tools/appointment/booking.py` — the only code change

### 2a. Add one import at the top (after line 3)

```python
from datetime import datetime, date, timedelta
import tools.appointment.database as db
from models.appointment import BookingConfirmation, CancellationResult, ErrorResult
import psycopg2.errors          # ← new
```

### 2b. Replace the token-creation block inside `book()` (currently lines 83-91)

**Before:**
```python
    # Create token
    token = db.insert_token(
        conn,
        session_id=session["session_id"],
        patient_id=patient["patient_id"],
        doctor_id=payload.doctor_id,
        hospital_id=payload.hospital_id,
        department=payload.department,
    )
```

**After:**
```python
    # Create token — the SELECT-then-INSERT above has a race window between
    # two near-simultaneous requests; the DB's unique index is the backstop.
    with conn.cursor() as cur:
        cur.execute("SAVEPOINT before_insert_token")
    try:
        token = db.insert_token(
            conn,
            session_id=session["session_id"],
            patient_id=patient["patient_id"],
            doctor_id=payload.doctor_id,
            hospital_id=payload.hospital_id,
            department=payload.department,
        )
    except psycopg2.errors.UniqueViolation as e:
        if e.diag.constraint_name != "uq_tokens_waiting_patient_session":
            raise
        with conn.cursor() as cur:
            cur.execute("ROLLBACK TO SAVEPOINT before_insert_token")
        return ErrorResult(
            status="ERROR", error_code="DUPLICATE_BOOKING",
            message=f"{patient['name']} already has an active appointment with this doctor today.",
        )
```

That's it — nothing else in `book()`, `cancel()`, or any other function changes. `insert_token()` in `database.py` is called exactly as before; only the caller now wraps it in a savepoint and translates one specific error.

---

## 3. Why this shape (no hardcoding, fully dynamic, reuses what's there)

- **No hardcoded doctor/patient/date values anywhere.** The index is defined purely in terms of columns (`patient_id`, `session_id`, `status`) — it applies to every patient, every doctor, every day, automatically, because `session_id` already carries the doctor+date identity via `doctor_sessions`'s own `UNIQUE(hospital_id, doctor_id, date)` constraint. No new column, no doctor-specific logic.
- **One error path, not two.** Both the normal case (`find_active_token()` catches it before insert — unchanged, still lines 74-81) and the race case (DB catches it) return the *exact same* `ErrorResult(status="ERROR", error_code="DUPLICATE_BOOKING", ...)` shape that's already used 4 times elsewhere in this file. The patient always sees the same message regardless of which layer caught it.
- **The constraint-name check (`if e.diag.constraint_name != "uq_tokens_waiting_patient_session": raise`) is deliberate**, not paranoia — it stops the code from mislabeling an unrelated `UniqueViolation` (if one is ever added elsewhere on `tokens`) as a duplicate booking. Anything else re-raises normally and falls through to the existing generic handler in `orchestrator/core.py`.
- **The `SAVEPOINT` is what keeps this safe**, not optional polish: `book()` shares one transaction with the rest of `handle_request()`. Verified live that catching the violation *without* rolling back and then letting the outer `commit()` run silently discards the *whole* transaction — including a brand-new patient row from `insert_family_member()` earlier in the same call, if this happened to be that family member's first-ever booking. The `SAVEPOINT`/`ROLLBACK TO SAVEPOINT` undoes only the failed token insert, leaving everything else in the transaction intact to commit normally.
- **Nothing new to maintain.** `database.py`'s `insert_token()` function is untouched — it doesn't need to know about duplicate handling at all, that's `book()`'s job, consistent with how the file already separates "database operations" from "business logic."

## 4. Verification checklist

- [ ] Run the migration; confirm `\d tokens` in psql shows the new index.
- [ ] Book normally — should behave exactly as before (pre-check still catches the common case, index never even triggers).
- [ ] Cancel a booking, then re-book the same doctor same day — should succeed (index only applies to `status = 'WAITING'`).
- [ ] Simulate the race: two near-simultaneous `book()` calls for the same patient+doctor+day (e.g. two threads, or fire the same WhatsApp message twice fast) — exactly one token should be created, the other should get the same `DUPLICATE_BOOKING` message a normal duplicate would.
- [ ] Confirm a first-time family member booking that hits the race still ends up with their `patients` row intact after the failed attempt (not wiped by the savepoint fix).
