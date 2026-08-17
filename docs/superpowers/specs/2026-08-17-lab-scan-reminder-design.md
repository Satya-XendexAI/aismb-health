# Lab Test & Scan Reminder Service — Design Spec

**Date:** 2026-08-17
**Scope:** Backend LLD only — no UI/channel integration
**Status:** Approved

---

## 1. Overview

The Lab Test & Scan Reminder Service reminds patients to complete ordered lab tests and scans before their due date. It fires configurable pre-due reminders (including preparation instructions), a post-due follow-up if the patient has not confirmed completion, and records outcomes for reporting.

All external dependencies (LIS/RIS order system, WhatsApp notifier) are behind swappable adapter interfaces. In-memory stubs are used for standalone development and testing. Real adapters wire in during integration.

The service is driven by an external caller that invokes `evaluate(now)` on a configurable schedule. It holds no internal timer threads.

**Out of scope:** branch/location suggestion, escalation to ordering doctor, LIS/RIS push-based completion events.

---

## 2. Architecture

```
┌─────────────────────────────────────────────────────────┐
│               LabScanReminderService                     │
│                                                         │
│  + register_order(order_id) → OrderReminderSchedule     │
│  + evaluate(now: datetime) → None          ← tick       │
│  + acknowledge_completion(reminder_id, patient_id)      │
│  + get_completion_report(patient_id) → CompletionReport │
│                                                         │
│  [ adapters injected at construction time ]             │
└──────────────┬──────────────────────────────────────────┘
               │ uses
    ┌──────────┼──────────────┐
    ▼          ▼              ▼
TestOrderProvider   ReminderNotifier   InternalStore
(external — LIS/RIS) (external —       (owned by service)
                      WhatsApp)
```

**Components:**

- `TestOrderProvider` — fetches `TestOrder` by `order_id` from LIS/RIS. `InMemoryTestOrderProvider` returns hardcoded data for tests.
- `ReminderNotifier` — sends reminder messages to patients. `InMemoryReminderNotifier` records sent alerts; `should_fail` flag for resilience tests.
- `InternalStore` — owns all mutable service state: schedules, reminder records, completion records. `InMemoryInternalStore` for tests.

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
  ordered_at         datetime
  test_name          str               e.g. "CBC", "MRI Brain"
  test_type          Enum              LAB | SCAN
  due_date           date              patient must complete by this date
  preparation_notes  str               e.g. "Fast for 8 hours before the test"
```

### 3.2 Internal Entities (owned by InternalStore)

```
OrderReminderSchedule
  schedule_id             UUID        PK
  order_id                UUID        unique
  patient_id              UUID
  hospital_id             UUID
  reminder_offsets_hours  List[int]   e.g. [24, 2] — fire N hours before due_date
  follow_up_hours         int         default 24 — fire N hours after due_date if not done
  status                  Enum        ACTIVE | COMPLETED | OVERDUE | CANCELLED
  created_at              datetime

ReminderRecord
  reminder_id        UUID             PK
  schedule_id        UUID             FK → OrderReminderSchedule
  order_id           UUID
  patient_id         UUID
  reminder_type      Enum             PRE_DUE | FOLLOW_UP
  offset_hours       int?             set for PRE_DUE (e.g. 24, 2); None for FOLLOW_UP
  due_at             datetime         when this reminder was due to fire
  fired_at           datetime?
  status             Enum             PENDING | SENT | FAILED | ACKNOWLEDGED | MISSED

CompletionRecord
  record_id          UUID             PK
  order_id           UUID
  patient_id         UUID
  outcome            Enum             COMPLETED | OVERDUE
  recorded_at        datetime
```

**Key rules:**
- One `OrderReminderSchedule` per `order_id`. Duplicate registration raises `DUPLICATE_ORDER`.
- `ReminderRecord` rows are created lazily by `evaluate()` — not precomputed upfront.
- Idempotency key for PRE_DUE: `(schedule_id, offset_hours, PRE_DUE)` — prevents double-firing.
- Idempotency key for FOLLOW_UP: `(schedule_id, FOLLOW_UP)` — fires exactly once.
- `CompletionRecord` written when patient self-confirms (`COMPLETED`) or follow-up fires with no ack (`OVERDUE`).
- Once schedule is `COMPLETED`, `OVERDUE`, or `CANCELLED`, `evaluate()` skips it entirely.

---

## 4. Core Flows

### 4.1 register_order(order_id)

```
Input: order_id

1. TestOrderProvider.get_order(order_id)
   → raise OrderNotFound if None

2. InternalStore.get_schedule_by_order(order_id)
   → raise DuplicateOrder if schedule already exists

3. Create OrderReminderSchedule(
     status = ACTIVE,
     reminder_offsets_hours = [24, 2],   ← configurable at service construction
     follow_up_hours = 24                ← configurable at service construction
   )
