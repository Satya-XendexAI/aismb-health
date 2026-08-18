# Patient Feedback Collection Service — Design Spec

**Date:** 2026-08-18 (revised 2026-08-18 ×2)
**Scope:** Backend LLD only — no UI/channel integration
**Status:** Approved

---

## 1. Overview

The Patient Feedback Collection Service is triggered manually (e.g., by a staff member or admin action). When triggered, it finds all completed encounters with no feedback session yet, creates a session for each, and sends the first question to the patient via WhatsApp.

Patients answer questions one at a time by calling `submit_answer`. Answers are stored as raw strings. The question list is a fixed constant — no injection or configuration needed.

Encounter data is managed directly in the DB from the backend. State is persisted in SQL via a `Repository` layer.

---

## 2. Architecture

```
┌──────────────────────────────────────────────────────────┐
│                PatientFeedbackService                     │
│                                                          │
│  + send_feedback_requests() → FeedbackSummary ← manual  │
│  + submit_answer(session_id, patient_id,                 │
│                  question_id, answer) → NextQuestion | None│
│                                                          │
│  [ notifier injected at construction ]                   │
└─────────────────┬────────────────────────────────────────┘
                  │ uses
         ┌────────┴──────────┐
         ▼                   ▼
  PatientNotifier        Repository
  (external — WhatsApp)  (SQL DB — encounters
                           + feedback_sessions
                           + feedback_responses)
```

**Components:**

- `PatientNotifier` — sends questions to patients via WhatsApp. `InMemoryPatientNotifier` with `should_fail` flag for tests.
- `Repository` — reads `encounters`, writes `feedback_sessions` and `feedback_responses` to SQL DB.

`PatientFeedbackService` holds no mutable state itself.

---

## 3. Data Models

### 3.1 DB Tables

```
encounters                               -- populated and managed by backend
  encounter_id       UUID        PK
  patient_id         UUID
  hospital_id        UUID
  encounter_type     str               e.g. "OPD", "SURGERY", "DISCHARGE"
  visit_ended_at     datetime
  status             Enum              ONGOING | COMPLETED

feedback_sessions                        -- one per encounter
  session_id         UUID        PK
  encounter_id       UUID        UNIQUE
  patient_id         UUID
  hospital_id        UUID
  current_index      int               0-based; advances per answer
  status             Enum              IN_PROGRESS | COMPLETED
  created_at         datetime

feedback_responses                       -- one per answer
  response_id        UUID        PK
  session_id         UUID        FK → feedback_sessions
  question_id        str               stable question identifier (e.g. "Q1", "Q2")
  patient_id         UUID
  answer             str               raw string; all answer types stored as str
  answered_at        datetime
```

**Idempotency key:** `encounter_id` on `feedback_sessions` (UNIQUE constraint) — creating a session twice for the same encounter is blocked at the DB level.

### 3.2 Question List (module-level constant)

```
QUESTIONS = [
  { id: "Q1", text: "How likely are you to recommend us? (0–10)",   answer_type: NPS_0_10  },
  { id: "Q2", text: "How satisfied were you overall? (1–5)",         answer_type: SCALE_1_5 },
  { id: "Q3", text: "How would you rate your doctor? (1–5)",         answer_type: SCALE_1_5 },
  { id: "Q4", text: "How would you rate our facilities? (1–5)",      answer_type: SCALE_1_5 },
  { id: "Q5", text: "Any comments or suggestions?",                  answer_type: FREE_TEXT },
]
```

---

## 4. Core Flows

### 4.1 send_feedback_requests() → FeedbackSummary

```
1. Find completed encounters with no feedback session:
   SELECT encounter_id, patient_id, hospital_id FROM encounters
   WHERE status = 'COMPLETED'
     AND encounter_id NOT IN (
       SELECT encounter_id FROM feedback_sessions
     )

2. For each encounter:

     a. INSERT INTO feedback_sessions(
          encounter_id, patient_id, hospital_id,
          current_index = 0, status = IN_PROGRESS, created_at = now
        )

     b. sent = True
        try:
          PatientNotifier.send_question(patient_id, session_id, QUESTIONS[0])
        except Exception:
          sent = False

3. Return FeedbackSummary(
     total   = count of encounters processed,
     sent    = count where notification succeeded,
     failed  = count where notification failed
   )
```

### 4.2 submit_answer(session_id, patient_id, question_id, answer) → NextQuestion | None

```
Input: session_id, patient_id, question_id, answer

1. SELECT * FROM feedback_sessions WHERE session_id = ?
   → raise SessionNotFound if None

2. Validate session.patient_id == patient_id
   → raise Unauthorised if mismatch

3. Validate session.status == IN_PROGRESS
   → raise SessionNotActive if COMPLETED

4. expected_question = QUESTIONS[session.current_index]
   Validate question_id == expected_question.id
   → raise WrongQuestion if mismatch

5. INSERT INTO feedback_responses(
     session_id, question_id, patient_id, answer, answered_at = now
   )

6. UPDATE feedback_sessions SET current_index = current_index + 1

7. If current_index < len(QUESTIONS):
     next_q = QUESTIONS[current_index]
     try:
       PatientNotifier.send_question(patient_id, session_id, next_q)
     except Exception:
       pass   ← best-effort
     Return NextQuestion(id=next_q.id, text=next_q.text, answer_type=next_q.answer_type)

8. Else (all questions answered):
     UPDATE feedback_sessions SET status = COMPLETED
     Return None
```

---

## 5. Error Cases

| Scenario | Error Code | Behaviour |
|---|---|---|
| `submit_answer` — session not found | `SESSION_NOT_FOUND` | Raise |
| `submit_answer` — patient_id mismatch | `UNAUTHORISED` | Raise |
| `submit_answer` — session already COMPLETED | `SESSION_NOT_ACTIVE` | Raise |
| `submit_answer` — wrong question_id for current position | `WRONG_QUESTION` | Raise |
| `PatientNotifier.send_question` raises | _(no raise)_ | Best-effort; response already saved |

---

## 6. Design Patterns

| Pattern | Application |
|---|---|
| DB-managed data | Encounters created and managed by backend; service reads them directly |
| Manual trigger | `send_feedback_requests()` called on demand — no scheduler or tick needed |
| Fixed question list | Questions are a module-level constant — no injection or provider needed |
| Idempotency | `UNIQUE (encounter_id)` on `feedback_sessions` prevents duplicate sessions |
| Fire-and-forget | `PatientNotifier` failures caught — response already saved before send attempt |
