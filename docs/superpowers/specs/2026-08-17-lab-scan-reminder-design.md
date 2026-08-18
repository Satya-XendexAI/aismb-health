# Lab Test & Scan Reminder Service — Design Spec

**Date:** 2026-08-17 (revised 2026-08-18)
**Scope:** Backend LLD only — no UI/channel integration
**Status:** Approved

---

## 1. Overview

The Lab Test & Scan Reminder Service is triggered manually (e.g., by a staff member or admin action). When triggered, it finds all patients who have pending test orders with no reminder sent yet, groups their orders into one WhatsApp message per patient, and sends it.

Test order data is managed directly in the DB from the backend. State is persisted in SQL via a `Repository` layer.

---

## 2. Architecture

```
┌─────────────────────────────────────────────────────────┐
│               LabScanReminderService                     │
│                                                         │
│  + send_reminders() → ReminderSummary   ← manual call   │
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

- `ReminderNotifier` — sends WhatsApp messages. `InMemoryReminderNotifier` with `should_fail` flag for tests.
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
  due_date           date
  preparation_notes  str               e.g. "Fast for 8 hours before the test"

reminder_records                         -- written by the service; one per patient per trigger
  reminder_id        UUID        PK
  patient_id         UUID
  triggered_at       datetime          when send_reminders() was called
  status             Enum        SENT | FAILED
  fired_at           datetime
```

**Idempotency key:** `patient_id` — if a SENT record already exists for a patient, they are skipped on subsequent triggers. FAILED records are retried on the next trigger.

---

## 4. Core Flow

### 4.1 send_reminders() → ReminderSummary

```
1. Find patients with pending orders:
   SELECT DISTINCT patient_id FROM test_orders
   WHERE patient_id NOT IN (
     SELECT patient_id FROM reminder_records WHERE status = 'SENT'
   )

2. For each patient_id:

     a. Fetch their orders:
        SELECT * FROM test_orders WHERE patient_id = ?

     b. status = 'SENT'
        try:
          ReminderNotifier.send_reminder(
            patient_id = patient_id,
            orders     = [{ test_name, due_date, prep_notes } for each order]
          )
        except Exception:
          status = 'FAILED'

     c. INSERT INTO reminder_records(patient_id, triggered_at = now,
                                     status, fired_at = now)

3. Return ReminderSummary(
     total_patients  = count of patients processed,
     sent            = count where status = SENT,
     failed          = count where status = FAILED
   )
```

---

## 5. Error Cases

| Scenario | Error Code | Behaviour |
|---|---|---|
| `ReminderNotifier.send_reminder` raises | _(no raise)_ | `reminder_records.status = FAILED`; continues to next patient; retried on next trigger |

---

## 6. Design Patterns

| Pattern | Application |
|---|---|
| DB-managed data | Test orders created and managed by backend; service reads them directly |
| Manual trigger | `send_reminders()` called on demand — no scheduler or tick needed |
| Idempotency | Patients with existing SENT record skipped; FAILED records retried on next trigger |
| Fire-and-forget | Reminders best-effort; FAILED rows logged and retried on next call |
