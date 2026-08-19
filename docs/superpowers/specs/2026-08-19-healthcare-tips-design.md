# Healthcare Tips & Wellness Service — Design Spec

**Date:** 2026-08-19
**Scope:** Backend LLD only — no UI/channel integration
**Status:** Approved

---

## 1. Overview

The Healthcare Tips & Wellness Service sends educational health tips to patients via WhatsApp. Tips are sourced from a healthcare Q&A dataset (JSONL format) and pre-loaded into the DB. When triggered manually, the service picks a random tip for each patient and sends the `completion` text as the WhatsApp message body.

All tip content and patient data is managed directly in the DB. State is persisted in SQL via a `Repository` layer.

---

## 2. Architecture

```
┌─────────────────────────────────────────────────────────┐
│             HealthcareTipsService                        │
│                                                         │
│  + send_tips() → TipsSummary        ← manual call       │
│                                                         │
│  [ notifier injected at construction ]                  │
└──────────────┬──────────────────────────────────────────┘
               │ uses
         ┌─────┴──────────────┐
         ▼                    ▼
  TipsNotifier            Repository
  (external — WhatsApp)   (SQL DB — tips + patients
                            + tip_records)

── one-time setup ──────────────────────────────────────────
load_tips_from_dataset(url, db)   ← standalone utility function
  Fetches JSONL from URL, parses each line,
  bulk-inserts into tips table. Run once by admin.
```

**Components:**

- `TipsNotifier` — sends WhatsApp messages. `InMemoryTipsNotifier` with `should_fail` flag for tests.
- `Repository` — reads `tips` and `patients`, writes `tip_records` to SQL DB.
- `load_tips_from_dataset` — standalone utility (not part of the service class); fetches the JSONL dataset from a URL, parses each `{ prompt, completion }` record, and bulk-inserts into the `tips` table. Idempotent via `ON CONFLICT DO NOTHING` on `(prompt)`.

`HealthcareTipsService` holds no mutable state itself.

---

## 3. Data Models

### 3.1 DB Tables

```
tips                                     -- pre-loaded from JSONL dataset (one-time setup)
  tip_id         UUID        PK
  prompt         str         UNIQUE      the original healthcare question
  completion     str                     the answer/tip text — sent as WhatsApp message body
  loaded_at      datetime

patients                                 -- populated and managed by backend
  patient_id     UUID        PK
  hospital_id    UUID
  name           str
  phone          str

tip_records                              -- written by the service; one per patient per trigger
  record_id      UUID        PK
  patient_id     UUID
  tip_id         UUID        FK → tips
  triggered_at   datetime               when send_tips() was called
  status         Enum        SENT | FAILED
  fired_at       datetime
```

**Tip selection:** Random — `SELECT * FROM tips ORDER BY RANDOM() LIMIT 1` per patient. No deduplication across triggers in the current scope.

---

## 4. Core Flows

### 4.1 load_tips_from_dataset(url, db) — one-time setup utility

```
1. GET url  → stream JSONL response

2. For each line:
     record = json.parse(line)        → { prompt, completion }

3. Bulk INSERT INTO tips(prompt, completion, loaded_at = now)
   ON CONFLICT (prompt) DO NOTHING    ← idempotent; safe to re-run
```

### 4.2 send_tips() → TipsSummary

```
1. Fetch all patients:
   SELECT patient_id FROM patients

2. For each patient_id:

     a. Select a random tip:
        SELECT tip_id, completion FROM tips ORDER BY RANDOM() LIMIT 1

     b. status = 'SENT'
        try:
          TipsNotifier.send_tip(
            patient_id = patient_id,
            message    = tip.completion       ← completion text sent as-is
          )
        except Exception:
          status = 'FAILED'

     c. INSERT INTO tip_records(
          patient_id, tip_id, triggered_at = now, status, fired_at = now
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
| No tips loaded in DB | _(no raise)_ | `send_tips()` skips all patients — no tips to send; returns `TipsSummary(total=N, sent=0, failed=0)` |
| `TipsNotifier.send_tip` raises | _(no raise)_ | `tip_records.status = FAILED`; continues to next patient |
| `load_tips_from_dataset` — HTTP error | raises | Caller handles; no partial inserts committed on failure |

---

## 6. Design Patterns

| Pattern | Application |
|---|---|
| DB-managed data | Tips pre-loaded into DB; patients managed by backend — service reads both directly |
| Manual trigger | `send_tips()` called on demand — no scheduler or tick needed |
| One-time setup utility | `load_tips_from_dataset` is a standalone function run by admin; decoupled from the service class |
| Random selection | `ORDER BY RANDOM() LIMIT 1` per patient — simple, no state needed |
| Fire-and-forget | Reminders best-effort; FAILED rows logged for visibility |
