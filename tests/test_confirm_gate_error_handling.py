from unittest.mock import MagicMock, patch

from models.session import Session, SessionState, Role, ToolCall, WAMessage
from orchestrator.core import WhatsAppOrchestrator


def _make_orchestrator_and_session(tool_exception):
    """A session parked at the confirm-gate for a BOOK action, with the
    tool call rigged to raise (simulating e.g. the missing-DB-index bug)."""
    session = Session(
        session_id="s1", hospital_id="glngs-chn", from_number="919876543210",
        state=SessionState.AWAITING_CONFIRM, history=[], role=Role.PATIENT,
        pending_tool=ToolCall(tool_name="appointment", args={"action": "BOOK", "date": "2026-09-01"}),
    )
    repository = MagicMock()
    repository.get_session.return_value = session

    notifier = MagicMock()
    orchestrator = WhatsAppOrchestrator(llm=MagicMock(), notifier=notifier, repository=repository)
    orchestrator._execute_tool = MagicMock(side_effect=tool_exception)

    return orchestrator, session, notifier


def test_tool_failure_at_confirm_gate_sends_friendly_message_not_crash():
    orchestrator, session, notifier = _make_orchestrator_and_session(
        RuntimeError("there is no unique or exclusion constraint matching the ON CONFLICT specification")
    )

    with patch("orchestrator.core.classify_confirm_reply", return_value="yes"):
        wa_message = WAMessage(from_number="919876543210", message_id="m1", text="yes", hospital_id="glngs-chn")
        orchestrator.handle_message(wa_message)  # must not raise

    notifier.send.assert_called_once()
    sent_text = notifier.send.call_args[0][1]
    assert "something went wrong" in sent_text
    assert sent_text != orchestrator.fallback_text  # the specific message, not the generic top-level one


def test_tool_failure_at_confirm_gate_clears_pending_state():
    orchestrator, session, notifier = _make_orchestrator_and_session(RuntimeError("boom"))

    with patch("orchestrator.core.classify_confirm_reply", return_value="yes"):
        wa_message = WAMessage(from_number="919876543210", message_id="m1", text="yes", hospital_id="glngs-chn")
        orchestrator.handle_message(wa_message)

    assert session.state == SessionState.IDLE
    assert session.pending_tool is None
