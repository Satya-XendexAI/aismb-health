# Prescription Medication Reminder Service — Design Spec

**Date:** 2026-08-17 (revised 2026-08-18 ×2)
**Scope:** Backend LLD only — no UI/channel integration
**Status:** Approved

---

## 1. Overview

The Prescription Medication Reminder Service is triggered manually (e.g., by a staff member or admin action). When triggered, it finds all patients with active medications today who have not yet received a reminder today, groups their medications into one WhatsApp message per patient, and sends it.

Medication data is managed directly in the DB from the backend. State is persisted in SQL via a `Repository` layer.

---

## 2. Architecture

```
┌─────────────────────────────────────────────────────────┐
│           MedicationReminderService                      │
│                                                         │
│  + send_reminders() → ReminderSummary   ← manual call   │
│                                                         │
│  [ notifier injected at construction ]                  │
└──────────────┬──────────────────────────────────────────┘
               │ uses
         ┌─────┴──────────────┐
         ▼                    ▼
  ReminderNotifier        Repository
  (external — WhatsApp)   (SQL DB — medications
                            + reminder_records)
```

**Components:**

- `ReminderNotifier` — sends WhatsApp messages. `InMemoryReminderNotifier` with `should_fail` flag for tests.
- `Repository` — reads `medications` and writes `reminder_records` to SQL DB.

`MedicationReminderService` holds no mutable state itself.

---

## 3. Data Models

### 3.1 DB Tables

```
medications                              -- populated and managed by backend
  medication_id     UUID        PK
  patient_id        UUID
  hospital_id       UUID
  drug_name         str               e.g. "Metformin 500mg"
  dose_description  str               e.g. "1 tablet twice daily"
  start_date        date
  end_date          date

reminder_records                         -- written by the service; one per patient per day
  reminder_id       UUID        PK
  patient_id        UUID
  reminder_date     date              the calendar date this reminder covers
  triggered_at      datetime          when send_reminders() was called
  status            Enum        SENT | FAILED
  fired_at          datetime
```

**Idempotency key:** `(patient_id, reminder_date)` — one reminder per patient per day. SENT records are skipped on subsequent triggers. FAILED records are retried on the next trigger.

---

## 4. Core Flow

### 4.1 send_reminders() → ReminderSummary

```
today = current date

1. Find patients with active medications and no reminder sent today:
   SELECT DISTINCT patient_id FROM medications
   WHERE start_date <= today AND end_date >= today
     AND patient_id NOT IN (
       SELECT patient_id FROM reminder_records
       WHERE reminder_date = today AND status = 'SENT'
     )

2. For each patient_id:

     a. Fetch their active medications:
        SELECT * FROM medications
        WHERE patient_id = ? AND start_date <= today AND end_date >= today

     b. status = 'SENT'
        try:
          ReminderNotifier.send_reminder(
            patient_id  = patient_id,
            medications = [{ drug_name, dose_description } for each medication]
          )
        except Exception:
          status = 'FAILED'

     c. INSERT INTO reminder_records(patient_id, reminder_date = today,
                                     triggered_at = now, status, fired_at = now)

3. Return ReminderSummary(
     total_patients = count of patients processed,
     sent           = count where status = SENT,
     failed         = count where status = FAILED
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
| DB-managed data | Medications created and managed by backend; service reads them directly |
| Manual trigger | `send_reminders()` called on demand — no scheduler or tick needed |
| Idempotency | `(patient_id, reminder_date)` prevents double-send on same day; FAILED records retried |
| Fire-and-forget | Reminders best-effort; FAILED rows logged and retried on next call |
