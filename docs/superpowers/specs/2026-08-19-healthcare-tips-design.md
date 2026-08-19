# Healthcare Tips & Wellness Service — Design Spec

**Date:** 2026-08-19 (revised 2026-08-19)
**Scope:** Backend LLD only — no UI/channel integration
**Status:** Approved

---

## 1. Overview

The Healthcare Tips & Wellness Service sends educational health tips to patients via WhatsApp. Tips are loaded into memory at construction time by reading a local JSONL file. When triggered manually, the service picks a random tip for each patient and sends the `completion` text as the WhatsApp message body.

No DB table is used for tips — they live entirely in memory. Only patient and delivery records touch the DB.

---

## 2. Architecture

```
┌─────────────────────────────────────────────────────────┐
│             HealthcareTipsService                        │
│                                                         │
│  + send_tips() → TipsSummary        ← manual call       │
│                                                         │
│  [ dataset_path + notifier injected at construction;    │
│    tips loaded from local JSONL file into memory on init]│
└──────────────┬──────────────────────────────────────────┘
               │ uses
         ┌─────┴──────────────┐
         ▼                    ▼
  TipsNotifier            Repository
  (external — WhatsApp)   (SQL DB — patients
                            + tip_records)
```

**Components:**

- `TipsNotifier` — sends WhatsApp messages. `InMemoryTipsNotifier` with `should_fail` flag for tests.
- `Repository` — reads `patients`, writes `tip_records` to SQL DB.
- **In-memory tips list** — loaded once at construction by reading `dataset_path` (local file) and parsing each JSONL line into `{ prompt, completion }`. Raises at construction if the file is not found or the list is empty.

`HealthcareTipsService` holds the in-memory tips list as read-only state set at construction. No mutable state.

---

## 3. Data Models

### 3.1 In-Memory (loaded at construction)

```
Tip                                      -- parsed from local JSONL file
  prompt      str                        the original healthcare question (not sent)
  completion  str                        the tip text → sent as WhatsApp message body
```

Local file: `data/healthcare_qa_dataset.jsonl` (downloaded from HuggingFace and stored with the project).

Each line is a JSON object `{ "prompt": "...", "completion": "..." }`.

### 3.2 DB Tables

```
patients                                 -- populated and managed by backend
  patient_id     UUID        PK
  hospital_id    UUID
  name           str
  phone          str

tip_records                              -- written by the service; one per patient per trigger
  record_id      UUID        PK
  patient_id     UUID
  tip_text       str                    the completion text that was sent
  triggered_at   datetime               when send_tips() was called
  status         Enum        SENT | FAILED
  fired_at       datetime
```

---

## 4. Core Flows

### 4.1 Construction

```
__init__(dataset_path, notifier, repository):

1. open(dataset_path)
   → raise DatasetLoadError if file not found

2. For each line:
     tip = json.parse(line)   → { prompt, completion }
     self.tips.append(tip)

3. if self.tips is empty:
   → raise DatasetLoadError("No tips loaded")
```

### 4.2 send_tips() → TipsSummary

```
1. Fetch all patients:
   SELECT patient_id FROM patients

2. For each patient_id:

     a. tip = random.choice(self.tips)

     b. status = 'SENT'
        try:
          TipsNotifier.send_tip(
            patient_id = patient_id,
            message    = tip.completion       ← completion text sent as-is
          )
        except Exception:
          status = 'FAILED'

     c. INSERT INTO tip_records(
          patient_id, tip_text = tip.completion,
          triggered_at = now, status, fired_at = now
        )

3. Return TipsSummary(
     total_patients = count of patients processed,
     sent           = count where status = SENT,
     failed         = count where status = FAILED
   )
```

---

## 5. Error Cases

| Scenario | Error Code | Behaviour |
|---|---|---|
| Local file not found at `dataset_path` | `DatasetLoadError` | Raised at construction — service cannot be created |
| File is empty (zero lines) | `DatasetLoadError` | Raised at construction |
| `TipsNotifier.send_tip` raises | _(no raise)_ | `tip_records.status = FAILED`; continues to next patient |

---

## 6. Design Patterns

| Pattern | Application |
|---|---|
| In-memory dataset | Tips read from local JSONL file at construction; no DB table needed |
| Manual trigger | `send_tips()` called on demand — no scheduler or tick needed |
| Fail-fast construction | Service raises at init if dataset cannot be loaded — no silent empty-tip state |
| Random selection | `random.choice(self.tips)` per patient — simple, stateless |
| Fire-and-forget | Delivery best-effort; FAILED rows logged for visibility |
