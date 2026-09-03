# WhatsApp Voice Message Transcription Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let patients send WhatsApp voice notes to the bot; transcribe them with Deepgram and feed the transcript into the existing MediNexus AI pipeline exactly as if it had been typed.

**Architecture:** Extend the single-file webhook module (`whatsapp.py`) with an audio branch parallel to the existing text branch: detect `message.type == "audio"` → fetch the Meta Media ID → download the raw audio via two Graph API calls → transcribe it with Deepgram's REST API → build the existing `WAMessage` dataclass with the transcript as `.text` → hand off to the orchestrator's existing `handle_message()`, unchanged. All new code is additive to `whatsapp.py`; `orchestrator/`, `models/`, and `tools/` are not touched.

**Tech Stack:** Python, FastAPI, `httpx` (already a dependency — used for both the Graph API calls and the Deepgram REST call, no new runtime package), Deepgram REST transcription API, `pytest` + `unittest.mock` (new, test-only dependency — the repo currently has no test suite).

**Spec:** No separate spec document exists. Requirements were captured directly from the stakeholder in conversation on 2026-09-01: receive a WhatsApp voice note, resolve its Media ID, download the audio from Meta, transcribe with Deepgram, feed the transcript into the existing MediNexus AI/appointment pipeline, and return a text response over WhatsApp as usual. This plan is self-contained.

## Global Constraints

- No new runtime dependency for HTTP calls — reuse `httpx` (`requirements.txt:9`, already `>=0.27.0`) for the Deepgram REST call, not a Deepgram SDK.
- `DEEPGRAM_API_KEY` is read via `os.getenv("DEEPGRAM_API_KEY", "")` and must never be hardcoded in source. It is already present in `.env` (added prior to this plan, under `# ── Speech-to-Text (Deepgram) ──`).
- New functions must be **sync** (`def`, not `async def`), matching the existing style of `WhatsAppNotifier.send` (`whatsapp.py:55`) and `WhatsAppOrchestrator.handle_message` (`orchestrator/core.py:40`, itself sync).
- Voice-message handling must reuse the existing `WAMessage` dataclass and `orchestrator.handle_message()` entrypoint unmodified — zero changes to `orchestrator/`, `models/`, or `tools/`.
- All new WhatsApp-facing code lives in `whatsapp.py`, matching the existing single-file webhook module pattern — no new module is created for it.
- `k8s/secret.yaml` only needs a documentation-line addition. `deploy/create-secret.sh` already reads every active `KEY=VALUE` line out of `.env` dynamically (`deploy/create-secret.sh:34-41`), so no functional change is required there for the key to reach the cluster secret.
- This is the first test suite in the repository (confirmed: no `test_*.py` exists outside `venv/`). `pytest>=8.0.0` is added to `requirements.txt` as part of Task 1.

---

## File Structure

- **Modify:** `whatsapp.py` — add Deepgram config, `extract_audio_message()`, `download_whatsapp_media()`, `transcribe_audio()`, `handle_audio_message()`, and a new branch in `receive_webhook()`.
- **Create:** `tests/test_whatsapp_voice.py` — all tests for the above, using `pytest` + `unittest.mock` + FastAPI's `TestClient` (ships with `fastapi`, no extra dependency).
- **Modify:** `requirements.txt` — add `pytest>=8.0.0` under a `# Testing` comment.
- **Modify:** `k8s/secret.yaml` — add a `DEEPGRAM_API_KEY: "REPLACE_ME"` documentation line under a new `# Speech-to-Text` section.
- **Already done (outside this plan):** `.env` — `DEEPGRAM_API_KEY` added.

---

### Task 1: Deepgram config + audio message parsing

**Files:**
- Modify: `requirements.txt`
- Modify: `whatsapp.py:32-37` (config block), `whatsapp.py:86` (after `extract_text_message`)
- Modify: `k8s/secret.yaml`
- Test: `tests/test_whatsapp_voice.py` (new file)

