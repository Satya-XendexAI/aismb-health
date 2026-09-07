# Context-Aware Confirm Resolution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the confirm-gate's isolated, context-blind yes/no/unclear classifier with one that reasons over recent conversation context, so a reply that's imperfectly transcribed or phrased unusually is understood the same way the rest of the conversation already is — instead of being judged as a bare, isolated string.

**Architecture:** `orchestrator/llm.py`'s `classify_confirm_reply()` is replaced by `resolve_confirmation()`, which sends the model the pending action, a plain-text summary of the last few conversational turns, and the patient's reply, then reads back a structured `yes`/`no`/`unclear` decision via tool-calling (not free-text parsing). The summary is built by a new `_recent_conversation_text()` helper in `orchestrator/core.py` that extracts only plain USER/ASSISTANT text turns from `session.history` — deliberately *not* the raw tool-call turns, so this side call never touches the delicate assistant-tool-call/tool-result pairing the main conversation loop depends on, and can't reintroduce the dangling-tool-call bug fixed earlier this session. The public contract (`"yes"` / `"no"` / `"unclear"` string) stays identical, so the call site in `_handle_awaiting_confirm()` barely changes, and most existing tests only need their patched function name updated.

**Tech Stack:** Python, the existing `GeminiLLMAdapter` (OpenAI-compatible `chat.completions.create` with `tools`/`tool_choice`), `pytest` + `unittest.mock`.

**Spec:** No separate spec document — captured directly from the conversation on 2026-09-07. The problem: `classify_confirm_reply()` only ever sees the bare reply text, with none of the conversation's context, making it unusually brittle for a stage where voice replies are typically short and error-prone to transcribe. This plan is self-contained.

## Global Constraints

- The return contract of the confirm-resolution function stays exactly `"yes"` / `"no"` / `"unclear"` (a plain string) — do not change this, or every call site and every existing test that mocks it breaks unnecessarily.
- Never let the new function raise out to its caller, and never let it return anything other than `"yes"`/`"no"`/`"unclear"` on any failure or unexpected model output — always fall back to `"unclear"`, so a pending action is never silently treated as confirmed. (Same safety property `classify_confirm_reply()` already had — preserve it.)
- Do not pass raw `session.history` (the `ChatTurn` list with tool-call/tool-result pairing) into this side call. Build a separate plain-text summary instead — see Architecture above for why.
- New functions are sync (`def`, not `async def`), matching the rest of `orchestrator/`.
- System prompts live in `prompts/system.py`, not inline in `orchestrator/llm.py` — this was already fixed once this session; don't reintroduce an inline prompt.
- `tests/` is in `.gitignore` (added this session) — existing tracked test files still commit normally when modified; genuinely new test files need `git add -f`.
- No explicit token/character budget on `_recent_conversation_text()`'s output — considered and deliberately skipped. `max_turns` (default 6) already bounds it, `_responder()` already caps `session.history` overall to `max_history_turns=10`, and WhatsApp messages are short-form. No other function in this codebase does explicit token budgeting either. Revisit only if real evidence of a problem shows up.

---

## File Structure

- **Modify:** `orchestrator/schemas.py` — add `CONFIRM_REPLY_TOOLS`, the tool schema forcing a structured `yes`/`no`/`unclear` decision.
- **Modify:** `prompts/system.py` — add `RESOLVE_CONFIRMATION_PROMPT`; remove `CLASSIFY_CONFIRM_REPLY_PROMPT` (fully superseded).
- **Modify:** `orchestrator/llm.py` — add `resolve_confirmation()`; remove `classify_confirm_reply()`.
- **Modify:** `orchestrator/core.py` — add `_recent_conversation_text()`; swap the `classify_confirm_reply()` call in `_handle_awaiting_confirm()` for `resolve_confirmation()`; update the import line.
- **Modify:** `tests/test_confirm_intent_multilingual.py` — rewrite to cover the new schema, prompt, `resolve_confirmation()`, and `_recent_conversation_text()`.
- **Modify:** `tests/test_voice_confirmation_matching.py`, `tests/test_appointment_response_formatting.py`, `tests/test_confirm_gate_error_handling.py`, `tests/test_language_propagation.py` — rename the patched function from `orchestrator.core.classify_confirm_reply` to `orchestrator.core.resolve_confirmation` (no other changes needed — same return-value contract).

