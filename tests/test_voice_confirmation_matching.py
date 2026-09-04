from unittest.mock import MagicMock, patch

from models.session import Session, SessionState, Role, ToolCall, WAMessage, ChatTurn, ChatRole
from orchestrator.core import WhatsAppOrchestrator


def _session_awaiting_confirm():
    return Session(
        session_id="s1", hospital_id="glngs-chn", from_number="919876543210",
        state=SessionState.AWAITING_CONFIRM, history=[], role=Role.PATIENT,
        pending_tool=ToolCall(
            tool_name="appointment",
            args={"action": "BOOK", "date": "2026-09-04", "doctor_name": "Thiagarajan Pandian"},
            tool_use_id="call_1",
        ),
    )


def test_voice_reply_classified_as_yes_actually_books_the_appointment():
    """End-to-end regression for the reported bug: a plain-string matcher
    on the raw reply ('Yes.', 'book it', 'book cheyandi', ...) either
    missed real confirmations or false-positived on unrelated text.
    Matching now goes through classify_confirm_reply (LLM-based) instead —
    this confirms a 'yes' classification actually drives a real booking."""
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

    with patch("orchestrator.core.classify_confirm_reply", return_value="yes") as mock_classify:
        wa_message = WAMessage(from_number="919876543210", message_id="m1", text="book cheyandi", hospital_id="glngs-chn")
        orchestrator.handle_message(wa_message)

    mock_classify.assert_called_once_with(
        orchestrator.llm, "book cheyandi", "BOOK appointment with Thiagarajan Pandian on 2026-09-04",
    )
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

    with patch("orchestrator.core.classify_confirm_reply", return_value="no"):
        wa_message = WAMessage(from_number="919876543210", message_id="m1", text="No.", hospital_id="glngs-chn")
        orchestrator.handle_message(wa_message)

    tool_result_turns = [t for t in session.history if t.role == ChatRole.TOOL_RESULT]
    assert len(tool_result_turns) == 1
    assert tool_result_turns[0].tool_call.tool_use_id == "call_1"
    assert '"status": "NOT_EXECUTED"' in tool_result_turns[0].content


def test_unclear_reply_reconsiders_via_llm_instead_of_re_executing():
    session = _session_awaiting_confirm()
    repository = MagicMock()
    repository.get_session.return_value = session

    notifier = MagicMock()
    llm = MagicMock()
    from models.session import AgentResponse, AgentResponseType
    llm.run_agent.return_value = AgentResponse(type=AgentResponseType.TEXT, text="Sure, what date works for you?")
    orchestrator = WhatsAppOrchestrator(llm=llm, notifier=notifier, repository=repository)

    with patch("orchestrator.core.classify_confirm_reply", return_value="unclear"):
        wa_message = WAMessage(from_number="919876543210", message_id="m1", text="tomorrow instead", hospital_id="glngs-chn")
        orchestrator.handle_message(wa_message)

    llm.run_agent.assert_called_once()
    assert session.pending_tool is None
    assert session.state == SessionState.IDLE
