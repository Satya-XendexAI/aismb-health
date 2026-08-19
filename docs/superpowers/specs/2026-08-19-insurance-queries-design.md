# Insurance Queries Service — Design Spec

**Date:** 2026-08-19
**Scope:** Backend LLD only — no UI/channel integration
**Status:** Approved

---

## 1. Overview

The Insurance Queries Service answers patient questions about health insurance via WhatsApp. A Q&A dataset is loaded from a local CSV file at construction time. When a patient sends a query, the service keyword-scores all dataset questions against the query, returns the best-matching answer, and sends it back via WhatsApp. If no question matches, a configurable fallback text is returned.

All Q&A data lives in memory. Only query logs touch the DB.

---

## 2. Architecture

```
┌──────────────────────────────────────────────────────────┐
│               InsuranceQueryService                       │
│                                                          │
│  + handle_query(patient_id, hospital_id, query_text)     │
│                              → InsuranceAnswer           │
│                                                          │
│  [ dataset_path + notifier + fallback_text injected      │
│    at construction; Q&A pairs loaded into memory on init ]│
└─────────────────┬────────────────────────────────────────┘
                  │ uses
         ┌────────┴──────────┐
         ▼                   ▼
  PatientNotifier        Repository
  (external — WhatsApp)  (SQL DB — query_records)
```

**Components:**

- `PatientNotifier` — sends the answer back to the patient via WhatsApp. `InMemoryPatientNotifier` with `should_fail` flag for tests.
- `Repository` — writes `query_records` to SQL DB.
- **In-memory Q&A list** — loaded once at construction by reading `dataset_path` (local CSV file), parsing `Question` and `Answer` columns. Raises at construction if the file is not found or the list is empty.

`InsuranceQueryService` holds the in-memory Q&A list as read-only state. No mutable state.

---

## 3. Data Models

### 3.1 In-Memory (loaded at construction)

```
QAPair                                   -- parsed from local CSV file
  question    str                        dataset question — used for keyword matching
  answer      str                        answer text → sent as WhatsApp message body
```

Local file: `data/HealthInsurance_Dataset_v2.csv` (downloaded from HuggingFace and stored with the project).

Columns used: `Question`, `Answer`. Columns ignored: `Context`, `Answer_Start_Sequence`, `Answers`.

### 3.2 DB Table

```
query_records                            -- written by the service; one per patient query
  record_id      UUID        PK
  patient_id     UUID
  hospital_id    UUID
  query_text     str                    raw patient query
  matched_question  str?               the dataset question that matched; None if fallback
  answer_text    str                    the answer that was sent
  responded_at   datetime
```

---

## 4. Core Flows

### 4.1 Construction

```
__init__(dataset_path, notifier, repository, fallback_text):

1. open(dataset_path) → parse CSV
   → raise DatasetLoadError if file not found

2. For each row:
     qa = { question: row["Question"], answer: row["Answer"] }
     self.qa_pairs.append(qa)

3. if self.qa_pairs is empty:
   → raise DatasetLoadError("No Q&A pairs loaded")
```

### 4.2 _find_best_match(query_text) → QAPair | None [internal]

```
1. query_words = set(query_text.lower().split())

2. scored = [
     (qa, score) for qa in self.qa_pairs
     where score = count of query_words that appear in qa.question.lower()
   ]

3. best = max(scored, key=lambda x: x[1])

4. if best.score > 0:
     return best.qa
   return None             ← no match; caller uses fallback
```

### 4.3 handle_query(patient_id, hospital_id, query_text) → InsuranceAnswer

```
Input: patient_id, hospital_id, query_text

1. match = _find_best_match(query_text)

2. if match:
     answer_text      = match.answer
     matched_question = match.question
   else:
     answer_text      = self.fallback_text
     matched_question = None

3. INSERT INTO query_records(
     patient_id, hospital_id,
     query_text, matched_question,
     answer_text, responded_at = now
   )

4. try:
     PatientNotifier.send_response(patient_id, answer_text)
   except Exception:
     pass   ← best-effort; query already logged

5. Return InsuranceAnswer(
     answer_text      = answer_text,
     matched_question = matched_question   ← None if fallback
   )
```

---

## 5. Error Cases

| Scenario | Error Code | Behaviour |
|---|---|---|
| Local file not found at `dataset_path` | `DatasetLoadError` | Raised at construction — service cannot be created |
| File is empty (zero rows) | `DatasetLoadError` | Raised at construction |
| No question matches the query | _(no raise)_ | `fallback_text` returned and logged; `matched_question = None` |
| `PatientNotifier.send_response` raises | _(no raise)_ | Best-effort; query already logged |

---

## 6. Design Patterns

| Pattern | Application |
|---|---|
| In-memory dataset | Q&A pairs read from local CSV at construction; no DB table needed |
| Keyword retrieval | Query split into words; each dataset question scored by word overlap; top match returned |
| Graceful degradation | No match → configurable `fallback_text`; patient always receives a response |
| Fail-fast construction | Raises at init if CSV cannot be loaded — no silent empty-dataset state |
| Request-response | `handle_query` is synchronous and patient-initiated — no tick or manual trigger |
| Audit log | Every query and its answer logged in `query_records` regardless of match or fallback |