**Interfaces:**
- Produces: `DEEPGRAM_API_KEY: str` (module-level constant in `whatsapp.py`), `DEEPGRAM_URL: str` (module-level constant), `extract_audio_message(payload: dict) -> dict | None` returning `{"from_number": str, "message_id": str, "media_id": str, "mime_type": str}` or `None`.

- [ ] **Step 1: Add pytest to requirements.txt**

Append to the end of `requirements.txt`:

```
# Testing
pytest>=8.0.0
```

- [ ] **Step 2: Install it**

Run: `pip install -r requirements.txt`
Expected: `pytest` installs without errors.

- [ ] **Step 3: Write the failing tests**

Create `tests/test_whatsapp_voice.py`:

```python
import whatsapp

AUDIO_PAYLOAD = {
    "entry": [
        {
            "changes": [
                {
                    "value": {
                        "messages": [
                            {
                                "from": "919876543210",
                                "id": "wamid.ABC123",
                                "type": "audio",
                                "audio": {
                                    "id": "1234567890123456",
                                    "mime_type": "audio/ogg; codecs=opus",
                                },
                            }
                        ]
                    }
                }
            ]
        }
    ]
}

TEXT_PAYLOAD = {
    "entry": [
        {
            "changes": [
                {
                    "value": {
                        "messages": [
                            {"from": "919876543210", "id": "wamid.XYZ", "type": "text", "text": {"body": "hi"}}
                        ]
                    }
                }
            ]
        }
    ]
}

AUDIO_PAYLOAD_NO_MIME = {
    "entry": [
        {
            "changes": [
                {
                    "value": {
                        "messages": [
                            {"from": "919876543210", "id": "wamid.ABC123", "type": "audio", "audio": {"id": "1234567890123456"}}
                        ]
                    }
                }
            ]
        }
    ]
}


def test_extract_audio_message_returns_media_id_and_mime_type():
    result = whatsapp.extract_audio_message(AUDIO_PAYLOAD)

    assert result == {
        "from_number": "919876543210",
        "message_id": "wamid.ABC123",
        "media_id": "1234567890123456",
        "mime_type": "audio/ogg; codecs=opus",
    }


def test_extract_audio_message_returns_none_for_text_message():
    assert whatsapp.extract_audio_message(TEXT_PAYLOAD) is None


def test_extract_audio_message_defaults_mime_type_when_missing():
    result = whatsapp.extract_audio_message(AUDIO_PAYLOAD_NO_MIME)
    assert result["mime_type"] == "audio/ogg"
```

- [ ] **Step 4: Run the tests to verify they fail**

Run: `pytest tests/test_whatsapp_voice.py -v`
Expected: `FAIL` — `AttributeError: module 'whatsapp' has no attribute 'extract_audio_message'`

- [ ] **Step 5: Add the config constants**

In `whatsapp.py`, immediately after the existing `GRAPH_API_URL` line (`whatsapp.py:37`):

```python
DEEPGRAM_API_KEY = os.getenv("DEEPGRAM_API_KEY", "")
DEEPGRAM_URL     = "https://api.deepgram.com/v1/listen"
```

- [ ] **Step 6: Add `extract_audio_message`**

In `whatsapp.py`, directly below `extract_text_message` (after `whatsapp.py:86`):

```python
def extract_audio_message(payload: dict) -> dict | None:
    """Pull the first audio message out of a Meta webhook payload, if present."""
    try:
        for entry in payload.get("entry", []):
            for change in entry.get("changes", []):
                for message in change.get("value", {}).get("messages", []):
                    if message.get("type") == "audio":
                        return {
                            "from_number": message["from"],
                            "message_id":  message["id"],
                            "media_id":    message["audio"]["id"],
                            "mime_type":   message["audio"].get("mime_type", "audio/ogg"),
                        }
    except (KeyError, TypeError) as exc:
        logger.error("Failed to parse audio webhook payload: %s", exc)
    return None
```

- [ ] **Step 7: Run the tests to verify they pass**

