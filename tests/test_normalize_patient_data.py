from unittest.mock import MagicMock, patch

from orchestrator.llm import normalize_to_english
from orchestrator.core import WhatsAppOrchestrator
from models.session import ToolCall


def _adapter_with_completion(text):
    message    = MagicMock(content=text)
    completion = MagicMock(choices=[MagicMock(message=message)])
    adapter = MagicMock()
    adapter.model = "gemini-3.6-flash"
    adapter.client.chat.completions.create.return_value = completion
    return adapter


# ── normalize_to_english ────────────────────────────────────────────────────

def test_normalize_to_english_transliterates_non_ascii_name():
    adapter = _adapter_with_completion("Pravali")

    result = normalize_to_english(adapter, "ప్రవళి")

    assert result == "Pravali"
    adapter.client.chat.completions.create.assert_called_once()


def test_normalize_to_english_leaves_ascii_text_untouched():
    adapter = _adapter_with_completion("should never be used")

    result = normalize_to_english(adapter, "Ramesh")

    assert result == "Ramesh"
    adapter.client.chat.completions.create.assert_not_called()


def test_normalize_to_english_leaves_empty_text_untouched():
    adapter = _adapter_with_completion("should never be used")

    assert normalize_to_english(adapter, "") == ""
    adapter.client.chat.completions.create.assert_not_called()


def test_normalize_to_english_falls_back_to_original_on_failure():
    adapter = MagicMock()
    adapter.model = "gemini-3.6-flash"
    adapter.client.chat.completions.create.side_effect = RuntimeError("down")

    result = normalize_to_english(adapter, "లక్ష్మి")

    assert result == "లక్ష్మి"


def test_normalize_to_english_falls_back_when_output_looks_malformed():
    adapter = _adapter_with_completion("Line 1: `Lakshmi` -> translated")

    result = normalize_to_english(adapter, "లక్ష్మి")

    assert result == "లక్ష్మి"


# ── orchestrator wiring ─────────────────────────────────────────────────────

def test_normalize_appointment_args_converts_name_location_and_symptoms():
    orchestrator = WhatsAppOrchestrator(llm=MagicMock(), notifier=MagicMock(), repository=MagicMock())
    tool_call = ToolCall(
        tool_name="appointment",
        args={
            "action": "BOOK",
            "patient_name": "ప్రవళి",
            "patient_location": "హైదరాబాద్",
            "symptoms": "కాళ్ళ నొప్పి",
        },
    )

    def fake_normalize(llm, text):
        return {"ప్రవళి": "Pravali", "హైదరాబాద్": "Hyderabad", "కాళ్ళ నొప్పి": "leg pain"}[text]

    with patch("orchestrator.core.normalize_to_english", side_effect=fake_normalize):
        orchestrator._normalize_appointment_args(tool_call)

    assert tool_call.args["patient_name"] == "Pravali"
    assert tool_call.args["patient_location"] == "Hyderabad"
    assert tool_call.args["symptoms"] == "leg pain"


def test_normalize_appointment_args_skips_missing_fields():
    orchestrator = WhatsAppOrchestrator(llm=MagicMock(), notifier=MagicMock(), repository=MagicMock())
    tool_call = ToolCall(tool_name="appointment", args={"action": "CANCEL", "patient_name": "Ramesh"})

    orchestrator._normalize_appointment_args(tool_call)  # real call: "Ramesh" is ASCII, no API hit

    assert tool_call.args == {"action": "CANCEL", "patient_name": "Ramesh"}
