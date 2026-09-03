from unittest.mock import MagicMock, patch

from models.session import Session, SessionState, Role, ToolCall, WAMessage
from orchestrator.core import WhatsAppOrchestrator


def test_hydrate_stores_wa_message_language_on_new_session():
    repository = MagicMock()
    repository.get_session.return_value = None
    repository.get_role.return_value = Role.PATIENT

    orchestrator = WhatsAppOrchestrator(llm=MagicMock(), notifier=MagicMock(), repository=repository)
    wa_message = WAMessage(
        from_number="919876543210", message_id="m1", text="hi",
        hospital_id="glngs-chn", language_code="te-IN",
    )

    context = orchestrator._hydrate(wa_message)

    assert context.session.language_code == "te-IN"


def test_hydrate_keeps_default_english_when_no_language_on_message():
    repository = MagicMock()
    repository.get_session.return_value = None
    repository.get_role.return_value = Role.PATIENT

    orchestrator = WhatsAppOrchestrator(llm=MagicMock(), notifier=MagicMock(), repository=repository)
    wa_message = WAMessage(from_number="919876543210", message_id="m1", text="hi", hospital_id="glngs-chn")

    context = orchestrator._hydrate(wa_message)

    assert context.session.language_code == "en"


def test_confirm_gate_translates_booking_card_into_session_language():
    session = Session(
        session_id="s1", hospital_id="glngs-chn", from_number="919876543210",
        state=SessionState.AWAITING_CONFIRM, history=[], role=Role.PATIENT,
        language_code="te-IN",
        pending_tool=ToolCall(tool_name="appointment", args={"action": "BOOK", "date": "2026-09-01"}),
    )
    repository = MagicMock()
    repository.get_session.return_value = session

    notifier     = MagicMock()
    orchestrator = WhatsAppOrchestrator(llm=MagicMock(), notifier=notifier, repository=repository)
    orchestrator._execute_tool = MagicMock(return_value={
        "action": "BOOK",
        "result": {
            "status": "CONFIRMED", "token_number": 1, "doctor_name": "Susan George",
            "department": "Cardiology", "hospital_name": "Test Hospital",
            "hospital_address": "", "fee": 500, "estimated_time": "2026-09-01T17:00:00",
        },
    })

    fake_labels = {"token": "టోకెన్", "doctor": "డాక్టర్"}  # partial is fine, format_booking_result falls back
    with patch("orchestrator.core.translate_labels", return_value=fake_labels) as mock_labels:
        wa_message = WAMessage(from_number="919876543210", message_id="m1", text="yes", hospital_id="glngs-chn")
        orchestrator.handle_message(wa_message)

    mock_labels.assert_called_once_with(orchestrator.llm, "te-IN")
    sent_text = notifier.send.call_args[0][1]
    assert "టోకెన్" in sent_text            # translated label used
    assert "డాక్టర్" in sent_text


def _session_awaiting_confirm(language_code):
    return Session(
        session_id="s1", hospital_id="glngs-chn", from_number="919876543210",
        state=SessionState.AWAITING_CONFIRM, history=[], role=Role.PATIENT,
        language_code=language_code,
        pending_tool=ToolCall(tool_name="appointment", args={"action": "BOOK", "date": "2026-09-01"}),
    )


def test_confirm_gate_translates_unrecognized_reply_prompt():
    session = _session_awaiting_confirm("te-IN")
    repository = MagicMock()
    repository.get_session.return_value = session

    notifier     = MagicMock()
    orchestrator = WhatsAppOrchestrator(llm=MagicMock(), notifier=notifier, repository=repository)

    with patch("orchestrator.core.translate_static", return_value="translated yes/no prompt") as mock_translate:
        wa_message = WAMessage(from_number="919876543210", message_id="m1", text="maybe later", hospital_id="glngs-chn")
        orchestrator.handle_message(wa_message)

    mock_translate.assert_called_once_with(orchestrator.llm, "Please reply *YES* to confirm or *NO* to cancel.", "te-IN")
    notifier.send.assert_called_once_with("919876543210", "translated yes/no prompt")


def test_confirm_gate_translates_negative_reply_message():
    session = _session_awaiting_confirm("te-IN")
    repository = MagicMock()
    repository.get_session.return_value = session

    notifier     = MagicMock()
    orchestrator = WhatsAppOrchestrator(llm=MagicMock(), notifier=notifier, repository=repository)

    with patch("orchestrator.core.translate_static", return_value="translated cancel message") as mock_translate:
        wa_message = WAMessage(from_number="919876543210", message_id="m1", text="no", hospital_id="glngs-chn")
        orchestrator.handle_message(wa_message)

    mock_translate.assert_called_once_with(orchestrator.llm, "Understood. Your request has been cancelled.", "te-IN")
    notifier.send.assert_called_once_with("919876543210", "translated cancel message")
