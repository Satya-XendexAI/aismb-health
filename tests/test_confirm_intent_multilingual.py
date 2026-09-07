import json
from unittest.mock import MagicMock

from orchestrator.schemas import CONFIRM_REPLY_TOOLS
from orchestrator.formatters import describe_tool
from orchestrator.llm import resolve_confirmation
from orchestrator.core import WhatsAppOrchestrator
from models.session import ChatTurn, ChatRole, ToolCall
from prompts.system import RESOLVE_CONFIRMATION_PROMPT


def test_confirm_reply_tools_schema_shape():
    assert len(CONFIRM_REPLY_TOOLS) == 1
    tool = CONFIRM_REPLY_TOOLS[0]
    assert tool["type"] == "function"
    assert tool["function"]["name"] == "resolve_confirmation"

    params = tool["function"]["parameters"]
    decision = params["properties"]["decision"]
    assert decision["enum"] == ["yes", "no", "unclear"]
    assert params["required"] == ["decision"]


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


def test_resolve_confirmation_uses_generous_max_tokens():
    # Regression: this model spends part of its token budget on internal
    # "thinking" before producing visible output. max_tokens=50 was
    # observed truncating generation (finish_reason "length") before the
    # tool call ever appeared against the real API — resolve_confirmation
    # silently returned "unclear" for a plain "yes", causing the exact
    # confirm-gate repeat loop this whole feature exists to fix. Guard
    # against that budget shrinking back down by accident.
    llm = _llm_returning_tool_call("yes")
    resolve_confirmation(llm, "yes")

    call_kwargs = llm.client.chat.completions.create.call_args.kwargs
    assert call_kwargs["max_tokens"] >= 512


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
