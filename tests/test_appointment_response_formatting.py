from unittest.mock import MagicMock, patch

from models.session import Session, SessionState, Role, ToolCall, WAMessage
from orchestrator.core import WhatsAppOrchestrator


def _session_awaiting_confirm(action: str, language_code: str = "en"):
    return Session(
        session_id="s1", hospital_id="glngs-chn", from_number="919876543210",
        state=SessionState.AWAITING_CONFIRM, history=[], role=Role.PATIENT,
        language_code=language_code,
        pending_tool=ToolCall(tool_name="appointment", args={"action": action, "patient_name": "Priya"}),
    )


def test_cancellation_sends_translated_message_without_falling_back_to_llm():
    """A CANCEL result used to fall straight through to the LLM freely
    narrating the raw tool JSON (format_booking_result only ever handled
    BOOK) — losing the well-formed message the tool already built, and with
    no guarantee it would stay in the patient's language. It should now be
    sent directly instead."""
    session = _session_awaiting_confirm("CANCEL")
    repository = MagicMock()
    repository.get_session.return_value = session

    notifier = MagicMock()
    llm = MagicMock()
    orchestrator = WhatsAppOrchestrator(llm=llm, notifier=notifier, repository=repository)
    orchestrator._execute_tool = MagicMock(return_value={
        "action": "CANCEL",
        "result": {
            "status": "CANCELLED",
            "message": "Token #3 for Priya has been cancelled.",
            "cancelled_for": "Priya",
        },
    })

    with patch("orchestrator.core.classify_confirm_reply", return_value="yes"):
        wa_message = WAMessage(from_number="919876543210", message_id="m1", text="yes", hospital_id="glngs-chn")
        orchestrator.handle_message(wa_message)

    llm.run_agent.assert_not_called()
    notifier.send.assert_called_once_with("919876543210", "Token #3 for Priya has been cancelled.")


def test_cancellation_message_is_translated_for_non_english_session():
    session = _session_awaiting_confirm("CANCEL", language_code="te-IN")
    repository = MagicMock()
    repository.get_session.return_value = session

    notifier = MagicMock()
    llm = MagicMock()
    orchestrator = WhatsAppOrchestrator(llm=llm, notifier=notifier, repository=repository)
    orchestrator._execute_tool = MagicMock(return_value={
        "action": "CANCEL",
        "result": {
            "status": "CANCELLED",
            "message": "Token #3 for Priya has been cancelled.",
            "cancelled_for": "Priya",
        },
    })

    with patch("orchestrator.core.translate_text", return_value="ప్రియ కోసం టోకెన్ #3 రద్దు చేయబడింది.") as mock_translate, \
         patch("orchestrator.core.classify_confirm_reply", return_value="yes"):
        wa_message = WAMessage(from_number="919876543210", message_id="m1", text="yes", hospital_id="glngs-chn")
        orchestrator.handle_message(wa_message)

    mock_translate.assert_called_once_with(orchestrator.llm, "Token #3 for Priya has been cancelled.", "te-IN")
    notifier.send.assert_called_once_with("919876543210", "ప్రియ కోసం టోకెన్ #3 రద్దు చేయబడింది.")


def test_cancellation_error_status_still_falls_back_to_llm():
    """A non-CANCELLED status (e.g. NO_ACTIVE_BOOKING) is an error case, not
    a success — it should still go through the LLM to be explained, not be
    sent verbatim as a confirmation."""
    session = _session_awaiting_confirm("CANCEL")
    repository = MagicMock()
    repository.get_session.return_value = session

    notifier = MagicMock()
    llm = MagicMock()
    from models.session import AgentResponse, AgentResponseType
    llm.run_agent.return_value = AgentResponse(
        type=AgentResponseType.TEXT, text="I couldn't find an active booking for Priya to cancel.",
    )
    orchestrator = WhatsAppOrchestrator(llm=llm, notifier=notifier, repository=repository)
    orchestrator._execute_tool = MagicMock(return_value={
        "action": "CANCEL",
        "result": {"status": "NO_ACTIVE_BOOKING", "message": "No active booking found for Priya."},
    })

    with patch("orchestrator.core.classify_confirm_reply", return_value="yes"):
        wa_message = WAMessage(from_number="919876543210", message_id="m1", text="yes", hospital_id="glngs-chn")
        orchestrator.handle_message(wa_message)

    llm.run_agent.assert_called_once()
    notifier.send.assert_called_once_with("919876543210", "I couldn't find an active booking for Priya to cancel.")
