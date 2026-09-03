"""
WhatsApp-style Chat Interface (local testing only)
----------------------------------------------------
A mobile WhatsApp-look chat UI that talks directly to the existing
WhatsAppOrchestrator — no Meta/WhatsApp API involved. Useful for fast
iteration on bot behavior without needing a real WhatsApp number.

Usage (run from the project root):
    uvicorn interface.app:app --reload --port 8001
    open http://localhost:8001
"""

import json
import logging
import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException, UploadFile, File, Form
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from orchestrator import WhatsAppOrchestrator, InMemoryRepository, GeminiLLMAdapter, WAMessage
from interface.notifier import CaptureNotifier
from whatsapp import transcribe_audio
from orchestrator.llm import translate_static

logging.basicConfig(level=logging.ERROR, format="%(name)s %(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

HOSPITAL_ID = "glngs-chn"
STATIC_DIR  = Path(__file__).parent / "static"

with open("config/doctors.json") as f:
    _cfg    = json.load(f)
    DOCTORS = _cfg["doctors"]
    ADMINS  = _cfg.get("admins", [])

# ── Orchestrator wiring ────────────────────────────────────────────────

notifier   = CaptureNotifier()
repository = InMemoryRepository(doctors=DOCTORS, admins=ADMINS)

orchestrator = WhatsAppOrchestrator(
    llm=GeminiLLMAdapter(),
    notifier=notifier,
    repository=repository,
)

# ── FastAPI app ─────────────────────────────────────────────────────────

app = FastAPI(title="WhatsApp Interface (Local Test)")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.middleware("http")
async def no_cache_static(request, call_next):
    """Local dev tool — always serve the latest static file, never a stale cached copy."""
    response = await call_next(request)
    if request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-store"
    return response


class ChatMessage(BaseModel):
    text:        str
    from_number: str


@app.get("/")
def index():
    # Version each static asset by its own mtime so the browser is forced to
    # fetch a fresh copy whenever app.js/style.css actually change on disk —
    # a stale cache can't match a URL it has never seen before.
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    for asset in ("app.js", "style.css"):
        version = int((STATIC_DIR / asset).stat().st_mtime)
        html = html.replace(f"/static/{asset}\"", f"/static/{asset}?v={version}\"")
    return HTMLResponse(html)


@app.get("/api/config")
def get_config():
    """Return known numbers and their roles for the login screen."""
    users = []
    for a in ADMINS:
        users.append({"phone": a["phone"], "name": a["name"], "role": "admin"})
    for d in DOCTORS:
        users.append({"phone": d["phone"], "name": d["name"], "role": "doctor"})
    return {"users": users, "hospital_id": HOSPITAL_ID}


@app.post("/api/send")
def send_message(message: ChatMessage):
    if not message.from_number.strip():
        raise HTTPException(status_code=400, detail="from_number is required")
    wa_message = WAMessage(
        from_number=message.from_number.strip(),
        message_id=str(uuid.uuid4()),
        text=message.text,
        hospital_id=HOSPITAL_ID,
    )
    orchestrator.handle_message(wa_message)
    return {"replies": notifier.drain()}


@app.post("/api/send-audio")
async def send_audio_message(from_number: str = Form(...), audio: UploadFile = File(...)):
    from_number = from_number.strip()
    if not from_number:
        raise HTTPException(status_code=400, detail="from_number is required")

    audio_bytes = await audio.read()
    try:
        transcript, language_code = transcribe_audio(audio_bytes, audio.content_type or "audio/webm")
    except Exception as exc:
        logger.error("Voice message transcription failed: %s", exc)
        return {"replies": ["Sorry, something went wrong processing your voice note. Please try typing instead."]}

    if not transcript.strip():
        text = translate_static(
            orchestrator.llm,
            "Sorry, I couldn't understand that voice note — could you type your message instead?",
            language_code,
        )
        return {"replies": [text]}

    wa_message = WAMessage(
        from_number=from_number,
        message_id=str(uuid.uuid4()),
        text=transcript,
        hospital_id=HOSPITAL_ID,
        language_code=language_code,
    )
    orchestrator.handle_message(wa_message)
    return {"replies": notifier.drain()}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("interface.app:app", host="0.0.0.0", port=8001, reload=True)
