# Missing Domain/Scope Boundary — Implementation Plan

**Problem:** Nothing anywhere in the system tells the model it may *only* discuss hospital/healthcare/appointment topics. When a message doesn't match any FLOW trigger in the prompt, `tool_choice="auto"` lets the model skip every tool and answer directly from its own general knowledge — which is what happened with "what is artificial intelligence." `_gate()` only checks *tool-call permissions by role*; it never runs at all for a plain-text response, so there's no checkpoint downstream either.

**Fix:** Add an explicit scope boundary to the system prompts — a general rule, not a topic blocklist — so the model redirects anything outside the hospital domain instead of answering it.

**Files touched:** 1 existing file (`prompts/system.py`), all three prompts. No schema, no backend, no new files.

---

## Why this is prompt-only, and deliberately not a backend check

Every other fix in this session that got a deterministic backend layer had *structured data* to validate — a date string, a phone number, an existence check. "Is this message about the hospital" has no such structure; it's free-text topic judgment, the same category as "is 'sometime next week' vague" and "does 'yes' actually answer the date question" — both of which were scoped as prompt-only for the same reason. A hardcoded keyword/regex list ("block messages containing 'AI', 'weather', 'politics'...") would be exactly the kind of brittle matching that caused the real `is_affirmative("book" in text)` bug earlier this session, except worse — topics are unbounded, a fixed list can never be complete, and it would misfire on legitimate hospital questions that happen to share a word with a blocked topic.

**A stronger option exists** (noted here, not implemented by default): a second, cheap LLM call that classifies "is this reply actually on-topic" before it's sent — the same two-call generate→validate pattern already used in `tools/query_data.py` (`sql_generate` + `sql_validate`). That would give a real enforced checkpoint instead of relying purely on the main model's compliance. It's deliberately left as an optional Phase 2 here, not the default: it adds an extra API call (and extra latency) on every single turn, and the Gemini quota is already under pressure from this session's testing. Worth doing later if the prompt-only version isn't reliable enough in practice — not worth the added cost/latency preemptively.

---

## `prompts/system.py` — one rule added to each of the three prompts

### `PATIENT_SYSTEM_PROMPT` — add as CORE RULE 7, after rule 6 (line 11)

```python
"7. STAY IN SCOPE: Only answer questions about this hospital, healthcare, doctors, "
"symptoms, or appointments. For anything else (general knowledge, unrelated topics), "
"don't answer it — politely say you're a hospital assistant and redirect: ask how you "
"can help with their health or appointment needs instead.\n\n"
```

### `DOCTOR_SYSTEM_PROMPT` — add after the existing "cannot book or cancel" line (line 136-137)

```python
"You cannot book or cancel appointments — that is handled by patients directly. "
"Be concise and professional. Use tools to retrieve accurate data. Never fabricate information.\n"
"Only answer questions related to this hospital's operations, patients, or doctors — "
"for anything else, decline and redirect back to what you can help with.\n\n"
```

### `ADMIN_SYSTEM_PROMPT` — add after the opening line (line 161)

```python
"You are MediNexus Admin Assistant. You help hospital administrators manage delays and patient flow.\n"
"Only answer questions related to hospital administration, delays, and patient flow — "
"for anything else, decline and redirect back to what you can help with.\n"
```

---

## Why this shape

- **A principle, not a list.** "Only answer questions about this hospital..." is a general boundary the model applies to *any* off-topic message, the same way "a date must come from the patient's own response" (the previous fix) covered every non-answer phrasing without enumerating them. Nothing to maintain as new off-topic subjects come up.
- **Applied to all three roles, not just patients** — the same architectural gap (`tool_choice="auto"` + no scope check anywhere downstream) exists identically for `DOCTOR_SYSTEM_PROMPT` and `ADMIN_SYSTEM_PROMPT`, which were both confirmed to have zero scope-boundary language either.
- **Honest about the ceiling.** Like the "yes as date" fix, this reduces the failure rate but cannot mathematically guarantee it, because the decision is still made by the model on every single turn. If that's not good enough once tested, Phase 2 (the validation-call pattern) is the documented next step — not a new idea invented after the fact, but scoped here up front.

---

## Verification (needs the live LLM — this is a judgment behavior, not something the backend alone can confirm)

- [ ] "What is artificial intelligence?" → bot declines and redirects to hospital help, does not answer the AI question.
- [ ] "What's the weather today?" → same, declines and redirects.
- [ ] A genuine hospital question in the same conversation right after ("what are your visiting hours?") → still answered normally — confirms the rule doesn't over-trigger on real hospital topics.
- [ ] Existing booking/cancel/symptom flows unaffected — this only adds a new rule, doesn't change any existing FLOW instruction.
- [ ] Doctor role: an off-topic question from a doctor's registered number → also declined and redirected.