---

### Task 1: Tool schema for a structured confirm decision

**Files:**
- Modify: `orchestrator/schemas.py` (append after `_report_delay_schema`, i.e. after line 219)
- Test: `tests/test_confirm_intent_multilingual.py` (new content — this task replaces the file's current content entirely; later tasks append to it)

**Interfaces:**
- Produces: `CONFIRM_REPLY_TOOLS: list` — a one-tool OpenAI-style function-calling schema list, importable from `orchestrator.schemas`.

- [ ] **Step 1: Write the failing test**

Replace the entire contents of `tests/test_confirm_intent_multilingual.py` with:

```python
from orchestrator.schemas import CONFIRM_REPLY_TOOLS


def test_confirm_reply_tools_schema_shape():
    assert len(CONFIRM_REPLY_TOOLS) == 1
    tool = CONFIRM_REPLY_TOOLS[0]
    assert tool["type"] == "function"
    assert tool["function"]["name"] == "resolve_confirmation"

    params = tool["function"]["parameters"]
    decision = params["properties"]["decision"]
    assert decision["enum"] == ["yes", "no", "unclear"]
    assert params["required"] == ["decision"]
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/test_confirm_intent_multilingual.py -v`
Expected: `FAIL` — `ImportError: cannot import name 'CONFIRM_REPLY_TOOLS' from 'orchestrator.schemas'`

- [ ] **Step 3: Add the schema**

In `orchestrator/schemas.py`, append after the `_report_delay_schema` block (after line 219, before the `PATIENT_TOOLS = [...]` line):

```python
_resolve_confirmation_schema = {
    "type": "function",
    "function": {
        "name": "resolve_confirmation",
        "description": (
            "Decide whether the patient's latest reply confirms or "
            "declines the pending action, using the conversation context "
            "provided."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "decision": {
                    "type": "string",
                    "enum": ["yes", "no", "unclear"],
                    "description": (
                        "'yes' if the reply clearly confirms the pending "
                        "action, in any language, phrasing, or code-mixing "
                        "— including naming the action back (e.g. 'cancel' "
                        "confirming a cancellation). 'no' if it clearly "
                        "declines. 'unclear' if it's new information, a "
                        "correction, an unrelated question, or genuinely "
                        "ambiguous."
                    ),
                },
            },
            "required": ["decision"],
        },
    },
}

CONFIRM_REPLY_TOOLS = [_resolve_confirmation_schema]
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `pytest tests/test_confirm_intent_multilingual.py -v`
Expected: `1 passed`

- [ ] **Step 5: Commit**

```bash
git add orchestrator/schemas.py tests/test_confirm_intent_multilingual.py
git commit -m "feat(confirm): add CONFIRM_REPLY_TOOLS schema for structured confirm decisions"
```

---

### Task 2: Prompt template for confirm resolution

**Files:**
- Modify: `prompts/system.py` — add `RESOLVE_CONFIRMATION_PROMPT`, remove `CLASSIFY_CONFIRM_REPLY_PROMPT`
- Test: `tests/test_confirm_intent_multilingual.py` (append)

**Interfaces:**
- Produces: `RESOLVE_CONFIRMATION_PROMPT: str` — a template with `{pending_action}` and `{recent_context}` placeholders, filled via `.format()`.
- Removes: `CLASSIFY_CONFIRM_REPLY_PROMPT` (no longer used after Task 4).

- [ ] **Step 1: Write the failing test**

Append to `tests/test_confirm_intent_multilingual.py`:

```python
from prompts.system import RESOLVE_CONFIRMATION_PROMPT


def test_resolve_confirmation_prompt_renders_pending_action_and_context():
    rendered = RESOLVE_CONFIRMATION_PROMPT.format(
        pending_action="BOOK appointment with Dr. Nidhi Singh on 2026-09-07",
        recent_context="Patient: I need a dermatologist for my sister.",
    )
    assert "BOOK appointment with Dr. Nidhi Singh on 2026-09-07" in rendered
    assert "Patient: I need a dermatologist for my sister." in rendered


def test_resolve_confirmation_prompt_warns_against_inferring_yes_from_a_mention():
    # Regression guard: if this rule is ever edited out of the prompt, a
    # reply like "I want to cancel, but actually move it to tomorrow"
    # could get silently misread as confirming the cancellation just
    # because it mentions the action.
    assert "Do not infer confirmation merely because the patient mentions the action" in RESOLVE_CONFIRMATION_PROMPT
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/test_confirm_intent_multilingual.py -v`
Expected: `FAIL` — `ImportError: cannot import name 'RESOLVE_CONFIRMATION_PROMPT' from 'prompts.system'`

- [ ] **Step 3: Replace the classify prompt with the resolve prompt**

In `prompts/system.py`, find `CLASSIFY_CONFIRM_REPLY_PROMPT` (in the "Utility prompts" section added earlier this session) and replace it entirely with:

```python
RESOLVE_CONFIRMATION_PROMPT = (
    "The patient was asked to confirm this pending action: \"{pending_action}\".\n\n"
    "Recent conversation, for context:\n{recent_context}\n\n"
    "The patient's latest reply may be in any language, script, or phrasing "
    "(including code-mixed languages), and may be an imperfect voice "
    "transcript. Using the context above, call resolve_confirmation with "
    "your decision:\n"
    "- 'yes' only when the latest reply actually confirms the pending "
    "action.\n"
    "- 'no' when the patient clearly rejects the pending action.\n"
    "- 'unclear' when the patient changes the request, provides new "
    "information, asks a question, corrects previous information, or the "
    "reply is genuinely ambiguous.\n"
    "- Do not infer confirmation merely because the patient mentions the "
    "action — e.g. \"I want to cancel my appointment, but actually can we "
    "move it to tomorrow?\" is 'unclear', not 'yes', even though it "
    "mentions cancelling."
)
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `pytest tests/test_confirm_intent_multilingual.py -v`
Expected: `3 passed`

- [ ] **Step 5: Commit**

```bash
git add prompts/system.py tests/test_confirm_intent_multilingual.py
git commit -m "feat(confirm): replace classify prompt with context-aware resolve prompt, explicit decision rules"
```

---

### Task 3: Verify `describe_tool()` produces sufficient detail

`resolve_confirmation()` (Task 4) can only reason well if `pending_action` — built by the existing `describe_tool()` in `orchestrator/formatters.py` — actually describes the booking specifically. This task adds regression coverage for that existing behavior before building on top of it; no source change is expected here.

**Files:**
- Test: `tests/test_confirm_intent_multilingual.py` (append)

**Interfaces:**
- Consumes: `describe_tool` (existing, `orchestrator.formatters`), `ToolCall` (existing, `models.session`).
- Produces: nothing new — guards an existing behavior the rest of this plan depends on.

- [ ] **Step 1: Write the tests**

Append to `tests/test_confirm_intent_multilingual.py`:

```python
from orchestrator.formatters import describe_tool
from models.session import ToolCall


def test_describe_tool_includes_doctor_department_and_date_when_present():
    tool_call = ToolCall(
        tool_name="appointment",
        args={
            "action": "BOOK", "doctor_id": "dr-susan-george--chn",
            "doctor_name": "Susan George", "department": "Cardiology",
            "patient_name": "Priya", "date": "2026-09-08",
        },
    )
    description = describe_tool(tool_call)

    assert "BOOK" in description
    assert "Susan George" in description
    assert "Cardiology" in description
    assert "Priya" in description
    assert "2026-09-08" in description


def test_describe_tool_still_identifies_the_doctor_with_only_required_fields():
    # The appointment tool schema (orchestrator/schemas.py) requires
    # doctor_id, so this is the worst realistic case — doctor_name,
    # department, and patient_name all absent — resolve_confirmation
    # still needs a usable pending_action string out of this.
    tool_call = ToolCall(
        tool_name="appointment",
        args={"action": "CANCEL", "doctor_id": "dr-susan-george--chn", "date": "2026-09-08"},
    )
    description = describe_tool(tool_call)

    assert "CANCEL" in description
    assert "Susan George" in description
    assert "2026-09-08" in description
```

- [ ] **Step 2: Run the tests**

Run: `pytest tests/test_confirm_intent_multilingual.py -v -k describe_tool`
Expected: `2 passed` — confirms the existing `describe_tool()` already produces a sufficiently specific description (the doctor's name is recovered from `doctor_id` via `doctor_display_name()`'s slug-to-title-case fallback even when `doctor_name` is absent), so no change to `orchestrator/formatters.py` is needed for this plan.

- [ ] **Step 3: Commit**

```bash
git add tests/test_confirm_intent_multilingual.py
git commit -m "test(confirm): verify describe_tool produces sufficient detail for context-aware confirm resolution"
```

---

### Task 4: `resolve_confirmation()` in orchestrator/llm.py

**Files:**
- Modify: `orchestrator/llm.py` — remove `classify_confirm_reply()`, add `resolve_confirmation()`
- Test: `tests/test_confirm_intent_multilingual.py` (append)

**Interfaces:**
- Consumes: `CONFIRM_REPLY_TOOLS` (Task 1, from `orchestrator.schemas`), `RESOLVE_CONFIRMATION_PROMPT` (Task 2, from `prompts.system`).
- Produces: `resolve_confirmation(llm: "GeminiLLMAdapter", reply_text: str, recent_context: str = "", pending_action: str | None = None) -> str` — returns `"yes"`, `"no"`, or `"unclear"`. Never raises.
- Removes: `classify_confirm_reply()` and its now-unused helper prompt reference.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_confirm_intent_multilingual.py`:

```python
import json
from unittest.mock import MagicMock

from orchestrator.llm import resolve_confirmation


def _llm_returning_tool_call(decision: str) -> MagicMock:
    tool_call = MagicMock()
    tool_call.function.name = "resolve_confirmation"
    tool_call.function.arguments = json.dumps({"decision": decision})
    message = MagicMock(tool_calls=[tool_call])
    completion = MagicMock(choices=[MagicMock(message=message)])
    llm = MagicMock()
    llm.client.chat.completions.create.return_value = completion
    return llm


def test_resolve_confirmation_returns_yes_from_tool_call():
    llm = _llm_returning_tool_call("yes")
    assert resolve_confirmation(llm, "avunu") == "yes"


def test_resolve_confirmation_returns_no_from_tool_call():
    llm = _llm_returning_tool_call("no")
    assert resolve_confirmation(llm, "వద్దు") == "no"


def test_resolve_confirmation_returns_unclear_from_tool_call():
    llm = _llm_returning_tool_call("unclear")
    assert resolve_confirmation(llm, "actually what's the fee") == "unclear"


def test_resolve_confirmation_falls_back_to_unclear_when_model_skips_the_tool():
    message = MagicMock(tool_calls=None)
    completion = MagicMock(choices=[MagicMock(message=message)])
    llm = MagicMock()
    llm.client.chat.completions.create.return_value = completion

    assert resolve_confirmation(llm, "hmm") == "unclear"


def test_resolve_confirmation_falls_back_to_unclear_on_malformed_arguments():
    tool_call = MagicMock()
    tool_call.function.name = "resolve_confirmation"
    tool_call.function.arguments = "{not valid json"
    message = MagicMock(tool_calls=[tool_call])
    completion = MagicMock(choices=[MagicMock(message=message)])
    llm = MagicMock()
    llm.client.chat.completions.create.return_value = completion

    assert resolve_confirmation(llm, "yes") == "unclear"


def test_resolve_confirmation_falls_back_to_unclear_on_llm_failure():
    llm = MagicMock()
    llm.client.chat.completions.create.side_effect = RuntimeError("network down")
    assert resolve_confirmation(llm, "yes") == "unclear"


def test_resolve_confirmation_falls_back_to_unclear_when_tool_name_does_not_match():
    # Defensive: tool_choice is forced to resolve_confirmation, but don't
    # trust that blindly — a wrong-named tool call must never be parsed as
    # if it were our own.
    tool_call = MagicMock()
    tool_call.function.name = "some_other_tool"
    tool_call.function.arguments = json.dumps({"decision": "yes"})
    message = MagicMock(tool_calls=[tool_call])
    completion = MagicMock(choices=[MagicMock(message=message)])
    llm = MagicMock()
    llm.client.chat.completions.create.return_value = completion

    assert resolve_confirmation(llm, "yes") == "unclear"


def test_resolve_confirmation_falls_back_to_unclear_when_decision_is_missing():
    tool_call = MagicMock()
    tool_call.function.name = "resolve_confirmation"
    tool_call.function.arguments = json.dumps({})
    message = MagicMock(tool_calls=[tool_call])
    completion = MagicMock(choices=[MagicMock(message=message)])
    llm = MagicMock()
    llm.client.chat.completions.create.return_value = completion

    assert resolve_confirmation(llm, "yes") == "unclear"


def test_resolve_confirmation_falls_back_to_unclear_on_unexpected_decision_value():
    tool_call = MagicMock()
    tool_call.function.name = "resolve_confirmation"
    tool_call.function.arguments = json.dumps({"decision": "maybe"})
    message = MagicMock(tool_calls=[tool_call])
    completion = MagicMock(choices=[MagicMock(message=message)])
    llm = MagicMock()
    llm.client.chat.completions.create.return_value = completion

    assert resolve_confirmation(llm, "hmm") == "unclear"


def test_resolve_confirmation_normalizes_decision_case():
    tool_call = MagicMock()
    tool_call.function.name = "resolve_confirmation"
    tool_call.function.arguments = json.dumps({"decision": "YES"})
    message = MagicMock(tool_calls=[tool_call])
    completion = MagicMock(choices=[MagicMock(message=message)])
    llm = MagicMock()
    llm.client.chat.completions.create.return_value = completion

    assert resolve_confirmation(llm, "yes") == "yes"


def test_resolve_confirmation_falls_back_to_unclear_on_empty_choices():
    completion = MagicMock(choices=[])
    llm = MagicMock()
    llm.client.chat.completions.create.return_value = completion

    assert resolve_confirmation(llm, "yes") == "unclear"


def test_resolve_confirmation_includes_pending_action_and_context_in_prompt():
    llm = _llm_returning_tool_call("yes")
    resolve_confirmation(
        llm, "cancel",
        recent_context="Patient: cancel my appointment with Dr. Susan George.",
        pending_action="CANCEL appointment with Dr. Susan George",
    )

    call_kwargs = llm.client.chat.completions.create.call_args.kwargs
    system_content = call_kwargs["messages"][0]["content"]
    assert "CANCEL appointment with Dr. Susan George" in system_content
    assert "Patient: cancel my appointment with Dr. Susan George." in system_content
    assert call_kwargs["messages"][1]["content"] == "cancel"
    assert call_kwargs["tools"] == CONFIRM_REPLY_TOOLS
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_confirm_intent_multilingual.py -v -k resolve_confirmation`
Expected: `FAIL` — `ImportError: cannot import name 'resolve_confirmation' from 'orchestrator.llm'`

- [ ] **Step 3: Remove `classify_confirm_reply` and add `resolve_confirmation`**

In `orchestrator/llm.py`:

1. Update the `prompts.system` import at the top of the file — remove `CLASSIFY_CONFIRM_REPLY_PROMPT`, add `RESOLVE_CONFIRMATION_PROMPT`:

```python
from prompts.system import (
    PATIENT_SYSTEM_PROMPT, TRANSLATE_MESSAGE_PROMPT, TRANSLATE_LABELS_PROMPT,
    NORMALIZE_TO_ENGLISH_PROMPT, RESOLVE_CONFIRMATION_PROMPT,
)
```

2. Add this import near the top (with the other project imports):

```python
from orchestrator.schemas import CONFIRM_REPLY_TOOLS
```

3. Delete the entire `classify_confirm_reply()` function (it currently sits between `translate_text()` and the `_STATIC_TRANSLATION_CACHE` line).

4. In its place, add:

```python
def resolve_confirmation(
    llm: "GeminiLLMAdapter", reply_text: str, recent_context: str = "", pending_action: str | None = None,
) -> str:
    """Decide whether `reply_text` confirms or declines a pending action,
    using recent conversation context (not just the bare reply) so an
    imperfectly transcribed or unusually phrased reply is understood the
    same way the rest of the conversation already is — instead of judging
    a short, isolated string with no context at all.

    Returns 'yes', 'no', or 'unclear' (new information, a correction, or
    anything that isn't a plain confirmation). Falls back to 'unclear' on
    any failure or unexpected output, so a pending action is never
    silently treated as confirmed."""
    system_prompt = RESOLVE_CONFIRMATION_PROMPT.format(
        pending_action=pending_action or "the pending action",
        recent_context=recent_context or "(no earlier context)",
    )
    try:
        completion = llm.client.chat.completions.create(
            model=llm.model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": reply_text},
            ],
            tools=CONFIRM_REPLY_TOOLS,
            tool_choice={"type": "function", "function": {"name": "resolve_confirmation"}},
            temperature=0.0,
            max_tokens=50,
        )
        message = completion.choices[0].message
        if not message.tool_calls:
            return "unclear"
        call = message.tool_calls[0]
        if call.function.name != "resolve_confirmation":
            # tool_choice forces this specific tool, but don't trust that
            # blindly — never parse a differently-named call as our own.
            return "unclear"
        args = json.loads(call.function.arguments)
        decision = (args.get("decision") or "").lower()
    except Exception as exc:
        logger.warning("Confirmation resolution failed, treating as unclear: %s", exc)
        return "unclear"

    return decision if decision in ("yes", "no", "unclear") else "unclear"
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/test_confirm_intent_multilingual.py -v`
Expected: `17 passed` (5 from Tasks 1-3, 12 new)

- [ ] **Step 5: Commit**

```bash
git add orchestrator/llm.py tests/test_confirm_intent_multilingual.py
git commit -m "feat(confirm): add context-aware resolve_confirmation(), remove classify_confirm_reply()"
```

---

### Task 5: `_recent_conversation_text()` helper in orchestrator/core.py

**Files:**
- Modify: `orchestrator/core.py` (add as a new method on `WhatsAppOrchestrator`, near `_hydrate`)
- Test: `tests/test_confirm_intent_multilingual.py` (append)

**Interfaces:**
- Produces: `WhatsAppOrchestrator._recent_conversation_text(session, max_turns: int = 6) -> str` — a plain-text summary of the last `max_turns` *conversational* turns (USER turns and plain-text ASSISTANT turns, one line each) — tool-call and tool-result turns are skipped and don't count against `max_turns`, so `max_turns=6` reliably means six lines of readable conversation, not six raw history entries that might include non-conversational turns.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_confirm_intent_multilingual.py`:

