# Post-Visit Follow-up Service — Design Spec

**Date:** 2026-08-17
**Scope:** Backend LLD only — no UI/channel integration
**Status:** Approved

---

## 1. Overview

The Post-Visit Follow-up Service checks patient well-being after a visit, discharge, or procedure. It sends a configurable sequence of follow-up questionnaire rounds at set intervals post-discharge. Each round delivers questions one at a time; when all answers are collected, triage rules are evaluated. Red-flag responses trigger a `NurseTask` and a notification to the task system.

All external dependencies (EMR, questionnaire config, triage rules, WhatsApp, nurse task system) are behind swappable adapter interfaces. In-memory stubs are used for standalone development and testing.

The service is driven by an external caller invoking `evaluate(now)` on a configurable schedule. It holds no internal timer threads.

---

## 2. Architecture

```
┌──────────────────────────────────────────────────────────────┐
│                  PostVisitFollowUpService                     │
│                                                              │
│  + register_encounter(encounter_id) → FollowUpSchedule       │
│  + evaluate(now: datetime) → None              ← tick        │
│  + submit_answer(session_id, patient_id,                     │
│                  question_id, answer) → NextQuestion | None  │
│  + get_followup_report(encounter_id) → FollowUpReport        │
│                                                              │
│  [ adapters injected at construction time ]                  │
└─────────────────┬────────────────────────────────────────────┘
                  │ uses
   ┌──────────────┼────────────────┬──────────────────┐
   ▼              ▼                ▼                  ▼
EncounterProvider  QuestionnaireProvider  TriageRulesProvider
(external — EMR)   (external — mock)      (external — mock)

          ┌────────────────────┬────────────────┐
          ▼                    ▼                ▼
   PatientNotifier        TaskNotifier     InternalStore
   (external — WhatsApp)  (external —      (owned by service)
                           task system)
```

**Components:**

- `EncounterProvider` — fetches encounter/discharge data from EMR. `InMemoryEncounterProvider` returns hardcoded data.
- `QuestionnaireProvider` — returns ordered `Question` list per encounter type. `InMemoryQuestionnaireProvider` for tests.
- `TriageRulesProvider` — returns triage rules per encounter type. Each rule maps a `(question_id, trigger_answer)` pair to an urgency level. `InMemoryTriageRulesProvider` for tests.
- `PatientNotifier` — sends questions to patient one at a time (WhatsApp-targeted, isolated). `InMemoryPatientNotifier` with `should_fail` flag.
- `TaskNotifier` — notifies nurse task system when a `NurseTask` is created. `InMemoryTaskNotifier` with `should_fail` flag.
- `InternalStore` — owns all mutable state: schedules, sessions, answers, nurse tasks. `InMemoryInternalStore` for tests.

`PostVisitFollowUpService` orchestrates all components and holds no mutable state itself.

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
  discharged_at      datetime

Question                                -- from QuestionnaireProvider
  question_id        UUID
  encounter_type     str
  order              int                sequence position (1-based)
  text               str                e.g. "Are you experiencing chest pain?"
  answer_type        Enum               YES_NO | SCALE_1_5 | FREE_TEXT

TriageRule                              -- from TriageRulesProvider
  rule_id            UUID
  encounter_type     str
  question_id        UUID
  trigger_answer     str                e.g. "YES", "4", "5"
  urgency            Enum               LOW | MEDIUM | HIGH | CRITICAL
```

### 3.2 Internal Entities (owned by InternalStore)

```
FollowUpSchedule
  schedule_id             UUID         PK
  encounter_id            UUID         unique
  patient_id              UUID
  hospital_id             UUID
  encounter_type          str
  round_intervals_hours   List[int]    e.g. [24, 72] — hours post-discharge per round
  current_round           int          0-based index; increments when a round session is created
  status                  Enum         ACTIVE | COMPLETED | CANCELLED
  created_at              datetime

QuestionnaireSession
  session_id              UUID         PK
  schedule_id             UUID         FK → FollowUpSchedule
  encounter_id            UUID
  patient_id              UUID
  round_number            int          1-based
  questions               List[UUID]   ordered question_ids for this session
  current_question_index  int          0-based; advances on each answer
  status                  Enum         PENDING | IN_PROGRESS | COMPLETED | NO_RESPONSE
  started_at              datetime?
  completed_at            datetime?
  expires_at              datetime     session created_at + session_expiry_hours (default 12h)

QuestionAnswer
  answer_id          UUID              PK
  session_id         UUID              FK → QuestionnaireSession
  question_id        UUID
  patient_id         UUID
  answer             str
  is_red_flag        bool              False until triage runs on last answer
  answered_at        datetime

NurseTask
  task_id            UUID              PK
  encounter_id       UUID
  patient_id         UUID
  session_id         UUID
  triggered_by       List[UUID]        question_ids that fired red flags
  urgency            Enum              LOW | MEDIUM | HIGH | CRITICAL (highest across rules)
  status             Enum              OPEN | NOTIFIED | CLOSED
  created_at         datetime

