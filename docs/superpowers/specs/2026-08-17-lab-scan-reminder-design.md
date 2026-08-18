# Lab Test & Scan Reminder Service — Design Spec

**Date:** 2026-08-17 (revised 2026-08-18)
**Scope:** Backend LLD only — no UI/channel integration
**Status:** Approved

---

## 1. Overview

The Lab Test & Scan Reminder Service sends a single reminder to a patient a configurable number of hours before a lab test or scan is due. It fires once per order and records whether the send succeeded or failed.

All external dependencies (LIS/RIS order system, WhatsApp notifier) are behind swappable adapter interfaces. State is persisted in a SQL database via a `Repository` layer.

The service is driven by an external caller invoking `evaluate(now)` on a configurable schedule. It holds no internal timer threads.

---

## 2. Architecture

```
┌─────────────────────────────────────────────────────────┐
│               LabScanReminderService                     │
│                                                         │
│  + register_order(order_id) → ReminderSchedule          │
│  + evaluate(now: datetime) → None          ← tick       │
│                                                         │
│  [ adapters injected at construction time ]             │
└──────────────┬──────────────────────────────────────────┘
               │ uses
    ┌──────────┼──────────────┐
    ▼          ▼              ▼
TestOrderProvider   ReminderNotifier   Repository
(external — LIS/RIS) (external —       (SQL DB)
                      WhatsApp)
```

**Components:**

- `TestOrderProvider` — fetches `TestOrder` by `order_id` from LIS/RIS. `InMemoryTestOrderProvider` returns hardcoded data for tests.
- `ReminderNotifier` — sends reminder messages to patients. `InMemoryReminderNotifier` records sent alerts; `should_fail` flag for resilience tests.
- `Repository` — persists schedules and reminder records to SQL DB.

`LabScanReminderService` orchestrates all three and holds no mutable state itself.

---

## 3. Data Models

### 3.1 Provider DTO (read-only, from TestOrderProvider)

```
TestOrder
  order_id           UUID
  patient_id         UUID
  hospital_id        UUID
  ordered_by         UUID              doctor_id
  test_name          str               e.g. "CBC", "MRI Brain"
  test_type          Enum              LAB | SCAN
  due_date           date              patient must complete by this date
  preparation_notes  str               e.g. "Fast for 8 hours before the test"
```

### 3.2 DB Tables

```
reminder_schedules
  schedule_id        UUID        PK
  order_id           UUID        UNIQUE
  patient_id         UUID
  hospital_id        UUID
  remind_hours_before int        hours before due_date to fire; default 24
  fire_at            datetime    due_date - remind_hours_before; computed at registration
  status             Enum        ACTIVE | COMPLETED
  created_at         datetime

reminder_records
  reminder_id        UUID        PK
  schedule_id        UUID        FK → reminder_schedules
  order_id           UUID
  patient_id         UUID
  status             Enum        SENT | FAILED
  fired_at           datetime
```

**Key rules:**
- One `reminder_schedules` row per `order_id`. `register_order` twice raises `DUPLICATE_ORDER`.
- One `reminder_records` row per schedule — created by `evaluate()` when `fire_at <= now`.
- Idempotency: `evaluate()` checks for an existing `reminder_records` row before firing — no double-send on repeated ticks.
- Schedule marked `COMPLETED` immediately after the reminder fires (regardless of SENT/FAILED).

---

## 4. Core Flows

### 4.1 register_order(order_id)

```
Input: order_id

1. TestOrderProvider.get_order(order_id)
   → raise OrderNotFound if None

2. SELECT * FROM reminder_schedules WHERE order_id = ?
   → raise DuplicateOrder if row exists

3. fire_at = datetime.combine(order.due_date, time(0,0))
             - timedelta(hours=remind_hours_before)

4. INSERT INTO reminder_schedules(
     order_id, patient_id, hospital_id,
     remind_hours_before, fire_at,
     status = 'ACTIVE', created_at = now
   )

5. Return schedule
```

### 4.2 evaluate(now: datetime)

Called by an external scheduler. Processes all ACTIVE schedules.

```
For each ACTIVE reminder_schedule where fire_at <= now:

  1. Check no existing reminder_records row for schedule_id
     → skip if already fired (idempotency)

  2. Fetch TestOrder via order_id from TestOrderProvider

  3. status = 'SENT'
     try:
       ReminderNotifier.send_reminder(
         patient_id  = schedule.patient_id,
         test_name   = order.test_name,
         due_date    = order.due_date,
         prep_notes  = order.preparation_notes
       )
     except Exception:
       status = 'FAILED'

  4. INSERT INTO reminder_records(status, fired_at = now)

  5. UPDATE reminder_schedules SET status = 'COMPLETED'
     WHERE schedule_id = ?
```

---

## 5. Error Cases

| Scenario | Error Code | Behaviour |
|---|---|---|
| Order not found in LIS/RIS | `ORDER_NOT_FOUND` | Raise on `register_order` |
| Schedule already exists for order | `DUPLICATE_ORDER` | Raise on `register_order` |
| `ReminderNotifier.send_reminder` raises | _(no raise)_ | `reminder_records.status = FAILED`; schedule still marked `COMPLETED` |

---

## 6. Design Patterns

| Pattern | Application |
|---|---|
| Adapter | `TestOrderProvider`, `ReminderNotifier` — swappable behind ABCs |
| Evaluate-on-tick | `evaluate(now)` called externally; time injected for deterministic tests |
| Idempotency | Existing `reminder_records` row prevents double-send on repeated ticks |
| Fire-and-forget | Reminder sends best-effort; FAILED records logged but not retried |