```python
from unittest.mock import MagicMock

from models.session import ChatTurn, ChatRole, ToolCall
from orchestrator.core import WhatsAppOrchestrator


def _orchestrator():
    return WhatsAppOrchestrator(llm=MagicMock(), notifier=MagicMock(), repository=MagicMock())


def test_recent_conversation_text_includes_plain_user_and_assistant_turns():
    session = MagicMock()
    session.history = [
        ChatTurn(role=ChatRole.USER, content="I need a dermatologist for my sister"),
        ChatTurn(role=ChatRole.ASSISTANT, content="Sure, which date works for you?"),
    ]

    text = _orchestrator()._recent_conversation_text(session)

    assert "Patient: I need a dermatologist for my sister" in text
    assert "Assistant: Sure, which date works for you?" in text


def test_recent_conversation_text_excludes_tool_call_and_tool_result_turns():
    session = MagicMock()
    session.history = [
        ChatTurn(role=ChatRole.USER, content="book it"),
        ChatTurn(role=ChatRole.ASSISTANT, content="", tool_call=ToolCall(tool_name="appointment", args={})),
        ChatTurn(role=ChatRole.TOOL_RESULT, content='{"status": "AWAITING_CONFIRMATION"}',
                 tool_call=ToolCall(tool_name="appointment", args={})),
    ]

    text = _orchestrator()._recent_conversation_text(session)

    assert text == "Patient: book it"


def test_recent_conversation_text_respects_max_turns():
    session = MagicMock()
    session.history = [
        ChatTurn(role=ChatRole.USER, content=f"message {i}") for i in range(10)
    ]

    text = _orchestrator()._recent_conversation_text(session, max_turns=3)

    assert text == "Patient: message 7\nPatient: message 8\nPatient: message 9"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_confirm_intent_multilingual.py -v -k recent_conversation_text`
