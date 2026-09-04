from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

from models.session import Session, SessionState, Role, ToolCall, WAMessage, ChatTurn, ChatRole
from orchestrator.core import WhatsAppOrchestrator


def _stale_session(hours_ago: float, **overrides):
    defaults = dict(
        session_id="s1", hospital_id="glngs-chn", from_number="919876543210",
        state=SessionState.IDLE, history=[ChatTurn(role=ChatRole.USER, content="hi")],
        pending_tool=None, role=Role.PATIENT, turn_count=5, booking_intent=True,
        memory_loaded=True, memory_context="some prior context",
        last_active_at=datetime.now(timezone.utc) - timedelta(hours=hours_ago),
    )
    defaults.update(overrides)
    return Session(**defaults)


def test_stale_session_resets_turn_count_so_greeting_fires_again():
    """A phone number that messaged hours ago, past the idle-reset window,
    should be treated as a fresh conversation — not silently resumed as if
    no time had passed, which was why the greeting never fired again."""
    session = _stale_session(hours_ago=10)
    repository = MagicMock()
    repository.get_session.return_value = session

    orchestrator = WhatsAppOrchestrator(llm=MagicMock(), notifier=MagicMock(), repository=repository)
    wa_message = WAMessage(from_number="919876543210", message_id="m1", text="hi", hospital_id="glngs-chn")

    context = orchestrator._hydrate(wa_message)

    assert context.session.turn_count == 0
    assert context.session.history == []
    assert context.session.booking_intent is False
    assert context.session.memory_loaded is False
    assert context.session.state == SessionState.IDLE


def test_recent_session_is_not_reset():
    """A session active a few minutes ago must keep its turn count and
    history — only a genuinely cold conversation should reset."""
    session = _stale_session(hours_ago=0.5)
    repository = MagicMock()
    repository.get_session.return_value = session

    orchestrator = WhatsAppOrchestrator(llm=MagicMock(), notifier=MagicMock(), repository=repository)
    wa_message = WAMessage(from_number="919876543210", message_id="m1", text="hi", hospital_id="glngs-chn")

    context = orchestrator._hydrate(wa_message)

    assert context.session.turn_count == 5
    assert len(context.session.history) == 1
    assert context.session.booking_intent is True


def test_stale_session_clears_pending_confirmation():
    """A booking left AWAITING_CONFIRM hours ago is stale — it shouldn't
    still be waiting for a yes/no when the patient starts a new chat."""
    session = _stale_session(
        hours_ago=8,
        state=SessionState.AWAITING_CONFIRM,
        pending_tool=ToolCall(tool_name="appointment", args={"action": "BOOK"}),
    )
    repository = MagicMock()
    repository.get_session.return_value = session

    orchestrator = WhatsAppOrchestrator(llm=MagicMock(), notifier=MagicMock(), repository=repository)
    wa_message = WAMessage(from_number="919876543210", message_id="m1", text="hi", hospital_id="glngs-chn")

    context = orchestrator._hydrate(wa_message)

    assert context.session.state == SessionState.IDLE
    assert context.session.pending_tool is None


def test_session_idle_reset_hours_is_configurable():
    session = _stale_session(hours_ago=2)
    repository = MagicMock()
    repository.get_session.return_value = session

    orchestrator = WhatsAppOrchestrator(
        llm=MagicMock(), notifier=MagicMock(), repository=repository, session_idle_reset_hours=1,
    )
    wa_message = WAMessage(from_number="919876543210", message_id="m1", text="hi", hospital_id="glngs-chn")

    context = orchestrator._hydrate(wa_message)

    assert context.session.turn_count == 0
