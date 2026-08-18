# Lab Test & Scan Reminder Service — Design Spec

**Date:** 2026-08-17 (revised 2026-08-18)
**Scope:** Backend LLD only — no UI/channel integration
**Status:** Approved

---

## 1. Overview

The Lab Test & Scan Reminder Service sends a single reminder to a patient a configurable number of hours before a lab test or scan is due. Test order data is managed directly in the DB from the backend — no external order system or registration flow. The service exposes a single `evaluate(now)` method that fires reminders for all due orders not yet sent.

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

reminder_records                         -- written by the service
  reminder_id        UUID        PK
  order_id           UUID        FK → test_orders
  patient_id         UUID
  status             Enum        SENT | FAILED
  fired_at           datetime
```

**Idempotency key:** `order_id` — one `reminder_records` row per order. `evaluate()` skips an order if a row already exists.

---

## 4. Core Flow

### 4.1 evaluate(now: datetime)

```
SELECT * FROM test_orders
WHERE (due_date - remind_hours_before * interval '1 hour') <= now

For each test_order:

  1. Check no existing reminder_records row for order_id
     → skip if already fired (idempotency)

  2. status = 'SENT'
     try:
       ReminderNotifier.send_reminder(
         patient_id    = order.patient_id,
         test_name     = order.test_name,
         due_date      = order.due_date,
         prep_notes    = order.preparation_notes
       )
     except Exception:
       status = 'FAILED'

  3. INSERT INTO reminder_records(order_id, patient_id, status, fired_at = now)
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
| Idempotency | `order_id` existence check in `reminder_records` prevents double-send on repeated ticks |
| Fire-and-forget | Reminders best-effort; FAILED rows logged but not retried |