NextQuestion                           -- return value of submit_answer when more questions remain
  question_id        UUID
  text               str
  answer_type        Enum

FollowUpReport                         -- return value of get_followup_report
  encounter_id            UUID
  patient_id              UUID
  total_rounds            int
  completed_rounds        int
  no_response_rounds      int
  red_flags_detected      bool
  nurse_task              NurseTask | None
```

**Key rules:**
- One `FollowUpSchedule` per encounter. `register_encounter` twice raises `DUPLICATE_ENCOUNTER`.
- One `QuestionnaireSession` per round. `evaluate()` creates sessions lazily when a round is due.
- `current_question_index` advances per `submit_answer`. When it reaches `len(questions)`, session becomes `COMPLETED` and triage runs immediately.
- `NurseTask.urgency` = highest urgency across all triggered rules. Urgency order: `LOW < MEDIUM < HIGH < CRITICAL`.
- A session expires if `now > expires_at` and status is still `PENDING` or `IN_PROGRESS` — marked `NO_RESPONSE` on next `evaluate()` tick.
- `NurseTask` is created once per session — guarded by checking for an existing task for `session_id` before creating.

---

## 4. Core Flows

### 4.1 register_encounter(encounter_id)

```
Input: encounter_id

1. EncounterProvider.get_encounter(encounter_id)
   → raise EncounterNotFound if None

2. InternalStore.get_schedule_by_encounter(encounter_id)
   → raise DuplicateEncounter if schedule already exists

3. Create FollowUpSchedule(
     encounter_type = encounter.encounter_type,
     round_intervals_hours = [24, 72],   ← configurable at service construction
     current_round = 0,
     status = ACTIVE,
     created_at = now
   )
4. InternalStore.save_schedule(schedule)
5. Return schedule
```

### 4.2 evaluate(now: datetime)

Called by an external scheduler. Processes all ACTIVE schedules.

```
For each ACTIVE FollowUpSchedule:

  Fetch Encounter via encounter_id from EncounterProvider.

  A. Expire stale sessions
     For each session in (PENDING, IN_PROGRESS) where now > session.expires_at:
       session.status = NO_RESPONSE
       InternalStore.save_session(session)

  B. Fire next round if due
     if schedule.current_round < len(schedule.round_intervals_hours):
       interval = schedule.round_intervals_hours[schedule.current_round]
       round_due_at = encounter.discharged_at + timedelta(hours=interval)

       if round_due_at <= now:
         existing = InternalStore.get_session_by_round(schedule_id, schedule.current_round + 1)
         if existing is None:
           questions = QuestionnaireProvider.get_questions(encounter.encounter_type)
           → raise NoQuestionsConfigured if list is empty
           Create QuestionnaireSession(
             round_number = schedule.current_round + 1,
             questions = [q.question_id for q in questions],
             current_question_index = 0,
             status = PENDING,
             expires_at = now + timedelta(hours=session_expiry_hours)
           )
           InternalStore.save_session(session)
           schedule.current_round += 1
           InternalStore.save_schedule(schedule)
           _send_question(session, questions[0], now)

  C. Mark schedule COMPLETED when all rounds done
     if schedule.current_round == len(schedule.round_intervals_hours):
       last_session = InternalStore.get_last_session(schedule_id)
       if last_session and last_session.status in (COMPLETED, NO_RESPONSE):
         schedule.status = COMPLETED
         InternalStore.save_schedule(schedule)

_send_question(session, question, now):
  session.started_at = session.started_at or now
  session.status = IN_PROGRESS
  InternalStore.save_session(session)
  try:
    PatientNotifier.send_question(session.patient_id, session.session_id, question)
  except Exception:
    pass  ← best-effort; session state already saved
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

4. Validate question_id == session.questions[session.current_question_index]
   → raise WrongQuestion if mismatch

5. Create QuestionAnswer(
     question_id=question_id, answer=answer,
     is_red_flag=False, answered_at=now
   )
   InternalStore.save_answer(answer_record)

6. session.current_question_index += 1
   session.status = IN_PROGRESS
   InternalStore.save_session(session)

7. If session.current_question_index < len(session.questions):
     next_q_id = session.questions[session.current_question_index]
     next_q = QuestionnaireProvider.get_question(next_q_id)
     _send_question(session, next_q, now)
     Return NextQuestion(question_id=next_q_id, text=next_q.text, answer_type=next_q.answer_type)

8. Else (all questions answered):
     session.status = COMPLETED
     session.completed_at = now
     InternalStore.save_session(session)
     _run_triage(session)
     Return None
```

### 4.4 _run_triage(session) [internal]

```
1. answers = InternalStore.get_answers(session.session_id)
2. rules = TriageRulesProvider.get_rules(encounter.encounter_type)

