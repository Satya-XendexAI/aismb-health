# Prescription Medication Reminder Service — Design Spec

**Date:** 2026-08-17
**Scope:** Backend LLD only — no UI/channel integration
**Status:** Approved

---

## 1. Overview

The Prescription Medication Reminder Service generates dosage reminders from e-prescriptions, tracks patient adherence via self-confirmation, fires missed-dose follow-ups, and sends a refill reminder before the course ends.

All external dependencies (ePrescription/EMR system, WhatsApp notifier) are behind swappable adapter interfaces. In-memory stubs are used for standalone development and testing. Real adapters wire in during integration.

The service is driven by an external caller that invokes `evaluate(now)` on a configurable schedule (e.g., every minute). The service itself holds no internal timer threads.

---

## 2. Architecture

```
┌─────────────────────────────────────────────────────────┐
│               MedicationReminderService                  │
│                                                         │
│  + create_schedule(prescription_id) → List[Schedule]    │
│  + evaluate(now: datetime) → None          ← tick       │
│  + acknowledge_dose(reminder_id, patient_id) → None     │
│  + get_adherence_report(patient_id, medication_id)      │
│                                                         │
│  [ adapters injected at construction time ]             │
└──────────────┬──────────────────────────────────────────┘
               │ uses
    ┌──────────┼──────────────┐
    ▼          ▼              ▼
PrescriptionProvider   ReminderNotifier   InternalStore
(external — EMR)       (external —        (owned by service)
                        WhatsApp)
```

**Components:**

- `PrescriptionProvider` — fetches `Prescription` and `List[PrescriptionMedication]` from the EMR. `InMemoryPrescriptionProvider` returns hardcoded data for tests.
- `ReminderNotifier` — sends reminder messages to patients. `InMemoryReminderNotifier` records sent alerts in a list; `should_fail` flag triggers failures for resilience tests.
- `InternalStore` — owns all mutable service state (schedules, reminder records, adherence records). `InMemoryInternalStore` for testing.

`MedicationReminderService` orchestrates all three. It holds no mutable state itself — all writes go through `InternalStore`.

---

## 3. Data Models

### 3.1 Provider DTOs (read-only, from PrescriptionProvider)

```
Prescription
  prescription_id   UUID
  patient_id        UUID
  hospital_id       UUID
  prescribed_by     UUID              doctor_id
  prescribed_at     datetime
  status            Enum              ACTIVE | COMPLETED | CANCELLED

PrescriptionMedication
  medication_id     UUID
  prescription_id   UUID
  drug_name         str
  dose_description  str               e.g. "1 tablet"
  dose_times        List[time]        e.g. [08:00, 14:00, 21:00]
  duration_days     int
  start_date        date
  refill_reminder_days  int           default 3
```

### 3.2 Internal Entities (owned by InternalStore)

```
MedicationSchedule
  schedule_id           UUID        PK
  medication_id         UUID
  prescription_id       UUID
  patient_id            UUID
  hospital_id           UUID
  ack_window_minutes    int         default 30 — window after DOSE reminder to confirm
  refill_reminder_sent  bool        default False
  status                Enum        ACTIVE | COMPLETED | CANCELLED
  created_at            datetime

ReminderRecord
  reminder_id       UUID        PK
  schedule_id       UUID        FK → MedicationSchedule
  medication_id     UUID
  patient_id        UUID
  reminder_type     Enum        DOSE | MISSED_FOLLOWUP | REFILL
  due_at            datetime    scheduled fire time (dose slot for DOSE; same as DOSE for MISSED_FOLLOWUP; computed for REFILL)
  fired_at          datetime?
  status            Enum        PENDING | SENT | FAILED | ACKNOWLEDGED | MISSED

AdherenceRecord
  record_id         UUID        PK
  medication_id     UUID
  patient_id        UUID
  due_at            datetime    the dose slot this records
  confirmed_at      datetime?
  outcome           Enum        TAKEN | MISSED
```

**Key rules:**
- One `MedicationSchedule` per `PrescriptionMedication`. A prescription with 4 drugs → 4 schedules.
- `ReminderRecord` rows are created lazily by `evaluate()` — not precomputed upfront.
- `AdherenceRecord` is written when a dose is acknowledged (`TAKEN`) or when a missed-followup fires (`MISSED`).
- Refill reminder fires once per schedule, tracked by `refill_reminder_sent` flag.
- Idempotency key for DOSE reminders: `(schedule_id, due_at, DOSE)` — duplicate check prevents double-firing.

---

## 4. Core Flows

### 4.1 create_schedule(prescription_id)

```
Input: prescription_id

1. PrescriptionProvider.get_prescription(prescription_id)
   → raise PrescriptionNotFound if None
   → raise InactivePrescription if status ≠ ACTIVE

2. PrescriptionProvider.get_medications(prescription_id)
   → raise NoMedicationsFound if list is empty

3. For each PrescriptionMedication:
     a. InternalStore.get_schedule_by_medication(medication_id)
        → raise DuplicateSchedule if ACTIVE schedule already exists
     b. Create MedicationSchedule (status = ACTIVE, refill_reminder_sent = False)
     c. InternalStore.save_schedule(schedule)

4. Return List[MedicationSchedule]
```

### 4.2 evaluate(now: datetime)

Called by an external scheduler. Processes all ACTIVE schedules.