Expected: `FAIL` — `AttributeError: 'WhatsAppOrchestrator' object has no attribute '_recent_conversation_text'`

- [ ] **Step 3: Add the helper**

In `orchestrator/core.py`, add this method right after `_hydrate()`:

```python
def _recent_conversation_text(self, session, max_turns: int = 6) -> str:
    """Plain-text summary of the last max_turns USER/ASSISTANT turns —
    tool calls and tool results deliberately excluded. This feeds a small
    side call (resolve_confirmation), not the main tool-calling loop, so
    it only needs readable conversation, never the raw tool-call/tool-
    result pairing the main loop's history depends on.

    Walks history backwards and stops once max_turns *conversational*
    lines are found — not the last max_turns raw history entries, which
    would undercount whenever tool-call/tool-result turns are interleaved
    (every confirm-gate booking proposal leaves at least one such turn)."""
    lines = []
    for turn in reversed(session.history):
        if turn.role == ChatRole.USER:
            lines.append(f"Patient: {turn.content}")
        elif turn.role == ChatRole.ASSISTANT and not turn.tool_call:
            lines.append(f"Assistant: {turn.content}")
        if len(lines) == max_turns:
            break
    return "\n".join(reversed(lines))
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/test_confirm_intent_multilingual.py -v`
Expected: `20 passed`