Run: `pytest tests/test_whatsapp_voice.py -v`
Expected: `3 passed`

- [ ] **Step 8: Add the documentation line to k8s/secret.yaml**

In `k8s/secret.yaml`, add a new section after the `WEBHOOK_SECRET` line:

```yaml
  # Speech-to-Text (Deepgram)
  DEEPGRAM_API_KEY: "REPLACE_ME"
```

- [ ] **Step 9: Commit**

```bash
git add requirements.txt whatsapp.py k8s/secret.yaml tests/test_whatsapp_voice.py
git commit -m "feat(whatsapp): add Deepgram config and audio message parsing"
```

---

### Task 2: Download audio from Meta

**Files:**
- Modify: `whatsapp.py` (after `extract_audio_message`, added in Task 1)
- Test: `tests/test_whatsapp_voice.py`

**Interfaces:**
- Consumes: `ACCESS_TOKEN: str` (`whatsapp.py:34`, existing).
- Produces: `download_whatsapp_media(media_id: str) -> bytes` — raises on any non-2xx response via `httpx.Response.raise_for_status()`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_whatsapp_voice.py`:

```python
from unittest.mock import patch, MagicMock


def test_download_whatsapp_media_returns_audio_bytes():
    media_lookup_response = MagicMock()
    media_lookup_response.json.return_value = {"url": "https://lookaside.fbsbx.com/whatsapp_business/attachments/fake-url"}
    media_lookup_response.raise_for_status.return_value = None

    audio_response = MagicMock()
    audio_response.content = b"FAKE_OGG_BYTES"
    audio_response.raise_for_status.return_value = None

    with patch("whatsapp.httpx.get", side_effect=[media_lookup_response, audio_response]) as mock_get:
        result = whatsapp.download_whatsapp_media("1234567890123456")

    assert result == b"FAKE_OGG_BYTES"
    assert mock_get.call_count == 2
    assert mock_get.call_args_list[0][0][0] == "https://graph.facebook.com/v20.0/1234567890123456"
    assert mock_get.call_args_list[1][0][0] == "https://lookaside.fbsbx.com/whatsapp_business/attachments/fake-url"


def test_download_whatsapp_media_sends_bearer_auth_on_both_calls():
    media_lookup_response = MagicMock()
    media_lookup_response.json.return_value = {"url": "https://lookaside.fbsbx.com/fake-url"}
    media_lookup_response.raise_for_status.return_value = None

    audio_response = MagicMock()
    audio_response.content = b"FAKE_OGG_BYTES"
    audio_response.raise_for_status.return_value = None

    with patch("whatsapp.httpx.get", side_effect=[media_lookup_response, audio_response]) as mock_get:
        whatsapp.download_whatsapp_media("1234567890123456")

    for call in mock_get.call_args_list:
        assert call.kwargs["headers"]["Authorization"] == f"Bearer {whatsapp.ACCESS_TOKEN}"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_whatsapp_voice.py -v -k download_whatsapp_media`
Expected: `FAIL` — `AttributeError: module 'whatsapp' has no attribute 'download_whatsapp_media'`

- [ ] **Step 3: Implement `download_whatsapp_media`**

In `whatsapp.py`, directly below `extract_audio_message`:

```python
def download_whatsapp_media(media_id: str) -> bytes:
    """Resolve a WhatsApp Media ID to a temporary URL, then download the raw bytes."""
    headers = {"Authorization": f"Bearer {ACCESS_TOKEN}"}

    meta = httpx.get(f"https://graph.facebook.com/v20.0/{media_id}", headers=headers, timeout=10.0)
    meta.raise_for_status()

    audio = httpx.get(meta.json()["url"], headers=headers, timeout=20.0)
    audio.raise_for_status()
    return audio.content
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/test_whatsapp_voice.py -v -k download_whatsapp_media`
Expected: `2 passed`

- [ ] **Step 5: Commit**

```bash
git add whatsapp.py tests/test_whatsapp_voice.py
git commit -m "feat(whatsapp): download voice note audio from Meta Graph API"
```

---

### Task 3: Transcribe audio with Deepgram

**Files:**
- Modify: `whatsapp.py` (after `download_whatsapp_media`, added in Task 2)
- Test: `tests/test_whatsapp_voice.py`

**Interfaces:**
- Consumes: `DEEPGRAM_API_KEY: str`, `DEEPGRAM_URL: str` (both from Task 1).
- Produces: `transcribe_audio(audio_bytes: bytes, mime_type: str) -> str` — raises on any non-2xx response via `httpx.Response.raise_for_status()`. Returns `""` (not `None`) if Deepgram returns no alternatives text — callers check `.strip()`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_whatsapp_voice.py`:

```python
def test_transcribe_audio_returns_transcript_text():
    deepgram_response = MagicMock()
    deepgram_response.raise_for_status.return_value = None
    deepgram_response.json.return_value = {
        "results": {
            "channels": [
                {"alternatives": [{"transcript": "I need to book an appointment with Dr Susan tomorrow"}]}
            ]
        }
    }

    with patch("whatsapp.httpx.post", return_value=deepgram_response) as mock_post:
        result = whatsapp.transcribe_audio(b"FAKE_OGG_BYTES", "audio/ogg; codecs=opus")

    assert result == "I need to book an appointment with Dr Susan tomorrow"
    call_kwargs = mock_post.call_args.kwargs
    assert call_kwargs["headers"]["Authorization"] == f"Token {whatsapp.DEEPGRAM_API_KEY}"
    assert call_kwargs["headers"]["Content-Type"] == "audio/ogg; codecs=opus"
    assert call_kwargs["content"] == b"FAKE_OGG_BYTES"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/test_whatsapp_voice.py -v -k transcribe_audio`
Expected: `FAIL` — `AttributeError: module 'whatsapp' has no attribute 'transcribe_audio'`

- [ ] **Step 3: Implement `transcribe_audio`**

In `whatsapp.py`, directly below `download_whatsapp_media`:

```python
def transcribe_audio(audio_bytes: bytes, mime_type: str) -> str:
    """Send raw audio bytes to Deepgram and return the transcript text."""
    headers = {"Authorization": f"Token {DEEPGRAM_API_KEY}", "Content-Type": mime_type}
    params  = {"model": "nova-2", "smart_format": "true", "punctuate": "true"}

    response = httpx.post(DEEPGRAM_URL, headers=headers, params=params, content=audio_bytes, timeout=30.0)
    response.raise_for_status()
    return response.json()["results"]["channels"][0]["alternatives"][0]["transcript"]
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `pytest tests/test_whatsapp_voice.py -v -k transcribe_audio`
Expected: `1 passed`

- [ ] **Step 5: Commit**

```bash
git add whatsapp.py tests/test_whatsapp_voice.py
git commit -m "feat(whatsapp): transcribe voice note audio via Deepgram"
```

---

### Task 4: Wire audio messages into the orchestrator and webhook route

**Files:**
- Modify: `whatsapp.py` (new `handle_audio_message` function, and `receive_webhook` at `whatsapp.py:123-140`)
- Test: `tests/test_whatsapp_voice.py`

**Interfaces:**
- Consumes: `download_whatsapp_media(media_id: str) -> bytes` (Task 2), `transcribe_audio(audio_bytes: bytes, mime_type: str) -> str` (Task 3), `extract_audio_message(payload: dict) -> dict | None` (Task 1), `WAMessage` dataclass (`orchestrator/__init__.py`, fields `from_number, message_id, text, hospital_id` per `models/session.py:45-49`), `orchestrator.handle_message(wa_message: WAMessage) -> None` (`orchestrator/core.py:40`, existing, unmodified), `WhatsAppNotifier.send(to_number: str, text: str) -> None` (`whatsapp.py:55`, existing, unmodified), `HOSPITAL_ID: str` (`whatsapp.py:32`, existing).
- Produces: `handle_audio_message(incoming: dict) -> None` — the background-task entrypoint for audio messages.

- [ ] **Step 1: Write the failing tests for `handle_audio_message`**

Append to `tests/test_whatsapp_voice.py`:

```python
from orchestrator import WAMessage

