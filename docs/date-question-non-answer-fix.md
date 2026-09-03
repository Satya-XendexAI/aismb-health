# "Yes" Interpreted As a Date — Implementation Plan

**Problem:** When the bot asks "which date would you like?", a non-answer like "Yes" can get lazily interpreted by the LLM as agreement to book today, producing `date=2026-09-01` — a perfectly valid, real date, just not one the patient actually chose.

**Why this can't be a backend check** (unlike `DATE_REQUIRED`/`INVALID_DATE`): by the time `book()` runs, a genuinely-stated "today" and an LLM-guessed "today" are the identical valid ISO string — there's no signal left to tell them apart. This is a prompt-compliance gap, not a validation gap, so the fix lives entirely in `prompts/system.py`. No schema or backend change.

**Deliberately not doing:** adding a keyword/regex heuristic in code to detect "was the reply just a bare yes-word" (e.g. checking if the raw message was "yes"/"ok"/etc. before accepting a same-day date). That's the exact class of fragile substring/keyword matching that caused the real `is_affirmative("book" in text)` bug fixed earlier this session — it would need the raw conversational text threaded all the way into `booking.py` just to catch one narrow phrasing, while still being wrong for plenty of legitimate exchanges (e.g. "Want today?" / "Yes" is a perfectly valid real answer in a different flow). Not worth the fragility for a gap that's fundamentally about LLM judgment, not data validation.

**Files touched:** 1 existing file, no new files, no schema/DB changes.

---

## `prompts/system.py` — reinforce what counts as actually answering the date question

Same block already updated for the mandatory-date fix (the `"→ Collect: name, preferred doctor, date..."` line). Add one more sentence right after it:

```python
"→ Collect: name, preferred doctor, date (ONLY these). ALWAYS ask which date "
"they want — 'today', 'tomorrow', a specific date, or a specific weekday all "
"count; convert whatever they say into YYYY-MM-DD yourself using today's date "
"above. Never assume today without asking. If what they say is vague (e.g. "
"'sometime next week', 'soon'), don't guess a date — ask them to be specific. "
"A date must come from the patient's own response — never invent, infer, or "
"default to a date from a reply that doesn't actually specify one (e.g. "
"'yes', 'ok', 'sure', 'fine', 'whatever that works'). If their reply doesn't "
"contain an actual date, ask again.\n"
```

Deliberately phrased as a general rule ("must come from their response") rather than a fixed list of non-answer words — a list would need updating every time someone finds a new phrasing ("okay then," "sounds good," etc.), while the rule itself covers all of them the same way. This mirrors the same reasoning that ruled out a code-level keyword check: enumerating phrases is brittle regardless of which layer it lives in, so neither the prompt nor the code should do it — the LLM's own judgment about "did this actually name a date" is what has to carry the weight here.

---

## Why this is the right (and honest) scope

- **No hardcoded phrase list to match against** — this is guidance for the LLM's own judgment about what counts as "actually answering," not a keyword filter running in code.
- **Consistent with the existing two-part design**: the deterministic layer (`DATE_REQUIRED`/`INVALID_DATE` in `booking.py`) still guarantees no booking can happen with a missing or fake date; this prompt line targets the one gap that layer structurally cannot close — a *valid* date the patient didn't actually choose.
- **This is mitigation, not elimination.** Same honest framing as the original "always ask" instruction: it reduces how often this happens, it cannot make it impossible, because nothing downstream of the LLM's decision can verify intent behind a syntactically valid date. Worth saying plainly rather than overselling.

---

## Verification (requires the LLM — can't be tested against the backend alone, since the bug lives in interpretation, not validation)

- [ ] Bot asks "which date would you like?" → patient replies "Yes" → bot should ask again for a specific date, not book for today.
- [ ] Same question → patient replies "today" → books today (a real answer, should still work).
- [ ] Same question → patient replies "sure, tomorrow" → resolves to tomorrow's date (not blocked just because "sure" is present — the sentence still contains an actual date).
- [ ] Confirm the mandatory-date and identity-mismatch fixes still work unaffected (this change touches only the collect-info instruction, nothing shared with those checks).