- [ ] **Step 5: Commit**

```bash
git add orchestrator/core.py tests/test_confirm_intent_multilingual.py
git commit -m "feat(confirm): add _recent_conversation_text helper for context-aware confirm resolution"
```

---

### Task 6: Wire `resolve_confirmation()` into the confirm-gate

**Files:**
- Modify: `orchestrator/core.py` — the `orchestrator.llm` import block, and the opening lines of `_handle_awaiting_confirm()`. (Line numbers given below matched the file before Tasks 3-5 added code earlier in it, and will have drifted — match by the exact code text shown, not the line number.)
- Modify: `tests/test_voice_confirmation_matching.py`, `tests/test_appointment_response_formatting.py`, `tests/test_confirm_gate_error_handling.py`, `tests/test_language_propagation.py`

**Interfaces:**
- Consumes: `resolve_confirmation` (Task 4), `_recent_conversation_text` (Task 5).
- Produces: `_handle_awaiting_confirm()`'s branching (`yes`/`no`/else) is unchanged — only how `reply` gets computed changes.

- [ ] **Step 1: Update the import**

In `orchestrator/core.py`, find the `orchestrator.llm` import block and replace:

```python
from orchestrator.llm import (
    translate_static, translate_text, translate_labels, normalize_to_english, classify_confirm_reply,
)
```

