# Post-Visit Follow-up Service — Design Spec

**Date:** 2026-08-17 (revised 2026-08-18 ×3)
**Scope:** Backend LLD only — no UI/channel integration
**Status:** Approved

---

## 1. Overview

The Post-Visit Follow-up Service is triggered manually (e.g., by a staff member or admin action). When triggered, it finds all patients with completed encounters whose visit ended at least 7 days ago (configurable) and who have not yet received a follow-up reminder, and sends each one a WhatsApp message.

Encounter data is managed directly in the DB from the backend. State is persisted in SQL via a `Repository` layer.

---

## 2. Architecture

```
┌─────────────────────────────────────────────────────────┐
│             PostVisitFollowUpService                     │
│                                                         │
│  + send_reminders() → ReminderSummary   ← manual call   │
│                                                         │
│  [ notifier + reminder_delay_days injected at           │
│    construction; default 7 days ]                       │
└──────────────┬──────────────────────────────────────────┘
               │ uses
         ┌─────┴──────────────┐
         ▼                    ▼
  ReminderNotifier        Repository
  (external — WhatsApp)   (SQL DB — encounters
                            + reminder_records)
```

**Components:**

- `ReminderNotifier` — sends WhatsApp messages. `InMemoryReminderNotifier` with `should_fail` flag for tests.
- `Repository` — reads `encounters` and writes `reminder_records` to SQL DB.

`PostVisitFollowUpService` holds no mutable state itself.

---

## 3. Data Models

### 3.1 DB Tables

```
encounters                               -- populated and managed by backend
  encounter_id       UUID        PK
  patient_id         UUID
  hospital_id        UUID
  encounter_type     str               e.g. "OPD", "SURGERY", "DISCHARGE"
  attending_doctor   UUID
  visit_ended_at     datetime
  status             Enum              ONGOING | COMPLETED

reminder_records                         -- written by the service; one per encounter per trigger
  reminder_id        UUID        PK
  encounter_id       UUID
  patient_id         UUID
  triggered_at       datetime          when send_reminders() was called
  status             Enum        SENT | FAILED
  fired_at           datetime
```

**Idempotency key:** `encounter_id` — if a SENT record already exists for an encounter, it is skipped on subsequent triggers. FAILED records are retried on the next trigger.

---

## 4. Core Flow

### 4.1 send_reminders() → ReminderSummary

```
1. Find eligible encounters:
   SELECT DISTINCT encounter_id, patient_id FROM encounters
   WHERE status = 'COMPLETED'
     AND visit_ended_at <= now - INTERVAL '{reminder_delay_days} days'   ← default 7 days
     AND encounter_id NOT IN (
       SELECT encounter_id FROM reminder_records WHERE status = 'SENT'
     )

2. For each encounter:

     a. status = 'SENT'
        try:
          ReminderNotifier.send_reminder(
            patient_id   = patient_id,
            encounter_id = encounter_id
          )
        except Exception:
          status = 'FAILED'

     b. INSERT INTO reminder_records(encounter_id, patient_id,
                                     triggered_at = now, status, fired_at = now)

3. Return ReminderSummary(
     total_encounters = count of encounters processed,
     sent             = count where status = SENT,
     failed           = count where status = FAILED
   )
```

---

## 5. Error Cases

| Scenario | Error Code | Behaviour |
|---|---|---|
| `ReminderNotifier.send_reminder` raises | _(no raise)_ | `reminder_records.status = FAILED`; continues to next encounter; retried on next trigger |

---

## 6. Design Patterns

| Pattern | Application |
|---|---|
| DB-managed data | Encounters created and managed by backend; service reads them directly |
| Manual trigger | `send_reminders()` called on demand — no scheduler or tick needed |
| Idempotency | Encounters with existing SENT record skipped; FAILED records retried on next trigger |
| Fire-and-forget | Reminders best-effort; FAILED rows logged and retried on next call |
