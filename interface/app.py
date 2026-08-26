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

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel

from orchestrator import WhatsAppOrchestrator, InMemoryRepository, GeminiLLMAdapter, WAMessage
from interface.notifier import CaptureNotifier

# ── Config ──────────────────────────────────────────────────────────────
# The number this interface pretends t===========================+o be. Any value works for patient
# testing. To test the doctor flow, use the exact phone value (digits
# only, e.g. "916300769676") from config/doctors.json.
PATIENT_NUMBER = "911234567463"

HOSPITAL_ID = "glngs-chn"
STATIC_DIR  = Path(__file__).parent / "static"

with open("config/doctors.json") as f:
    DOCTORS = json.load(f)["doctors"]

# ── Orchestrator wiring ────────────────────────────────────────────────

notifier   = CaptureNotifier()
repository = InMemoryRepository(doctors=DOCTORS)

orchestrator = WhatsAppOrchestrator(
    llm=GeminiLLMAdapter(),
    notifier=notifier,
    repository=repository,
)

# ── FastAPI app ─────────────────────────────────────────────────────────

app = FastAPI(title="WhatsApp Interface (Local Test)")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


class ChatMessage(BaseModel):
    text: str


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.post("/api/send")
def send_message(message: ChatMessage):
    wa_message = WAMessage(
        from_number=PATIENT_NUMBER,
        message_id=str(uuid.uuid4()),
        text=message.text,
        hospital_id=HOSPITAL_ID,
    )
    orchestrator.handle_message(wa_message)
    return {"replies": notifier.drain()}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("interface.app:app", host="0.0.0.0", port=8001, reload=True)