with:

```python
from orchestrator.llm import (
    translate_static, translate_text, translate_labels, normalize_to_english, resolve_confirmation,
)
```

- [ ] **Step 2: Update the call site**

In `orchestrator/core.py`, inside `_handle_awaiting_confirm()`, replace its opening lines:

```python
    def _handle_awaiting_confirm(self, wa_message, context):
        session = context.session
        pending = session.pending_tool
        pending_action = describe_tool(pending) if pending else None
        reply = classify_confirm_reply(self.llm, wa_message.text, pending_action)
```

with:

```python
    def _handle_awaiting_confirm(self, wa_message, context):
        session = context.session
        pending = session.pending_tool
        pending_action = describe_tool(pending) if pending else None
        recent_context = self._recent_conversation_text(session)
        reply = resolve_confirmation(self.llm, wa_message.text, recent_context, pending_action)
```

Everything below this in `_handle_awaiting_confirm()` (the plan-gate branch, the yes/no/else branching for the single-tool gate) is unchanged — it already just branches on the string `reply`.

- [ ] **Step 3: Run the core test suite to see what needs updating**

Run: `pytest tests/ -v -k "confirm or appointment_response or language_propagation" 2>&1 | tail -60`
Expected: several failures, each an `AttributeError` or mock-assertion failure referencing `orchestrator.core.classify_confirm_reply`.

