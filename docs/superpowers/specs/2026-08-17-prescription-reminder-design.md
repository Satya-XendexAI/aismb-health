# Prescription Medication Reminder Service — Design Spec

**Date:** 2026-08-17 (revised 2026-08-18)
**Scope:** Backend LLD only — no UI/channel integration
**Status:** Approved

---

## 1. Overview

The Prescription Medication Reminder Service fires dose reminders for active medications. Medication data is managed directly in the DB from the backend — no EMR adapter or registration flow. The service exposes a single `evaluate(now)` method that groups all due doses by patient and sends one WhatsApp reminder per patient per dose time, listing all their due medications together.

Sent reminders are recorded in a SQL DB table for idempotency.

---

## 2. Architecture

```
┌─────────────────────────────────────────────────────────┐
│            MedicationReminderService                     │
│                                                         │
│  + evaluate(now: datetime) → None          ← tick       │
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

- `ReminderNotifier` — sends reminder messages to patients via WhatsApp. `InMemoryReminderNotifier` with `should_fail` flag for tests.
- `Repository` — reads `medications` and writes `reminder_records` to SQL DB.

`MedicationReminderService` holds no mutable state itself.

---

## 3. Data Models

### 3.1 DB Tables

```
medications                              -- populated and managed by backend
  medication_id     UUID        PK
  patient_id        UUID
  drug_name         str               e.g. "Metformin 500mg"
  dose_description  str               e.g. "1 tablet"
  dose_times        List[time]        e.g. [08:00, 21:00]
  start_date        date
  end_date          date

reminder_records                         -- written by the service; one per patient per dose slot
  reminder_id       UUID        PK
  patient_id        UUID
  due_at            datetime    the dose slot (date + dose_time) this reminder covers
  status            Enum        SENT | FAILED
  fired_at          datetime
```

**Idempotency key:** `(patient_id, due_at)` — one reminder per patient per dose slot. `evaluate()` skips a slot if a row already exists for that patient.

---

## 4. Core Flow

### 4.1 evaluate(now: datetime)

```
1. Fetch active medications:
   SELECT * FROM medications
   WHERE start_date <= now.date() AND end_date >= now.date()

2. Collect due dose slots:
   For each medication, for each dose_time:
     due_at = datetime.combine(now.date(), dose_time)
     if due_at <= now: include (patient_id, due_at, medication)

3. Group by (patient_id, due_at)

4. For each (patient_id, due_at) group:

     a. Check no existing reminder_records row for (patient_id, due_at)
        → skip if already sent (idempotency)

     b. status = 'SENT'
        try:
          ReminderNotifier.send_reminder(
            patient_id  = patient_id,
            due_at      = due_at,
            medications = [{ drug_name, dose_description } for each med in group]
          )
        except Exception:
          status = 'FAILED'

     c. INSERT INTO reminder_records(patient_id, due_at, status, fired_at = now)
```

---

## 5. Error Cases

| Scenario | Error Code | Behaviour |
|---|---|---|
| `ReminderNotifier.send_reminder` raises | _(no raise)_ | `reminder_records.status = FAILED`; `evaluate()` continues to next dose |

---

## 6. Design Patterns

| Pattern | Application |
|---|---|
| DB-managed data | Medications created and managed by backend; service reads them directly — no adapter or registration flow |
| Evaluate-on-tick | `evaluate(now)` called externally; time injected for deterministic tests |
| Idempotency | `(patient_id, due_at)` existence check prevents double-send on repeated ticks |
| Fire-and-forget | Reminders best-effort; FAILED rows logged but not retried |
