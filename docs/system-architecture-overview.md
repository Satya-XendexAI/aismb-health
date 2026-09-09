# MediNexus Hospital WhatsApp Assistant — Full System Walkthrough

Built by reading every file in the codebase end to end. Organized by how a message actually flows through the system, not by directory structure.

---

## 1. Entry points — two ways a message gets in

**`whatsapp.py`** — the real production path. A FastAPI service exposing `GET/POST /webhook` for Meta's Cloud API. `GET` handles Meta's subscription handshake; `POST` verifies the HMAC signature (`X-Hub-Signature-256`), extracts the first text message from the payload, wraps it in a `WAMessage`, and fires it at the orchestrator as a `BackgroundTask` (so Meta gets an immediate `200 OK` while processing happens async). `WhatsAppNotifier.send()` posts replies back through the Graph API, converting `**bold**` to WhatsApp's native `*bold*` on the way out. Known limitation (documented earlier this session): one send attempt, no retry, no delivery-status tracking.

**`interface/app.py`** — local dev/testing only. Same orchestrator, but messages come from a browser-based WhatsApp-mockup UI (`interface/static/`) via `POST /api/send` instead of Meta. `GET /api/config` powers the login screen by listing known doctor/admin numbers from `config/doctors.json`; anyone else logs in with a custom number and becomes a patient. `GET /` serves `index.html` with cache-busted asset URLs (`?v=<mtime>`) so the browser can never serve a stale `app.js`/`style.css`.

Both entry points converge on the exact same object: `WhatsAppOrchestrator(llm, notifier, repository)`.

---

## 2. Identity and role — resolved once, before anything else happens

`orchestrator/session.py`'s `InMemoryRepository.get_role(from_number)` checks the phone number against the admin list, then the doctor list, else `Role.PATIENT`. This runs once per session in `orchestrator/core.py`'s `_hydrate()`, which either loads an existing in-memory `Session` (keyed by `hospital_id:from_number`) or creates a fresh one. **Role is a pure function of phone number, never of message content** — this is the mechanism that closed the "doctor vs patient intent ambiguity" question earlier: a patient and a doctor asking about "appointments with Dr. X" are structurally incapable of getting each other's data, because the tool sets available to each role are entirely different, decided before the LLM sees the text.

---

## 3. The main loop — `orchestrator/core.py`

`handle_message()` wraps everything in a top-level try/except (any unhandled exception falls back to a generic apology, never crashes the process) and delegates to `_handle_message_inner`:

- If `session.state == AWAITING_CONFIRM`, goes to `_handle_awaiting_confirm` (see §5).
- Otherwise, appends the message to history, and `_build_prompt_and_tools()` picks the system prompt + tool schema list based on role: `PATIENT_TOOLS`/`DOCTOR_TOOLS`/`ADMIN_TOOLS` from `orchestrator/schemas.py`. For patients, a `memory_tool` preload injects their profile/family/appointments into the prompt on the first turn needing it (`_preload_memory`), and a warmup mode (`PATIENT_TOOLS_WARMUP`) strips the heavier `appointment` tool for the first couple of turns unless clear booking intent is detected — a cost/latency optimization, not a permission boundary.
- `_react_loop()` then repeatedly calls the LLM (`_llm_call_with_retry`, with exponential backoff on transient 429/503 errors — this is what surfaced the Gemini quota exhaustion earlier this session), executes whatever tool it calls (wrapped in try/except so a tool crash becomes a visible error to the LLM rather than killing the turn), and loops until the model returns plain text or `max_iterations` (5) is hit. Two special-cased tools (`execute_plan`, `report_delay`) break out early into their own confirmation flows via `orchestrator/gates.py`.

---

## 4. The gate — `_gate()`, permission only, never topic

```python
ROLE_PERMISSIONS = {
    "appointment": {PATIENT}, "list_appointments": {PATIENT}, "memory_tool": {PATIENT},
    "query_data": {DOCTOR, ADMIN}, "kg_retriever": {PATIENT, DOCTOR, ADMIN},
    "get_session_impact": {ADMIN}, "find_available_doctors": {ADMIN}, "execute_plan": {ADMIN},
    "report_delay": {DOCTOR},
}
```

Checks whether the resolved role may call the tool the LLM just chose, and separately forces `appointment` calls through a confirmation step. Crucially — and this was the exact gap behind the "off-topic questions" defect — **this only runs when the LLM's response is a tool call.** A plain-text reply (like answering "what is AI?" from general knowledge) never reaches this function at all. That gap is closed by prompt instruction now (§7), not code, because "is this on-topic" isn't structured data a function can validate.

---

## 5. Confirmation gate — booking/cancel, delay reports, admin plans

Three independent AWAITING_CONFIRM flows, all in `_handle_awaiting_confirm`:

- **Single-tool** (`appointment`, via `gates.interrupt_tool`): the LLM's proposed call is parked as `session.pending_tool`, `describe_tool()` renders "Please confirm: BOOK appointment with Dr. X (Dept) for Name on Date". A reply is classified by `orchestrator/utils.py`'s `is_affirmative`/`is_negative` — **exact match only** now (this session's fix; it used to substring-match "book"/"confirm" and misfired on corrections like "book it for tomorrow instead"). Anything that isn't a clean yes/no gets routed back through the normal LLM loop instead of a canned reply, so the model can actually understand and correct the pending request.
- **Delay report** (`gates.interrupt_delay`): a doctor says they're running late → `tools/delay_report.get_delay_preview` computes every waiting patient's new ETA → shown for confirmation → on yes, `execute_delay_report` shifts `doctor_sessions.started_at` and texts each patient.
- **Admin plan** (`gates.interrupt_plan`/`execute_approved_plan`): see §6.

---

## 6. The tools, one by one

- **`tools/appointment/`** (`booking.py` + `database.py`) — the core booking engine, and where most of this session's fixes live: `book()` validates hospital/mode/doctor, **now requires and validates the date** (`DATE_REQUIRED`/`INVALID_DATE` via `date.fromisoformat`), **validates an alternate family-member phone** (10 digits, optional `91` prefix stripped), resolves the patient via `find_family_member` — **phone number alone is the stable identity for "self"**, family members by name+relation — flags a `NAME_MISMATCH` if a differently-named self-claim shows up, checks for an existing WAITING token, then inserts one inside a `SAVEPOINT` so a `UniqueViolation` from the new `uq_tokens_waiting_patient_session` partial index (the race-condition backstop) can be caught and translated into the same `DUPLICATE_BOOKING` shape without losing other work in the same transaction. `cancel()` mirrors the lookup but is deliberately untouched by the date-mandatory rule.
- **`tools/kg/`** (`client.py`/`resolver.py`/`queries.py`/`context.py`, re-exported via `tools/kg_retriever.py`) — doctor search against Neo4j: a small LLM parses free text into structured entities (specialization/name/language/experience, no hardcoded symptom map), fuzzy-resolves specialization strings onto real graph node names, then fuses fulltext + semantic vector search results. Returns a clean `{"found": false, "doctors": []}` shape on no match — which `_react_loop` uses to break a retry storm after 2 consecutive empty searches, a fix from earlier this session.
- **`tools/memory_tool.py`** — one JOIN query pulling a patient's own profile, family members, and active/recent (30-day) appointments, strictly scoped by `requested_by_phone`.
- **`tools/query_data.py`** — text-to-SQL for doctors/admins: generate → validate (two separate LLM calls) → `_is_safe()` blocks anything but `SELECT` and a forbidden-keyword regex → execute with the doctor's own `doctor_id` (or hospital-wide for admin) hard-bound as a parameter, so the generated SQL can never escape its scope regardless of phrasing.
- **`tools/session_impact.py` / `tools/bulk_ops.py` / `workers/notification_worker.py`** — the admin delay-management pipeline: `get_session_impact` (who's waiting, elderly/outstation via a small LLM distance estimate) → `find_available_doctors` (alternates with capacity) → the LLM triages each patient (REASSIGN/SHIFT/RETAIN) → `execute_plan` → `bulk_reschedule` applies all DB changes in one connection (rolls back everything on any failure) → `notify_patients_bulk` fires WhatsApp notifications on a background thread so the admin isn't blocked waiting for every send.

---

## 7. Prompts — `prompts/system.py`, one per role

All three (`PATIENT_SYSTEM_PROMPT`, `DOCTOR_SYSTEM_PROMPT`, `ADMIN_SYSTEM_PROMPT`) now carry the **scope-boundary rule** added this session (redirect off-topic questions instead of answering from general knowledge) — the only guardrail in the whole system that's prompt-only by design, since "is this on-topic" has no structured data for a backend check to validate, unlike date/phone/duplicate-identity, which all got real code-level guarantees. The patient prompt also carries this session's date-collection rules (always ask, never infer from a non-answer like "yes") and the error-code reaction instructions (`INVALID_PHONE`, `NAME_MISMATCH`, `DATE_REQUIRED`, `INVALID_DATE`).

---

## 8. Database — Postgres, 5 core tables

`hospitals` → `doctors` → `doctor_sessions` (one per doctor/day) → `tokens` (queue entries, `WAITING`/`SERVING`/`COMPLETED`/`CANCELLED`) ← `patients` (one row per person, `requested_by_phone` ties every family member back to the WhatsApp number that manages them).

Two partial unique indexes added this session:
- `uq_tokens_waiting_patient_session` — no two WAITING tokens for the same patient+session.
- `uq_patients_self_per_phone` — at most one "self" identity per phone; family members separately unique by phone+name+relation.

ETA is never persisted anywhere — computed fresh at booking time from `doctors.avg_checkin_time`/`avg_consultation_minutes` and the live queue count, shown once in the confirmation, and gone.