- [ ] **Step 4: Rename the patched function in each dependent test file**

In each of these four files, every occurrence of:
```python
patch("orchestrator.core.classify_confirm_reply", return_value="yes")
```
(and the `"no"` / `"unclear"` variants — note: some earlier tests may still say `return_value="unclear"` from before this session's classifier used that literal string; keep whatever value was already there, only change the dotted path) becomes:
```python
patch("orchestrator.core.resolve_confirmation", return_value="yes")
```

Apply this rename in:
- `tests/test_voice_confirmation_matching.py`
- `tests/test_appointment_response_formatting.py`
- `tests/test_confirm_gate_error_handling.py`
- `tests/test_language_propagation.py`

No other lines in these files need to change — the return-value contract (`"yes"`/`"no"`/`"unclear"`) is identical to before.

- [ ] **Step 5: Run the full test suite to verify everything passes**

Run: `pytest -v 2>&1 | tail -30`
Expected: all tests pass, no reference to `classify_confirm_reply` remains anywhere.

Verify no leftover references:
Run: `grep -rn "classify_confirm_reply" --include="*.py" .`
Expected: no output.

- [ ] **Step 6: Commit**

```bash
git add orchestrator/core.py tests/test_voice_confirmation_matching.py tests/test_appointment_response_formatting.py tests/test_confirm_gate_error_handling.py tests/test_language_propagation.py
git commit -m "feat(confirm): wire resolve_confirmation into the confirm-gate"
```

---

## Manual End-to-End Verification (after Task 6)

Automated tests mock every LLM call; before considering this done, verify against a real deployment:

1. Deploy (same commit → merge `main` → push → PR → merge → pipeline flow as prior sessions).
2. On WhatsApp, start a booking and let it reach the confirm prompt.
3. Reply with a natural, non-bare-word voice note (e.g. "yeah go ahead and book that" or a code-mixed phrase like "book cheyandi") — confirm it books correctly on the first try instead of looping.
4. Reply to a *different* booking's confirm prompt with something that genuinely changes the request (e.g. "actually make it tomorrow instead") — confirm it's read as new information (not a false yes/no) and the AI reconsiders correctly.
5. Reply to a confirm prompt with a plain rejection ("no, don't book it") — confirm it cancels cleanly.
6. Check `kubectl logs deployment/aismb-whatsapp -n aismb-health --since=15m | grep "Voice transcript"` alongside the bot's behavior to correlate what was actually said with what `resolve_confirmation` decided, if anything still looks off.

---

## Definition of Done

**Functional**
- [ ] `classify_confirm_reply()` is removed; `resolve_confirmation()` exists in its place.
- [ ] The pending action, recent USER/ASSISTANT context, and the patient's latest reply are all part of what the model sees.
- [ ] Tool-call and tool-result turns are excluded from the context summary.
- [ ] The decision comes back via structured tool-calling, restricted to `yes`/`no`/`unclear`.

**Safety**
- [ ] No LLM/API failure, malformed output, wrong-tool response, or unexpected decision value can result in anything other than `"unclear"`.
- [ ] `resolve_confirmation()` never raises out to `_handle_awaiting_confirm()`.

**Compatibility**
- [ ] `_handle_awaiting_confirm()`'s yes/no/unclear branching is unchanged — only how `reply` is computed changed.
- [ ] `grep -rn "classify_confirm_reply" --include="*.py" .` returns nothing.
- [ ] Full `pytest -v` suite passes.

**Production** (Manual E2E Verification above)
- [ ] A natural, non-bare-word voice confirmation books on the first try.
- [ ] A genuinely changed request is read as new information, not a false yes.
- [ ] A plain rejection cancels cleanly.
- [ ] No duplicate tool execution observed across the session.
