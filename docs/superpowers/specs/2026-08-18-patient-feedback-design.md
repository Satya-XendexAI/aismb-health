# Patient Feedback Collection Service — Design Spec

**Date:** 2026-08-18
**Scope:** Backend LLD only — no UI/channel integration
**Status:** Approved

---

## 1. Overview

The Patient Feedback Collection Service collects structured and free-text feedback from patients after every encounter (OPD visit, inpatient discharge, procedure). Feedback is collected via a sequential WhatsApp conversation — one question at a time — triggered 2 hours after the encounter ends.

The 6-question sequence covers NPS, overall satisfaction, doctor experience, facility rating, a complaint flag, and (conditionally) a free-text complaint detail. If a patient raises a complaint, a `ComplaintRecord` is saved to the DB. No categorization is performed — the analytics pipeline will process raw responses at a later stage. There is no CRM integration for the demo phase; all data is stored in the service's own store.

All external dependencies (EMR, WhatsApp notifier) are behind swappable adapter interfaces. The question list is injected at construction time — no provider adapter needed. In-memory stubs are used for standalone development and testing.

The service is driven by an external caller invoking `evaluate(now)` on a configurable schedule. It holds no internal timer threads.

---

## 2. Architecture

```
┌──────────────────────────────────────────────────────────────┐
│                  PatientFeedbackService                       │
│                                                              │
│  + register_encounter(encounter_id) → FeedbackSchedule       │
│  + evaluate(now: datetime) → None              ← tick        │
│  + submit_answer(session_id, patient_id,                     │
│                  question_id, answer) → NextQuestion | None  │
│  + get_feedback_report(encounter_id) → FeedbackReport        │
│                                                              │
│  [ adapters + question list injected at construction time ]  │
└─────────────────┬────────────────────────────────────────────┘
                  │ uses
        ┌─────────┼──────────────┐
        ▼         ▼              ▼
EncounterProvider  PatientNotifier  InternalStore
(external — EMR)   (external —      (owned by service)
                    WhatsApp)
```

**Components:**

- `EncounterProvider` — fetches encounter data from EMR. `InMemoryEncounterProvider` returns hardcoded data for tests.
- `PatientNotifier` — sends questions to patients one at a time via WhatsApp. `InMemoryPatientNotifier` with `should_fail` flag.
- `InternalStore` — owns all mutable state: schedules, sessions, responses, complaint records. `InMemoryInternalStore` for tests.
- **Question list** — a `List[FeedbackQuestion]` passed at construction time. The default 6-question sequence is a module-level constant. No adapter needed.

`PatientFeedbackService` orchestrates all three and holds no mutable state itself.

---

## 3. Data Models

### 3.1 Provider DTOs (read-only)

```
Encounter                               -- from EncounterProvider
  encounter_id       UUID
  patient_id         UUID
  hospital_id        UUID
  encounter_type     str                e.g. "OPD", "SURGERY", "DISCHARGE"
  attending_doctor   UUID
  visit_ended_at     datetime           consultation_ended_at or discharged_at
```

### 3.2 Construction-time config

```
FeedbackQuestion                        -- List[FeedbackQuestion] injected at construction
  question_id        UUID               stable well-known UUID per question
  text               str
  answer_type        Enum               NPS_0_10 | SCALE_1_5 | YES_NO | FREE_TEXT
  order              int                1-based sequence position
  conditional_on     UUID?              if set, only asked when the referenced question
                                        was answered YES
```

**Default question sequence:**

| order | text | answer_type | conditional_on |
|---|---|---|---|
| 1 | "How likely are you to recommend us? (0–10)" | NPS_0_10 | — |
| 2 | "How satisfied were you overall? (1–5)" | SCALE_1_5 | — |
| 3 | "How would you rate your doctor? (1–5)" | SCALE_1_5 | — |
| 4 | "How would you rate our facilities? (1–5)" | SCALE_1_5 | — |
| 5 | "Do you have a complaint to raise?" | YES_NO | — |
| 6 | "Please describe your complaint." | FREE_TEXT | Q5 |

### 3.3 Internal Entities (owned by InternalStore)

