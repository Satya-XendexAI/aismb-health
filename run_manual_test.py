"""
WhatsApp Orchestrator — Manual CLI Test
----------------------------------------
Simulates a WhatsApp conversation in your terminal.
Bot responses print immediately; session state persists across turns.

Usage:
    pip install openai python-dotenv
    export GEMINI_API_KEY=...   # or set in .env
    python run_manual_test.py

Commands during chat:
    switch   — change the active phone number (known vs unknown patient)
    status   — print current session state
    reset    — clear session history for current number
    quit     — exit
"""

import json
import uuid
import logging
from orchestrator import (
    WhatsAppOrchestrator,
    WAMessage,
    GeminiLLMAdapter,
    PrintWANotifier,
    InMemoryRepository,
)

# ── Config ─────────────────────────────────────────────────────────────────────

HOSPITAL_ID = "glngs-chn"   # must match hospital_id in the DB

with open("config/doctors.json") as f:
    _cfg    = json.load(f)
    DOCTORS = _cfg["doctors"]
    ADMINS  = _cfg.get("admins", [])

# ── Helpers ────────────────────────────────────────────────────────────────────

def print_banner():
    print()
    print("=" * 55)
    print("  WhatsApp Orchestrator  —  Manual Test CLI")
    print("=" * 55)
    print("  Patient  : any number")
    print("  Doctor   : numbers in config/doctors.json → doctors")
    print("  Admin    : numbers in config/doctors.json → admins")
    print()
    print("  Commands: switch | status | reset | quit")
    print("=" * 55)
    print()

def _resolve_role(number: str) -> str:
    if any(a["phone"] == number for a in ADMINS):
        return "admin"
    if any(d["phone"] == number for d in DOCTORS):
        return "doctor"
    return "patient"

def print_status(from_number: str, repository: InMemoryRepository):
    session = repository.get_session(HOSPITAL_ID, from_number)
    if session is None:
        print(f"\n  [STATUS] No session yet for {from_number}\n")
        return
    print(f"\n  [STATUS] number={from_number}")
    print(f"           role={session.role.value}")
    print(f"           state={session.state.value}")
    print(f"           history_turns={len(session.history)}")
    print(f"           pending_tool={session.pending_tool}\n")

# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    logging.basicConfig(level=logging.WARNING)

    repository = InMemoryRepository(doctors=DOCTORS, admins=ADMINS)

    orc = WhatsAppOrchestrator(
        llm=GeminiLLMAdapter(),
        notifier=PrintWANotifier(),
        repository=repository,
        fallback_text="I'm sorry, I couldn't process that. Please try again.",
        max_iterations=5,
        max_history_turns=10,
    )

    print_banner()

    raw = input("  Enter your WhatsApp number: ").strip()
    from_number = raw if raw else "+91-9999999999"
    print(f"\n  Using: {from_number}  (role={_resolve_role(from_number)})\n")
    print("-" * 55)

    while True:
        try:
            text = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n\nGoodbye!")
            break

        if not text:
            continue

        if text.lower() == "quit":
            print("Goodbye!")
            break

        if text.lower() == "switch":
            raw = input("  New number (Enter for +91-known): ").strip()
            from_number = raw if raw else "+91-unknown"
            print(f"  Switched to: {from_number}  (role={_resolve_role(from_number)})\n")
            continue

        if text.lower() == "status":
            print_status(from_number, repository)
            continue

        if text.lower() == "reset":
            session = repository.get_session(HOSPITAL_ID, from_number)
            if session:
                session.history      = []
                session.pending_tool = None
                from orchestrator import SessionState
                session.state = SessionState.IDLE
                repository.save_session(session)
            print("  [RESET] Session cleared.\n")
            continue

        orc.handle_message(WAMessage(
            from_number=from_number,
            message_id=str(uuid.uuid4()),
            text=text,
            hospital_id=HOSPITAL_ID,
        ))


if __name__ == "__main__":
    main()
