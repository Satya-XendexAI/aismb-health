from unittest.mock import patch

from fastapi.testclient import TestClient

import interface.app as app_module

client = TestClient(app_module.app)

AUDIO_FILE = {"audio": ("voice.webm", b"FAKE_AUDIO_BYTES", "audio/webm")}


def test_send_audio_transcribes_and_returns_replies():
    with patch("interface.app.transcribe_audio", return_value=("book me an appointment", "te-IN")) as mock_tx, \
         patch.object(app_module.orchestrator, "handle_message") as mock_handle:
        mock_handle.side_effect = lambda wa: app_module.notifier.send(wa.from_number, "Sure, let's book it")

        response = client.post("/api/send-audio", data={"from_number": "919876543210"}, files=AUDIO_FILE)

    assert response.status_code == 200
    assert response.json() == {"replies": ["Sure, let's book it"]}
    mock_tx.assert_called_once_with(b"FAKE_AUDIO_BYTES", "audio/webm")
    sent = mock_handle.call_args[0][0]
    assert sent.language_code == "te-IN"


def test_send_audio_empty_transcript_returns_fallback_reply():
    with patch("interface.app.transcribe_audio", return_value=("   ", "en-IN")), \
         patch.object(app_module.orchestrator, "handle_message") as mock_handle:
        response = client.post("/api/send-audio", data={"from_number": "919876543210"}, files=AUDIO_FILE)

    assert response.status_code == 200
    assert "couldn't understand" in response.json()["replies"][0]
    mock_handle.assert_not_called()


def test_send_audio_missing_from_number_returns_400():
    response = client.post("/api/send-audio", data={"from_number": " "}, files=AUDIO_FILE)
    assert response.status_code == 400