```
FeedbackSchedule
  schedule_id        UUID               PK
  encounter_id       UUID               unique
  patient_id         UUID
  hospital_id        UUID
  encounter_type     str
  delay_hours        int                configurable at construction; default 2
  status             Enum               ACTIVE | COMPLETED | CANCELLED
  created_at         datetime

FeedbackSession
  session_id         UUID               PK
  schedule_id        UUID               FK → FeedbackSchedule
  encounter_id       UUID
  patient_id         UUID
  question_ids       List[UUID]         ordered; starts with non-conditional questions;
                                        Q6 appended dynamically if Q5 = YES
  current_index      int                0-based; advances per answer
  status             Enum               PENDING | IN_PROGRESS | COMPLETED | NO_RESPONSE
  started_at         datetime?
  completed_at       datetime?
  expires_at         datetime           created_at + session_expiry_hours (default 24h)

FeedbackResponse
  response_id        UUID               PK
  session_id         UUID               FK → FeedbackSession
  question_id        UUID
  patient_id         UUID
  answer             str                raw string; all answer types stored as str
  answered_at        datetime

ComplaintRecord
  complaint_id       UUID               PK
  encounter_id       UUID
  patient_id         UUID
  session_id         UUID
  detail             str                answer to the free-text complaint question
  status             Enum               OPEN | RESOLVED
  created_at         datetime

NextQuestion                           -- return value of submit_answer when more questions remain
  question_id        UUID
  text               str
  answer_type        Enum

FeedbackReport                         -- return value of get_feedback_report
  encounter_id       UUID
  patient_id         UUID
  session_status     Enum?             None if session not yet created
  responses          List[FeedbackResponse]
  complaint          ComplaintRecord?
```

**Key rules:**
- One `FeedbackSchedule` per encounter. `register_encounter` twice raises `DUPLICATE_ENCOUNTER`.
- One `FeedbackSession` per schedule, created lazily by `evaluate()` when the delay is due.
- `question_ids` starts with all non-conditional questions (Q1–Q5). Q6 is appended only when Q5 = YES, guarded to prevent double-append.
- `ComplaintRecord` is created when Q6 (complaint detail) is saved — not on Q5 = YES — so the record always contains the detail text.
- Session expires 24h after creation with no completion → `NO_RESPONSE`; schedule marked `COMPLETED`.

---

## 4. Core Flows

### 4.1 register_encounter(encounter_id)

```
Input: encounter_id

1. EncounterProvider.get_encounter(encounter_id)
   → raise EncounterNotFound if None

2. InternalStore.get_schedule_by_encounter(encounter_id)
   → raise DuplicateEncounter if exists

3. Create FeedbackSchedule(
     patient_id = encounter.patient_id,
     hospital_id = encounter.hospital_id,
     encounter_type = encounter.encounter_type,
     delay_hours = delay_hours,          ← configurable at construction; default 2
     status = ACTIVE,
     created_at = now
   )
4. InternalStore.save_schedule(schedule)
5. Return schedule
```

### 4.2 evaluate(now: datetime)

Called by an external scheduler. Processes all ACTIVE schedules.

```
For each ACTIVE FeedbackSchedule:

  A. Expire stale session
     session = InternalStore.get_session(schedule_id)
     if session and session.status in (PENDING, IN_PROGRESS) and now > session.expires_at:
       session.status = NO_RESPONSE
       InternalStore.save_session(session)
       schedule.status = COMPLETED
       InternalStore.save_schedule(schedule)
       continue

  B. Create session if due and not yet created
     if session is None:
       encounter = EncounterProvider.get_encounter(schedule.encounter_id)
       due_at = encounter.visit_ended_at + timedelta(hours=schedule.delay_hours)
       if now >= due_at:
         question_ids = [q.question_id for q in questions if q.conditional_on is None]
         Create FeedbackSession(
           question_ids = question_ids,   ← Q6 excluded; appended dynamically if Q5 = YES
           current_index = 0,
           status = PENDING,
           expires_at = now + timedelta(hours=session_expiry_hours)
         )
         InternalStore.save_session(session)
         first_q = next(q for q in questions if q.question_id == session.question_ids[0])
         _send_question(session, first_q, now)

  C. Mark schedule COMPLETED when session is done
     if session and session.status in (COMPLETED, NO_RESPONSE):
       schedule.status = COMPLETED
       InternalStore.save_schedule(schedule)

_send_question(session, question, now):
  session.started_at = session.started_at or now
  session.status = IN_PROGRESS
  InternalStore.save_session(session)
  try:
    PatientNotifier.send_question(session.patient_id, session.session_id, question)
  except Exception:
    pass   ← best-effort; session state already saved
```

### 4.3 submit_answer(session_id, patient_id, question_id, answer) → NextQuestion | None

```
Input: session_id, patient_id, question_id, answer

1. InternalStore.get_session(session_id)
   → raise SessionNotFound if None

2. Validate session.patient_id == patient_id
   → raise Unauthorised if mismatch

3. Validate session.status in (PENDING, IN_PROGRESS)
   → raise SessionNotActive if COMPLETED | NO_RESPONSE

4. Validate question_id == session.question_ids[session.current_index]
   → raise WrongQuestion if mismatch

5. Save FeedbackResponse(
     question_id = question_id,
     answer = answer,
     answered_at = now
   )
   InternalStore.save_response(response)

6. Conditional expansion:
   current_q = questions lookup by question_id
   if answer == "YES":
     child = next(q for q in questions if q.conditional_on == question_id, None)
     if child and child.question_id not in session.question_ids:
       session.question_ids.append(child.question_id)

7. session.current_index += 1
   session.status = IN_PROGRESS
   InternalStore.save_session(session)

8. If session.current_index < len(session.question_ids):
     next_q_id = session.question_ids[session.current_index]
     next_q = questions lookup by next_q_id
     _send_question(session, next_q, now)
     Return NextQuestion(
       question_id = next_q.question_id,
       text = next_q.text,
       answer_type = next_q.answer_type
     )

9. Else (all questions answered):
     session.status = COMPLETED
     session.completed_at = now
     InternalStore.save_session(session)
     _create_complaint_if_needed(session)
     Return None
```

