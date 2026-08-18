# Lab Test & Scan Reminder Service — Design Spec

**Date:** 2026-08-17 (revised 2026-08-18)
**Scope:** Backend LLD only — no UI/channel integration
**Status:** Approved

---

## 1. Overview

The Lab Test & Scan Reminder Service sends a single reminder per patient grouping all their due lab tests and scans into one WhatsApp message. Test order data is managed directly in the DB from the backend — no external order system or registration flow. The service exposes a single `evaluate(now)` method that groups due orders by patient and sends one reminder per patient per day.

State is persisted in a SQL database via a `Repository` layer.

The service is driven by an external caller invoking `evaluate(now)` on a configurable schedule. It holds no internal timer threads.

---

## 2. Architecture

```
┌─────────────────────────────────────────────────────────┐
│               LabScanReminderService                     │
│                                                         │
│  + evaluate(now: datetime) → None          ← tick       │
│                                                         │
│  [ notifier injected at construction ]                  │
└──────────────┬──────────────────────────────────────────┘
               │ uses
         ┌─────┴──────────────┐
         ▼                    ▼
  ReminderNotifier        Repository
  (external — WhatsApp)   (SQL DB — test_orders
                            + reminder_records)
```

**Components:**

- `ReminderNotifier` — sends reminder messages to patients via WhatsApp. `InMemoryReminderNotifier` with `should_fail` flag for tests.
- `Repository` — reads `test_orders` and writes `reminder_records` to SQL DB.

`LabScanReminderService` holds no mutable state itself.

---

## 3. Data Models

### 3.1 DB Tables

```
test_orders                              -- populated and managed by backend
  order_id           UUID        PK
  patient_id         UUID
  hospital_id        UUID
  test_name          str               e.g. "CBC", "MRI Brain"
  test_type          Enum              LAB | SCAN
  due_date           date              patient must complete by this date
  preparation_notes  str               e.g. "Fast for 8 hours before the test"
  remind_hours_before int              hours before due_date to fire; default 24

reminder_records                         -- written by the service; one per patient per day
  reminder_id        UUID        PK
  patient_id         UUID
  reminder_date      date              the calendar day this reminder covers
  status             Enum        SENT | FAILED
  fired_at           datetime
```

**Idempotency key:** `(patient_id, reminder_date)` — one reminder per patient per day. `evaluate()` skips a patient if a row already exists for today.

---

## 4. Core Flow

### 4.1 evaluate(now: datetime)

```
1. Fetch due orders:
   SELECT * FROM test_orders
   WHERE (due_date - remind_hours_before * interval '1 hour') <= now

2. Group by patient_id

3. For each patient_id group:

     a. Check no existing reminder_records row for (patient_id, now.date())
        → skip if already sent today (idempotency)

     b. status = 'SENT'
        try:
          ReminderNotifier.send_reminder(
            patient_id = patient_id,
            orders     = [{ test_name, due_date, prep_notes } for each order in group]
          )
        except Exception:
          status = 'FAILED'

     c. INSERT INTO reminder_records(patient_id, reminder_date = now.date(),
                                     status, fired_at = now)
```

---

## 5. Error Cases

| Scenario | Error Code | Behaviour |
|---|---|---|
| `ReminderNotifier.send_reminder` raises | _(no raise)_ | `reminder_records.status = FAILED`; `evaluate()` continues to next order |

---

## 6. Design Patterns

| Pattern | Application |
|---|---|
| DB-managed data | Test orders created and managed by backend; service reads them directly — no adapter or registration flow |
| Evaluate-on-tick | `evaluate(now)` called externally; time injected for deterministic tests |
| Idempotency | `(patient_id, reminder_date)` existence check prevents double-send on repeated ticks |
| Fire-and-forget | Reminders best-effort; FAILED rows logged but not retried |
