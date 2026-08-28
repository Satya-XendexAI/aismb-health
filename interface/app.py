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
import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel

from orchestrator import WhatsAppOrchestrator, InMemoryRepository, GeminiLLMAdapter, WAMessage
from interface.notifier import CaptureNotifier

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


class ChatMessage(BaseModel):
    text:        str
    from_number: str


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


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


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("interface.app:app", host="0.0.0.0", port=8001, reload=True)
