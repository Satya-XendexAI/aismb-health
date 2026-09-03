"""
WhatsApp Webhook Service
-------------------------
Connects the WhatsAppOrchestrator to real WhatsApp messages via the Meta
Cloud API. Point Meta's webhook (via ngrok in dev) at POST /webhook.

Usage:
    pip install -r requirements.txt
    uvicorn whatsapp:app --reload --port 8000
"""

import os
import re
import json
import hmac
import hashlib
import logging

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Request, BackgroundTasks, Query
from fastapi.responses import PlainTextResponse, JSONResponse

from orchestrator import WhatsAppOrchestrator, InMemoryRepository, GeminiLLMAdapter, WAMessage
from orchestrator.llm import translate_static

load_dotenv()
logging.basicConfig(level=logging.ERROR, format="%(name)s %(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

# ── Config ──────────────────────────────────────────────────────────────

HOSPITAL_ID     = "glngs-chn"
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID", "")
ACCESS_TOKEN    = os.getenv("ACCESS_TOKEN", "")
VERIFY_TOKEN    = os.getenv("VERIFY_TOKEN", "")
WEBHOOK_SECRET  = os.getenv("WEBHOOK_SECRET", "")
GRAPH_API_URL   = f"https://graph.facebook.com/v20.0/{PHONE_NUMBER_ID}/messages"
SARVAM_API_KEY  = os.getenv("SARVAM_API_KEY", "")
SARVAM_URL      = "https://api.sarvam.ai/speech-to-text"

with open("config/doctors.json") as f:
    _cfg    = json.load(f)
    DOCTORS = _cfg["doctors"]
    ADMINS  = _cfg.get("admins", [])


# ── Outgoing messages ─────────────────────────────────────────────────────

def to_whatsapp_markdown(text: str) -> str:
    """Convert standard **bold** markdown to WhatsApp's own *bold* syntax."""
    return re.sub(r"\*\*(.+?)\*\*", r"*\1*", text)


class WhatsAppNotifier:
    """Sends outgoing text messages via the Meta Graph API."""

    def send(self, to_number: str, text: str):
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type":    "individual",
            "to":                to_number,
            "type":              "text",
            "text":              {"body": to_whatsapp_markdown(text), "preview_url": False},
        }
        headers = {"Authorization": f"Bearer {ACCESS_TOKEN}"}

        response = httpx.post(GRAPH_API_URL, headers=headers, json=payload, timeout=10.0)
        if response.status_code != 200:
            logger.error("WhatsApp send failed (%s): %s", response.status_code, response.text)


# ── Incoming messages ─────────────────────────────────────────────────────

def extract_text_message(payload: dict) -> dict | None:
    """Pull the first text message out of a Meta webhook payload, if present."""
    try:
        for entry in payload.get("entry", []):
            for change in entry.get("changes", []):
                for message in change.get("value", {}).get("messages", []):
                    if message.get("type") == "text":
                        return {
                            "from_number": message["from"],
                            "message_id":  message["id"],
                            "text":        message["text"]["body"],
                        }
    except (KeyError, TypeError) as exc:
        logger.error("Failed to parse webhook payload: %s", exc)
    return None


def extract_audio_message(payload: dict) -> dict | None:
    """Pull the first audio (voice note) message out of a Meta webhook payload, if present."""
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


def download_whatsapp_media(media_id: str) -> bytes:
    """Resolve a WhatsApp Media ID to a temporary URL, then download the raw bytes."""
    headers = {"Authorization": f"Bearer {ACCESS_TOKEN}"}

    meta = httpx.get(f"https://graph.facebook.com/v20.0/{media_id}", headers=headers, timeout=10.0)
    meta.raise_for_status()

    audio = httpx.get(meta.json()["url"], headers=headers, timeout=20.0)
    audio.raise_for_status()
    return audio.content


def transcribe_audio(audio_bytes: bytes, mime_type: str) -> tuple[str, str]:
    """Send raw audio bytes to Sarvam AI; return (transcript, detected_language_code).

    Sarvam supports Indian languages (Hindi, Telugu, Tamil, Kannada, ...)
    that Deepgram's free tier doesn't. language_code is left unset in the
    request so it auto-detects the spoken language per message; that
    detected language (e.g. "te-IN") is returned so replies can match it.
    """
    # Sarvam's file-type allowlist matches bare MIME types only — strip any
    # ";codecs=..." parameter that browsers (MediaRecorder) and WhatsApp both
    # attach, e.g. "audio/webm;codecs=opus" -> "audio/webm", or it 400s.
    base_mime_type = mime_type.split(";")[0].strip()

    headers = {"api-subscription-key": SARVAM_API_KEY}
    data    = {"model": "saaras:v4", "mode": "transcribe"}
    files   = {"file": ("audio", audio_bytes, base_mime_type)}

    response = httpx.post(SARVAM_URL, headers=headers, data=data, files=files, timeout=30.0)
    response.raise_for_status()
    body = response.json()
    return body["transcript"], body.get("language_code", "en-IN")


def handle_audio_message(incoming: dict):
    """Background-task entrypoint: download, transcribe, and hand off a voice note."""
    try:
        audio_bytes = download_whatsapp_media(incoming["media_id"])
        transcript, language_code = transcribe_audio(audio_bytes, incoming["mime_type"])

        if not transcript.strip():
            text = translate_static(
                orchestrator.llm,
                "Sorry, I couldn't understand that voice note — could you type your message instead?",
                language_code,
            )
            WhatsAppNotifier().send(incoming["from_number"], text)
            return

        wa_message = WAMessage(
            from_number=incoming["from_number"],
            message_id=incoming["message_id"],
            text=transcript,
            hospital_id=HOSPITAL_ID,
            language_code=language_code,
        )
        orchestrator.handle_message(wa_message)
    except Exception as exc:
        logger.error("Voice message handling failed: %s", exc)
        WhatsAppNotifier().send(
            incoming["from_number"],
            "Sorry, something went wrong processing your voice note. Please try typing instead.",
        )


def verify_signature(body: bytes, signature_header: str) -> bool:
    if not WEBHOOK_SECRET:
        return True
    expected = "sha256=" + hmac.new(WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature_header, expected)


# ── Orchestrator wiring ────────────────────────────────────────────────────

repository = InMemoryRepository(doctors=DOCTORS, admins=ADMINS)

orchestrator = WhatsAppOrchestrator(
    llm=GeminiLLMAdapter(),
    notifier=WhatsAppNotifier(),
    repository=repository,
)


# ── FastAPI app ─────────────────────────────────────────────────────────────

app = FastAPI(title="WhatsApp Webhook Service")


@app.get("/webhook")
def verify_webhook(
    hub_mode:         str = Query(None, alias="hub.mode"),
    hub_verify_token: str = Query(None, alias="hub.verify_token"),
    hub_challenge:    str = Query(None, alias="hub.challenge"),
):
    if hub_mode == "subscribe" and hub_verify_token == VERIFY_TOKEN:
        return PlainTextResponse(hub_challenge)
    return JSONResponse({"error": "verification failed"}, status_code=403)


@app.post("/webhook")
async def receive_webhook(request: Request, background_tasks: BackgroundTasks):
    body = await request.body()

    if not verify_signature(body, request.headers.get("X-Hub-Signature-256", "")):
        return JSONResponse({"error": "invalid signature"}, status_code=403)

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

    return JSONResponse({"status": "ok"})


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("whatsapp:app", host="0.0.0.0", port=int(os.getenv("PORT", 8000)), reload=True)
