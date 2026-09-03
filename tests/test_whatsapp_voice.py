from unittest.mock import patch, MagicMock

from fastapi.testclient import TestClient

import whatsapp
from orchestrator import WAMessage

client = TestClient(whatsapp.app)

AUDIO_PAYLOAD = {
    "entry": [{"changes": [{"value": {"messages": [
        {"from": "919876543210", "id": "wamid.ABC123", "type": "audio",
         "audio": {"id": "1234567890123456", "mime_type": "audio/ogg; codecs=opus"}}
    ]}}]}]
}

TEXT_PAYLOAD = {
    "entry": [{"changes": [{"value": {"messages": [
        {"from": "919876543210", "id": "wamid.XYZ", "type": "text", "text": {"body": "hi"}}
    ]}}]}]
}

INCOMING_AUDIO = {
    "from_number": "919876543210",
    "message_id":  "wamid.ABC123",
    "media_id":    "1234567890123456",
    "mime_type":   "audio/ogg; codecs=opus",
}


# ── extract_audio_message ──────────────────────────────────────────────────

def test_extract_audio_message_returns_media_id_and_mime_type():
    result = whatsapp.extract_audio_message(AUDIO_PAYLOAD)
    assert result == INCOMING_AUDIO


def test_extract_audio_message_returns_none_for_text_message():
    assert whatsapp.extract_audio_message(TEXT_PAYLOAD) is None


# ── download_whatsapp_media ─────────────────────────────────────────────────

def test_download_whatsapp_media_returns_audio_bytes():
    media_lookup = MagicMock(**{"json.return_value": {"url": "https://lookaside.fbsbx.com/fake"}})
    audio = MagicMock(content=b"FAKE_OGG_BYTES")

    with patch("whatsapp.httpx.get", side_effect=[media_lookup, audio]) as mock_get:
        result = whatsapp.download_whatsapp_media("1234567890123456")

    assert result == b"FAKE_OGG_BYTES"
    assert mock_get.call_args_list[0][0][0] == "https://graph.facebook.com/v20.0/1234567890123456"
    assert mock_get.call_args_list[1][0][0] == "https://lookaside.fbsbx.com/fake"
    for call in mock_get.call_args_list:
        assert call.kwargs["headers"]["Authorization"] == f"Bearer {whatsapp.ACCESS_TOKEN}"


# ── transcribe_audio ─────────────────────────────────────────────────────────

def test_transcribe_audio_returns_transcript_text_and_language():
    sarvam_response = MagicMock(**{"json.return_value": {
        "transcript": "book an appointment", "language_code": "te-IN",
    }})

    with patch("whatsapp.httpx.post", return_value=sarvam_response) as mock_post:
        transcript, language_code = whatsapp.transcribe_audio(b"FAKE_AUDIO_BYTES", "audio/ogg")

    assert transcript == "book an appointment"
    assert language_code == "te-IN"
    call_kwargs = mock_post.call_args.kwargs
    assert call_kwargs["headers"]["api-subscription-key"] == whatsapp.SARVAM_API_KEY
    assert call_kwargs["data"] == {"model": "saaras:v4", "mode": "transcribe"}
    assert call_kwargs["files"]["file"][1] == b"FAKE_AUDIO_BYTES"
    assert call_kwargs["files"]["file"][2] == "audio/ogg"


def test_transcribe_audio_strips_codecs_parameter_sarvam_rejects():
    # Sarvam's file-type allowlist only matches bare MIME types and 400s on
    # anything with a ";codecs=..." parameter — exactly what MediaRecorder
    # (browser) and WhatsApp both send by default.
    sarvam_response = MagicMock(**{"json.return_value": {"transcript": "hello", "language_code": "en-IN"}})

    with patch("whatsapp.httpx.post", return_value=sarvam_response) as mock_post:
        whatsapp.transcribe_audio(b"FAKE_AUDIO_BYTES", "audio/webm;codecs=opus")

    assert mock_post.call_args.kwargs["files"]["file"][2] == "audio/webm"


# ── handle_audio_message ──────────────────────────────────────────────────────

def test_handle_audio_message_happy_path_calls_orchestrator():
    with patch("whatsapp.download_whatsapp_media", return_value=b"BYTES") as mock_dl, \
         patch("whatsapp.transcribe_audio", return_value=("book me an appointment", "te-IN")) as mock_tx, \
         patch.object(whatsapp.orchestrator, "handle_message") as mock_handle:

        whatsapp.handle_audio_message(INCOMING_AUDIO)

    mock_dl.assert_called_once_with("1234567890123456")
    mock_tx.assert_called_once_with(b"BYTES", "audio/ogg; codecs=opus")
    sent = mock_handle.call_args[0][0]
    assert isinstance(sent, WAMessage)
    assert sent.text == "book me an appointment"
    assert sent.from_number == "919876543210"
    assert sent.hospital_id == whatsapp.HOSPITAL_ID
    assert sent.language_code == "te-IN"


def test_handle_audio_message_empty_transcript_sends_fallback_text():
    with patch("whatsapp.download_whatsapp_media", return_value=b"BYTES"), \
         patch("whatsapp.transcribe_audio", return_value=("   ", "en-IN")), \
         patch.object(whatsapp.orchestrator, "handle_message") as mock_handle, \
         patch("whatsapp.WhatsAppNotifier.send") as mock_send:

        whatsapp.handle_audio_message(INCOMING_AUDIO)

    mock_handle.assert_not_called()
    assert "couldn't understand" in mock_send.call_args[0][1]


def test_handle_audio_message_download_failure_sends_error_text():
    with patch("whatsapp.download_whatsapp_media", side_effect=RuntimeError("network down")), \
         patch.object(whatsapp.orchestrator, "handle_message") as mock_handle, \
         patch("whatsapp.WhatsAppNotifier.send") as mock_send:

        whatsapp.handle_audio_message(INCOMING_AUDIO)

    mock_handle.assert_not_called()
    assert "something went wrong" in mock_send.call_args[0][1]


# ── webhook route ────────────────────────────────────────────────────────────

def test_webhook_routes_audio_message_to_background_task():
    with patch("whatsapp.WEBHOOK_SECRET", ""), \
         patch("whatsapp.handle_audio_message") as mock_handler:
        response = client.post("/webhook", json=AUDIO_PAYLOAD)

    assert response.status_code == 200
    mock_handler.assert_called_once_with(INCOMING_AUDIO)


def test_webhook_still_routes_text_message_to_orchestrator():
    with patch("whatsapp.WEBHOOK_SECRET", ""), \
         patch.object(whatsapp.orchestrator, "handle_message") as mock_handle:
        response = client.post("/webhook", json=TEXT_PAYLOAD)

    assert response.status_code == 200
    assert mock_handle.call_args[0][0].text == "hi"
