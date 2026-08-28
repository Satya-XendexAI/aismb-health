# Conversational Warmup — Design Spec

**Goal:** Prevent the bot from rushing to appointment booking on turn 1 for symptom-led messages. Enforce a 2-turn warm conversation before the `appointment` tool becomes available, while bypassing warmup when the patient explicitly intends to book.

**Architecture:** Intent detection on turn 0 (keyword scan) sets `session.booking_intent`. A turn counter (`session.turn_count`) is incremented on every real patient message. Tool selection gates `PATIENT_TOOLS` behind `turn_count >= 3 OR booking_intent == True`. The system prompt guides the LLM to gather symptom context before presenting doctors.

**Tech Stack:** Python dataclasses (Session), existing orchestrator tool-selection logic, no new dependencies.

---

## Intent Detection

Single keyword scan on the patient's first message text (lowercased). No extra LLM call.

```python
BOOKING_KEYWORDS = {
    "book", "appointment", "cancel", "token",
    "schedule", "register", "slot", "fix appointment"
}

def _detect_booking_intent(text: str) -> bool:
    lowered = text.lower()
    return any(kw in lowered for kw in BOOKING_KEYWORDS)
```

- Runs once, on `turn_count == 0` for PATIENT role only.
- Result stored in `session.booking_intent` (bool) for the session lifetime.

## Session Model Changes

`models/session.py` — two new fields on `Session`:

```python
turn_count:     int  = 0
booking_intent: bool = False
```

## Tool Gating

`orchestrator/schemas.py` — new export:
```python
PATIENT_TOOLS_WARMUP = [_kg_retriever_schema]  # appointment stripped
```

`orchestrator/core.py` — in `handle_message`, `else` branch (new messages only):

```python
if session.turn_count == 0 and session.role == Role.PATIENT:
    session.booking_intent = _detect_booking_intent(wa_message.text)
session.turn_count += 1
```

Tool selection:
```python
use_full_tools = session.booking_intent or session.turn_count >= 3
tool_schemas   = PATIENT_TOOLS if use_full_tools else PATIENT_TOOLS_WARMUP
```

AWAITING_CONFIRM replies (YES/NO) do not increment `turn_count`.

## System Prompt

`prompts/system.py` — `PATIENT_SYSTEM_PROMPT` rewritten to guide staged conversation:

1. Acknowledge symptom with empathy (one sentence)
2. Ask one focused follow-up (severity, duration, context)
3. Call `kg_retriever` once need is understood
4. Present doctors (name, specialization, fee)
5. Only then ask if they want to book

If patient directly asks to book/cancel, proceed immediately.

## Conversation Flow After Change

**Symptom-led (warmup enforced):**
- Turn 1: "chest pain" → bot empathizes, asks follow-up question
- Turn 2: patient answers → bot calls kg_retriever, presents doctors
- Turn 3: "Would you like to book?"

**Booking-intent (warmup skipped):**
- Turn 1: "book with Dr. Susan" → full tools available, bot proceeds to booking