4. InternalStore.save_schedule(schedule)
5. Return schedule
```

### 4.2 evaluate(now: datetime)

Called by an external scheduler. Processes all ACTIVE schedules.

```
For each ACTIVE OrderReminderSchedule:

  Fetch TestOrder via order_id from TestOrderProvider.
  due_datetime = datetime.combine(order.due_date, time(0, 0))

  A. PRE_DUE reminders
     For each offset_hours in schedule.reminder_offsets_hours:
       fire_at = due_datetime - timedelta(hours=offset_hours)
       if fire_at <= now:
         if no ReminderRecord for (schedule_id, offset_hours, PRE_DUE):
           fire_reminder(
             type=PRE_DUE, offset_hours=offset_hours, due_at=fire_at,
             payload={
               "test_name": order.test_name,
               "due_date": str(order.due_date),
               "preparation_notes": order.preparation_notes,
             }
           )

  B. FOLLOW_UP reminder
     follow_up_at = due_datetime + timedelta(hours=schedule.follow_up_hours)
     if follow_up_at <= now AND schedule.status == ACTIVE:
       if no ReminderRecord for (schedule_id, FOLLOW_UP):
         fire_reminder(type=FOLLOW_UP, offset_hours=None, due_at=follow_up_at,
                       payload={"test_name": order.test_name})
         schedule.status = OVERDUE
         create CompletionRecord(outcome=OVERDUE, recorded_at=now)
         InternalStore.save_schedule(schedule)
         InternalStore.save_completion_record(record)

fire_reminder(type, offset_hours, due_at, payload):
  status = SENT
  fired_at = now
  try:
    ReminderNotifier.send_reminder(patient_id, type, payload)
  except Exception:
    status = FAILED
  create and save ReminderRecord(status=status, fired_at=fired_at, ...)
```

### 4.3 acknowledge_completion(reminder_id, patient_id)

```
Input: reminder_id, patient_id

1. InternalStore.get_reminder(reminder_id)
   → raise ReminderNotFound if None

2. Validate reminder.patient_id == patient_id
   → raise Unauthorised if mismatch

3. Validate reminder.status == SENT
   → raise ReminderNotAcknowledgeable if MISSED | ACKNOWLEDGED | FAILED

4. Fetch schedule → validate schedule.status == ACTIVE
   → raise OrderAlreadyClosed if COMPLETED | OVERDUE | CANCELLED

5. Update reminder.status = ACKNOWLEDGED
6. Update schedule.status = COMPLETED
7. Create CompletionRecord(outcome=COMPLETED, recorded_at=now)
8. InternalStore.save_reminder(reminder)
9. InternalStore.save_schedule(schedule)
10. InternalStore.save_completion_record(record)
```

### 4.4 get_completion_report(patient_id)

```
Input: patient_id

1. Fetch all CompletionRecords for patient_id
2. Return CompletionReport:
     patient_id:      UUID
     total_orders:    int    (len(records))
     completed:       int    (outcome == COMPLETED)
     overdue:         int    (outcome == OVERDUE)
     completion_pct:  float  (completed / total_orders × 100, 0.0 if total == 0)
```

---

## 5. Error Cases

| Scenario | Error Code | Behaviour |
|---|---|---|
| Order not found in LIS/RIS | `ORDER_NOT_FOUND` | Raise on `register_order` |
| Schedule already exists for order | `DUPLICATE_ORDER` | Raise on `register_order` |
| `acknowledge_completion` — reminder not found | `REMINDER_NOT_FOUND` | Raise |
| `acknowledge_completion` — patient_id mismatch | `UNAUTHORISED` | Raise |
| `acknowledge_completion` — reminder status not SENT | `REMINDER_NOT_ACKNOWLEDGEABLE` | Raise |
| `acknowledge_completion` — schedule already COMPLETED/OVERDUE/CANCELLED | `ORDER_ALREADY_CLOSED` | Raise |
| `ReminderNotifier.send_reminder` raises | _(no raise)_ | `ReminderRecord.status = FAILED`; `evaluate()` continues |

---

## 6. Concurrency and Idempotency

**evaluate() idempotency:**
- PRE_DUE: checked by `(schedule_id, offset_hours, PRE_DUE)` — calling `evaluate()` twice at the same `now` fires no duplicate records.
- FOLLOW_UP: checked by `(schedule_id, FOLLOW_UP)` — fires exactly once per schedule.

**acknowledge_completion idempotency:** Second call raises `REMINDER_NOT_ACKNOWLEDGEABLE` — status is already `ACKNOWLEDGED`.

**register_order idempotency:** Second call raises `DUPLICATE_ORDER`.

---

## 7. Design Patterns

| Pattern | Application |
|---|---|
| Adapter | `TestOrderProvider`, `ReminderNotifier` — swappable behind ABCs |
| Evaluate-on-tick | `evaluate(now)` called externally; time injected for deterministic tests |
| Idempotency key | `(schedule_id, offset_hours, type)` prevents double-firing on repeated ticks |
| Lazy record creation | `ReminderRecord` rows created at fire time, not precomputed |
| Audit trail | Every reminder event recorded with `SENT`/`FAILED`/`ACKNOWLEDGED`/`MISSED` |

---

## 8. Open Questions

- Should `FAILED` PRE_DUE reminders be retried on the next `evaluate()` tick? Current design: the idempotency check finds the existing `FAILED` record and skips it — no retry. If retry is needed, the idempotency check must exclude `FAILED` records.
- Should `reminder_offsets_hours` be configurable per order (e.g., scan orders need 48h notice, lab tests only 24h)? Current design: single configurable default at service construction time applied to all orders.