```
For each ACTIVE MedicationSchedule:

  Fetch PrescriptionMedication via medication_id from PrescriptionProvider.

  A. DOSE reminders
     today = now.date()
     For each dose_time in medication.dose_times:
       due_at = datetime.combine(today, dose_time)
       if due_at <= now:
         if no ReminderRecord exists for (schedule_id, due_at, DOSE):
           fire_reminder(patient_id, DOSE, due_at, schedule_id, medication_id)

  B. MISSED_FOLLOWUP check
     For each ReminderRecord of type DOSE with status = SENT:
       if record.fired_at + ack_window_minutes < now:
         fire_reminder(patient_id, MISSED_FOLLOWUP, record.due_at, schedule_id, medication_id)
         update record.status = MISSED
         create AdherenceRecord(outcome = MISSED, due_at = record.due_at)

  C. REFILL reminder
     end_date = medication.start_date + timedelta(days=medication.duration_days)
     days_remaining = (end_date - now.date()).days
     if days_remaining <= medication.refill_reminder_days AND NOT schedule.refill_reminder_sent:
       fire_reminder(patient_id, REFILL, datetime.combine(end_date, time(0,0)), schedule_id, medication_id)
       schedule.refill_reminder_sent = True
       InternalStore.save_schedule(schedule)

  D. Course completion
     if now.date() > end_date:
       schedule.status = COMPLETED
       InternalStore.save_schedule(schedule)

fire_reminder(patient_id, type, due_at, schedule_id, medication_id):
  status = SENT
  fired_at = now
  try:
    ReminderNotifier.send_reminder(patient_id, type, payload)
  except Exception:
    status = FAILED
  create and save ReminderRecord(status=status, fired_at=fired_at, ...)
```

### 4.3 acknowledge_dose(reminder_id, patient_id)

```
Input: reminder_id, patient_id

1. InternalStore.get_reminder(reminder_id)
   → raise ReminderNotFound if None
2. Validate reminder.patient_id == patient_id
   → raise Unauthorised if mismatch
3. Validate reminder.status == SENT
   → raise ReminderNotAcknowledgeable if status is MISSED | ACKNOWLEDGED | FAILED
4. Update reminder.status = ACKNOWLEDGED
5. Create AdherenceRecord(outcome = TAKEN, confirmed_at = now, due_at = reminder.due_at)
6. InternalStore.save_reminder(reminder)
7. InternalStore.save_adherence_record(record)
```

### 4.4 get_adherence_report(patient_id, medication_id)

```
Input: patient_id, medication_id

1. Fetch all AdherenceRecords for (patient_id, medication_id)
2. Return AdherenceReport:
     medication_id:   UUID
     patient_id:      UUID
     total_doses:     int    (len(records))
     taken:           int    (count where outcome = TAKEN)
     missed:          int    (count where outcome = MISSED)
     adherence_pct:   float  (taken / total_doses × 100 if total_doses > 0 else 0.0)
```

---

## 5. Error Cases

| Scenario | Error Code | Behaviour |
|---|---|---|
| Prescription not found | `PRESCRIPTION_NOT_FOUND` | Raise on `create_schedule` |
| Prescription status ≠ ACTIVE | `INACTIVE_PRESCRIPTION` | Raise on `create_schedule` |
| No medications on prescription | `NO_MEDICATIONS_FOUND` | Raise on `create_schedule` |
| ACTIVE schedule already exists for medication | `DUPLICATE_SCHEDULE` | Raise on `create_schedule` |
| `acknowledge_dose` — reminder not found | `REMINDER_NOT_FOUND` | Raise |
| `acknowledge_dose` — patient_id mismatch | `UNAUTHORISED` | Raise |
| `acknowledge_dose` — status is MISSED/ACKNOWLEDGED/FAILED | `REMINDER_NOT_ACKNOWLEDGEABLE` | Raise |
| `ReminderNotifier.send_reminder` raises | _(no raise)_ | `ReminderRecord.status = FAILED`; `evaluate()` continues |

---

## 6. Concurrency and Idempotency

**evaluate() idempotency:** Before creating a DOSE `ReminderRecord`, the service checks for an existing record with the same `(schedule_id, due_at, DOSE)`. Calling `evaluate()` twice at the same `now` is safe — no duplicate reminders fire.

**MISSED_FOLLOWUP idempotency:** Before creating a `MISSED_FOLLOWUP` record, the service checks for an existing record with `(schedule_id, due_at, MISSED_FOLLOWUP)`. Prevents duplicate follow-ups if `evaluate()` is called multiple times within the same window.

**Refill idempotency:** `refill_reminder_sent = True` flag on `MedicationSchedule` — fires exactly once per schedule.

---

## 7. Design Patterns

| Pattern | Application |
|---|---|
| Adapter | `PrescriptionProvider`, `ReminderNotifier` — swappable behind ABCs |
| Evaluate-on-tick | `evaluate(now)` called externally; time injected for deterministic tests |
| Idempotency key | `(schedule_id, due_at, reminder_type)` prevents double-firing |
| Lazy record creation | `ReminderRecord` rows created at fire time, not precomputed |
| Audit trail | Every reminder event recorded in `ReminderRecord` with `SENT`/`FAILED`/`ACKNOWLEDGED`/`MISSED` |

---

## 8. Open Questions

- Should a `FAILED` DOSE reminder be retried on the next `evaluate()` tick, or left as FAILED permanently? Current design: left as FAILED — the idempotency check would skip it on retry. If retry is needed, status must be `PENDING` initially and only flipped to `SENT`/`FAILED` after the send attempt.
- Should cancelled prescriptions auto-cancel active schedules? Current design: `create_schedule` rejects `CANCELLED` prescriptions, but mid-course cancellation of a running schedule is not handled — attender would need a `cancel_schedule(schedule_id)` endpoint. Out of scope for this spec.