INCOMING_AUDIO = {
    "from_number": "919876543210",
    "message_id":  "wamid.ABC123",
    "media_id":    "1234567890123456",
    "mime_type":   "audio/ogg; codecs=opus",
}


def test_handle_audio_message_happy_path_calls_orchestrator():
    with patch("whatsapp.download_whatsapp_media", return_value=b"FAKE_OGG_BYTES") as mock_download, \
         patch("whatsapp.transcribe_audio", return_value="book me an appointment") as mock_transcribe, \
         patch.object(whatsapp.orchestrator, "handle_message") as mock_handle:

        whatsapp.handle_audio_message(INCOMING_AUDIO)

    mock_download.assert_called_once_with("1234567890123456")
    mock_transcribe.assert_called_once_with(b"FAKE_OGG_BYTES", "audio/ogg; codecs=opus")
    mock_handle.assert_called_once()

    sent_message = mock_handle.call_args[0][0]
    assert isinstance(sent_message, WAMessage)
    assert sent_message.text == "book me an appointment"
    assert sent_message.from_number == "919876543210"
    assert sent_message.message_id == "wamid.ABC123"
    assert sent_message.hospital_id == whatsapp.HOSPITAL_ID


def test_handle_audio_message_empty_transcript_sends_fallback_text():
    with patch("whatsapp.download_whatsapp_media", return_value=b"FAKE_OGG_BYTES"), \
         patch("whatsapp.transcribe_audio", return_value="   "), \
         patch.object(whatsapp.orchestrator, "handle_message") as mock_handle, \
         patch("whatsapp.WhatsAppNotifier.send") as mock_send:

        whatsapp.handle_audio_message(INCOMING_AUDIO)

    mock_handle.assert_not_called()
    mock_send.assert_called_once()
    assert mock_send.call_args[0][0] == "919876543210"
    assert "couldn't understand" in mock_send.call_args[0][1]


def test_handle_audio_message_download_failure_sends_error_text():
    with patch("whatsapp.download_whatsapp_media", side_effect=RuntimeError("network down")), \
         patch.object(whatsapp.orchestrator, "handle_message") as mock_handle, \
         patch("whatsapp.WhatsAppNotifier.send") as mock_send:

        whatsapp.handle_audio_message(INCOMING_AUDIO)

    mock_handle.assert_not_called()
    mock_send.assert_called_once()
    assert mock_send.call_args[0][0] == "919876543210"
    assert "something went wrong" in mock_send.call_args[0][1]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_whatsapp_voice.py -v -k handle_audio_message`
Expected: `FAIL` — `AttributeError: module 'whatsapp' has no attribute 'handle_audio_message'`

- [ ] **Step 3: Implement `handle_audio_message`**

In `whatsapp.py`, directly below `transcribe_audio`:

```python
def handle_audio_message(incoming: dict):
    """Background-task entrypoint: download, transcribe, and hand off a voice note."""
    try:
        audio_bytes = download_whatsapp_media(incoming["media_id"])
        transcript  = transcribe_audio(audio_bytes, incoming["mime_type"])

        if not transcript.strip():
            WhatsAppNotifier().send(
                incoming["from_number"],
                "Sorry, I couldn't understand that voice note — could you type your message instead?",
            )
            return

        wa_message = WAMessage(
            from_number=incoming["from_number"],
            message_id=incoming["message_id"],
            text=transcript,
            hospital_id=HOSPITAL_ID,
        )
        orchestrator.handle_message(wa_message)
    except Exception as exc:
        logger.error("Voice message handling failed: %s", exc)
        WhatsAppNotifier().send(
            incoming["from_number"],
            "Sorry, something went wrong processing your voice note. Please try typing instead.",
        )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/test_whatsapp_voice.py -v -k handle_audio_message`
Expected: `3 passed`

- [ ] **Step 5: Write the failing test for the webhook route branch**

Append to `tests/test_whatsapp_voice.py`:

```python
from fastapi.testclient import TestClient

