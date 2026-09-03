from unittest.mock import MagicMock

from models.session import Session, SessionState, Role, ToolCall, WAMessage, ChatTurn, ChatRole
from orchestrator.core import WhatsAppOrchestrator
from orchestrator.utils import is_affirmative, is_negative


def test_is_affirmative_matches_speech_to_text_transcript_with_trailing_period():
    """Sarvam (and STT services generally) return a natural-sentence
    transcript for a one-word spoken reply — 'yes' comes back as 'Yes.',
    not the bare word a typed reply would be."""
    assert is_affirmative("Yes.")
    assert is_affirmative("Okay.")
    assert is_affirmative("Sure!")


def test_is_negative_matches_speech_to_text_transcript_with_trailing_period():
    assert is_negative("No.")
    assert is_negative("Cancel.")


def test_is_affirmative_still_rejects_longer_sentence_with_trailing_punctuation():
    assert not is_affirmative("Book it for tomorrow instead.")


def _session_awaiting_confirm():
    return Session(
        session_id="s1", hospital_id="glngs-chn", from_number="919876543210",
        state=SessionState.AWAITING_CONFIRM, history=[], role=Role.PATIENT,
        pending_tool=ToolCall(
            tool_name="appointment", args={"action": "BOOK", "date": "2026-09-04"},
            tool_use_id="call_1",
        ),
    )


def test_voice_reply_with_trailing_period_actually_books_the_appointment():
    """End-to-end regression for the reported bug: a typed 'yes' booked
    correctly, but a voice 'Yes.' (with STT punctuation) fell through to
    the ambiguous-reply path and never called the booking tool at all."""
    session = _session_awaiting_confirm()
    repository = MagicMock()
    repository.get_session.return_value = session

    notifier = MagicMock()
    orchestrator = WhatsAppOrchestrator(llm=MagicMock(), notifier=notifier, repository=repository)
    orchestrator._execute_tool = MagicMock(return_value={
        "action": "BOOK",
        "result": {
            "status": "CONFIRMED", "token_number": 2, "doctor_name": "Thiagarajan Pandian",
            "department": "Orthopaedics", "hospital_name": "Test Hospital",
            "hospital_address": "", "fee": 500, "estimated_time": "2026-09-04T17:00:00",
        },
    })

    wa_message = WAMessage(from_number="919876543210", message_id="m1", text="Yes.", hospital_id="glngs-chn")
    orchestrator.handle_message(wa_message)

    orchestrator._execute_tool.assert_called_once()
    assert session.pending_tool is None
    assert session.state == SessionState.IDLE


def test_declined_pending_appointment_is_resolved_not_left_dangling():
    """After a decline, the proposed booking's tool-call turn (written to
    history before the confirm-gate deferred it) must be resolved with an
    explicit NOT_EXECUTED result — otherwise a later LLM call sees an
    unpaired tool-call turn and can mistake it for a completed booking."""
    session = _session_awaiting_confirm()
    session.history.append(ChatTurn(role=ChatRole.ASSISTANT, content="", tool_call=session.pending_tool))
    repository = MagicMock()
    repository.get_session.return_value = session

    notifier = MagicMock()
    orchestrator = WhatsAppOrchestrator(llm=MagicMock(), notifier=notifier, repository=repository)

    wa_message = WAMessage(from_number="919876543210", message_id="m1", text="No.", hospital_id="glngs-chn")
    orchestrator.handle_message(wa_message)

    tool_result_turns = [t for t in session.history if t.role == ChatRole.TOOL_RESULT]
    assert len(tool_result_turns) == 1
    assert tool_result_turns[0].tool_call.tool_use_id == "call_1"
    assert '"status": "NOT_EXECUTED"' in tool_result_turns[0].content
