# Prescription Medication Reminder Service — Design Spec

**Date:** 2026-08-17 (revised 2026-08-18)
**Scope:** Backend LLD only — no UI/channel integration
**Status:** Approved

---

## 1. Overview

The Prescription Medication Reminder Service fires dose reminders for active medications. Medication data is managed directly in the DB from the backend — no EMR adapter or registration flow. The service exposes a single `evaluate(now)` method that fires a WhatsApp reminder for each medication dose due at or before `now`, skipping doses already sent.

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

reminder_records                         -- written by the service
  reminder_id       UUID        PK
  medication_id     UUID        FK → medications
  patient_id        UUID
  due_at            datetime    the exact dose slot (date + dose_time)
  status            Enum        SENT | FAILED
  fired_at          datetime
```

**Idempotency key:** `(medication_id, due_at)` — one row per dose slot per medication. `evaluate()` skips a slot if a row already exists.

---

## 4. Core Flow

### 4.1 evaluate(now: datetime)

```
SELECT * FROM medications
WHERE start_date <= now.date() AND end_date >= now.date()

For each medication:

  For each dose_time in medication.dose_times:
    due_at = datetime.combine(now.date(), dose_time)

    if due_at > now: skip   ← not yet due

    if SELECT EXISTS FROM reminder_records
       WHERE medication_id = ? AND due_at = ?: skip   ← already fired

    status = 'SENT'
    try:
      ReminderNotifier.send_reminder(
        patient_id       = medication.patient_id,
        drug_name        = medication.drug_name,
        dose_description = medication.dose_description,
        due_at           = due_at
      )
    except Exception:
      status = 'FAILED'

    INSERT INTO reminder_records(medication_id, patient_id,
                                 due_at, status, fired_at = now)
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
| Idempotency | `(medication_id, due_at)` existence check prevents double-send on repeated ticks |
| Fire-and-forget | Reminders best-effort; FAILED rows logged but not retried |