client = TestClient(whatsapp.app)


def test_webhook_routes_audio_message_to_background_task():
    with patch("whatsapp.WEBHOOK_SECRET", ""), \
         patch("whatsapp.handle_audio_message") as mock_handler:
        response = client.post("/webhook", json=AUDIO_PAYLOAD)

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    mock_handler.assert_called_once()
    incoming_arg = mock_handler.call_args[0][0]
    assert incoming_arg["media_id"] == "1234567890123456"
    assert incoming_arg["from_number"] == "919876543210"


def test_webhook_still_routes_text_message_to_orchestrator():
    with patch("whatsapp.WEBHOOK_SECRET", ""), \
         patch.object(whatsapp.orchestrator, "handle_message") as mock_handle:
        response = client.post("/webhook", json=TEXT_PAYLOAD)

    assert response.status_code == 200
    mock_handle.assert_called_once()
    sent_message = mock_handle.call_args[0][0]
    assert sent_message.text == "hi"
```

- [ ] **Step 6: Run the tests to verify they fail**

Run: `pytest tests/test_whatsapp_voice.py -v -k webhook_routes`
Expected: `FAIL` — audio messages currently return `{"status": "ok"}` without ever calling `handle_audio_message` (it doesn't exist as a route branch yet), so `mock_handler.assert_called_once()` fails.

- [ ] **Step 7: Add the audio branch to `receive_webhook`**

Replace `whatsapp.py:130-138`:

```python
    incoming = extract_text_message(json.loads(body))
    if incoming:
        wa_message = WAMessage(
            from_number=incoming["from_number"],
            message_id=incoming["message_id"],
            text=incoming["text"],
            hospital_id=HOSPITAL_ID,
        )
        background_tasks.add_task(orchestrator.handle_message, wa_message)
```

with:

```python
    payload = json.loads(body)

    incoming_text = extract_text_message(payload)
    if incoming_text:
        wa_message = WAMessage(
            from_number=incoming_text["from_number"],
            message_id=incoming_text["message_id"],
            text=incoming_text["text"],
            hospital_id=HOSPITAL_ID,
        )
        background_tasks.add_task(orchestrator.handle_message, wa_message)
        return JSONResponse({"status": "ok"})

    incoming_audio = extract_audio_message(payload)
    if incoming_audio:
        background_tasks.add_task(handle_audio_message, incoming_audio)
```

(The trailing `return JSONResponse({"status": "ok"})` already at `whatsapp.py:140` covers both the audio branch and the no-match case — leave it in place.)

- [ ] **Step 8: Run the tests to verify they pass**

Run: `pytest tests/test_whatsapp_voice.py -v`
Expected: `12 passed`

- [ ] **Step 9: Run the full test suite**

Run: `pytest -v`
Expected: all tests pass (only this new file exists, so same count as Step 8).

- [ ] **Step 10: Commit**

```bash
git add whatsapp.py tests/test_whatsapp_voice.py
git commit -m "feat(whatsapp): route audio messages through transcription into the orchestrator"
```

---

## Manual End-to-End Verification (after Task 4)

Automated tests mock every network call; before considering this done, verify against real services once:

1. Deploy/run with `DEEPGRAM_API_KEY` set in the environment (already in `.env`).
2. Send a real WhatsApp voice note to the bot's number.
3. Confirm in logs (or a temporary debug log line) that: the Media ID was extracted, both Graph API calls succeeded, Deepgram returned a non-empty transcript, and `orchestrator.handle_message` ran with that transcript.
4. Confirm the bot's text reply arrives on WhatsApp as normal.
5. Send a silent/near-silent voice note and confirm the "couldn't understand" fallback message arrives instead of a crash or hang.