3. red_flag_rules = [
     rule for rule in rules
     if any(
       a.question_id == rule.question_id and a.answer == rule.trigger_answer
       for a in answers
     )
   ]

4. If red_flag_rules is empty: return  ← no action

5. Mark red-flag answers:
   For each answer in answers:
     if any(r.question_id == answer.question_id for r in red_flag_rules):
       answer.is_red_flag = True
       InternalStore.save_answer(answer)

6. urgency = max(red_flag_rules, key=lambda r: URGENCY_ORDER[r.urgency]).urgency
   URGENCY_ORDER = {LOW: 0, MEDIUM: 1, HIGH: 2, CRITICAL: 3}

7. Check no existing NurseTask for this session_id → skip if already created

8. Create NurseTask(
     triggered_by=[r.question_id for r in red_flag_rules],
     urgency=urgency,
     status=OPEN,
     created_at=now
   )
   InternalStore.save_task(task)

9. try:
     TaskNotifier.notify_task_created(task)
     task.status = NOTIFIED
     InternalStore.save_task(task)
   except Exception:
     pass  ← task saved as OPEN; notification retry is the task system's responsibility
```

### 4.5 get_followup_report(encounter_id)

```
Input: encounter_id

1. InternalStore.get_schedule_by_encounter(encounter_id)
   → raise EncounterNotFound if None

2. sessions = InternalStore.get_sessions(schedule_id)
3. nurse_task = InternalStore.get_task_by_encounter(encounter_id)

4. Return FollowUpReport(
     encounter_id = encounter_id,
     patient_id = schedule.patient_id,
     total_rounds = len(schedule.round_intervals_hours),
     completed_rounds = count(s for s in sessions if s.status == COMPLETED),
     no_response_rounds = count(s for s in sessions if s.status == NO_RESPONSE),
     red_flags_detected = nurse_task is not None,
     nurse_task = nurse_task,
   )
```

---

## 5. Error Cases

| Scenario | Error Code | Behaviour |
|---|---|---|
| Encounter not found in EMR | `ENCOUNTER_NOT_FOUND` | Raise on `register_encounter` and `get_followup_report` |
| Schedule already exists for encounter | `DUPLICATE_ENCOUNTER` | Raise on `register_encounter` |
| No questions configured for encounter type | `NO_QUESTIONS_CONFIGURED` | Raise inside `evaluate()` on session creation |
| `submit_answer` — session not found | `SESSION_NOT_FOUND` | Raise |
| `submit_answer` — patient_id mismatch | `UNAUTHORISED` | Raise |
| `submit_answer` — session COMPLETED or NO_RESPONSE | `SESSION_NOT_ACTIVE` | Raise |
| `submit_answer` — wrong question_id for current position | `WRONG_QUESTION` | Raise — patient must answer in order |
| `PatientNotifier.send_question` raises | _(no raise)_ | Best-effort; session state saved; patient misses question but session remains IN_PROGRESS |
| `TaskNotifier.notify_task_created` raises | _(no raise)_ | `NurseTask` saved with status `OPEN`; retry is task system's responsibility |

---

## 6. Concurrency and Idempotency

**evaluate() idempotency:**
- Session creation guarded by `get_session_by_round(schedule_id, round_number)` — calling `evaluate()` twice at the same `now` creates no duplicate sessions.
- Session expiry is idempotent — marking `NO_RESPONSE` twice is a no-op.

**submit_answer idempotency:** Submitting the same answer twice raises `WRONG_QUESTION` on the second call (index has already advanced).

**_run_triage idempotency:** `NurseTask` creation guarded by checking for existing task for `session_id` — triage called twice creates only one task.

---

## 7. Design Patterns

| Pattern | Application |
|---|---|
| Adapter | `EncounterProvider`, `QuestionnaireProvider`, `TriageRulesProvider`, `PatientNotifier`, `TaskNotifier` — all swappable behind ABCs |
| Evaluate-on-tick | `evaluate(now)` called externally; time injected for deterministic tests |
| Session state machine | `QuestionnaireSession` status: `PENDING → IN_PROGRESS → COMPLETED / NO_RESPONSE` |
| Lazy session creation | Sessions created only when their round interval is due, not precomputed |
| Best-effort notifications | `PatientNotifier` and `TaskNotifier` failures are caught and logged — service state is saved first |

---

## 8. Open Questions

- Should a `NO_RESPONSE` round block subsequent rounds (patient clearly disengaged), or should all rounds fire regardless? Current design: rounds fire on schedule regardless of prior `NO_RESPONSE`.
- Should the attender be able to manually close a `NurseTask` via this service, or is that entirely the task system's responsibility? Current design: task closure is out of scope — this service only creates tasks.
- If `PatientNotifier` fails to send a question, should `evaluate()` retry on the next tick? Current design: no retry — session remains `IN_PROGRESS` until it expires.