### 4.4 _create_complaint_if_needed(session) [internal]

```
1. responses = InternalStore.get_responses(session.session_id)

2. complaint_detail_q = next(
     q for q in questions
     if q.answer_type == FREE_TEXT and q.conditional_on is not None,
     None
   )
   If complaint_detail_q is None: return   ← no complaint question in this question set

3. detail_response = next(
     r for r in responses if r.question_id == complaint_detail_q.question_id,
     None
   )
   If detail_response is None: return      ← Q5 was answered NO; Q6 never asked

4. Check InternalStore.get_complaint_by_session(session.session_id)
   → skip if ComplaintRecord already exists (idempotency guard)

5. Create ComplaintRecord(
     encounter_id = session.encounter_id,
     patient_id = session.patient_id,
     session_id = session.session_id,
     detail = detail_response.answer,
     status = OPEN,
     created_at = now
   )
   InternalStore.save_complaint(complaint)
```

### 4.5 get_feedback_report(encounter_id)

```
Input: encounter_id

1. InternalStore.get_schedule_by_encounter(encounter_id)
   → raise EncounterNotFound if None

2. session = InternalStore.get_session(schedule_id)
3. responses = InternalStore.get_responses(session.session_id) if session else []
4. complaint = InternalStore.get_complaint_by_session(session.session_id) if session else None

5. Return FeedbackReport(
     encounter_id = encounter_id,
     patient_id = schedule.patient_id,
     session_status = session.status if session else None,
     responses = responses,
     complaint = complaint
   )
```

---

## 5. Error Cases

| Scenario | Error Code | Behaviour |
|---|---|---|
| Encounter not found in EMR | `ENCOUNTER_NOT_FOUND` | Raise on `register_encounter` and `get_feedback_report` |
| Schedule already exists for encounter | `DUPLICATE_ENCOUNTER` | Raise on `register_encounter` |
| `submit_answer` — session not found | `SESSION_NOT_FOUND` | Raise |
| `submit_answer` — patient_id mismatch | `UNAUTHORISED` | Raise |
| `submit_answer` — session COMPLETED or NO_RESPONSE | `SESSION_NOT_ACTIVE` | Raise |
| `submit_answer` — wrong question_id for current position | `WRONG_QUESTION` | Raise — patient must answer in order |
| `PatientNotifier.send_question` raises | _(no raise)_ | Best-effort; session state saved; session remains IN_PROGRESS until expiry |

---

## 6. Concurrency and Idempotency

- `evaluate()` session creation guarded by `get_session(schedule_id)` — calling twice at the same `now` creates no duplicate session.
- Conditional Q6 append guarded by `if child.question_id not in session.question_ids` — answering Q5 twice cannot add Q6 twice.
- `submit_answer` index advancement means submitting the same answer twice raises `WRONG_QUESTION` on the second call.
- `_create_complaint_if_needed` guarded by `get_complaint_by_session` check — idempotent if called twice.

---

## 7. Design Patterns

| Pattern | Application |
|---|---|
| Adapter | `EncounterProvider`, `PatientNotifier` — swappable behind ABCs; in-memory stubs for tests |
| Construction-time injection | Question list passed at service construction; no provider adapter needed |
| Evaluate-on-tick | `evaluate(now)` called externally; time injected for deterministic tests |
| Session state machine | `FeedbackSession` status: `PENDING → IN_PROGRESS → COMPLETED / NO_RESPONSE` |
| Conditional question expansion | Q6 appended to `question_ids` dynamically on Q5 = YES, before index advances |
| Best-effort notification | `PatientNotifier` failures caught — session state saved first |

---

## 8. Open Questions

- Should a `NO_RESPONSE` session be retried (resend the following day) or silently closed? Current design: silently closed — schedule marked `COMPLETED`.
- Should NPS and SCALE answers be validated against their range at `submit_answer` time (e.g., reject "11" for NPS)? Current design: no validation — all answers stored as raw strings; analytics pipeline handles type enforcement.
- Should `ComplaintRecord` status (`OPEN` → `RESOLVED`) be manageable via this service, or is resolution out of scope? Current design: out of scope — the service only creates complaint records.
- Should feedback be skipped if the patient has already submitted feedback for this encounter via another channel? Current design: one session per encounter; duplicate feedback not possible through this service.
