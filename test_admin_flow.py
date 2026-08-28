"""
Admin Plan-Gate Flow — End-to-End Test
---------------------------------------
Scenario: Dr. Sharma is running 60 minutes late.
  - 4 patients waiting: 2 elderly+outstation, 2 local+young
  - Alternative doctors: Dr. Rao (4 in queue), Dr. Mehta (7 in queue)

Expected LLM reasoning:
  - Ravi  (72, Nellore)   → REASSIGN to Dr. Rao   (elderly + outstation)
  - Mohan (65, Bangalore) → REASSIGN to Dr. Rao   (elderly + outstation)
  - Priya (35, Chennai)   → SHIFT by 60 mins      (local, can wait)
  - Anjali(28, Chennai)   → SHIFT by 60 mins      (local, can wait)

Test flow:
  Turn 1 → admin says "Dr. Sharma is 1 hour late"
          → mock LLM calls get_session_impact, find_available_doctors, execute_plan
          → orchestrator fires plan gate, sends summary to admin
  Turn 2 → admin says "YES"
          → Phase 1: bulk_reschedule runs (mock DB)
          → Phase 2: notify_patients_bulk fires in background
          → admin gets confirmation with job_id

Run:
    source venv/bin/activate
    python test_admin_flow.py
"""

import json
import time
import types
import uuid
from unittest.mock import patch, MagicMock

# ── Test data ──────────────────────────────────────────────────────────────────

HOSPITAL_ID    = "glngs-chn"
ADMIN_PHONE    = "99999"
SESSION_ID     = "sess-sharma-0828"
ALT_SESSION_ID = "sess-rao-0828"

IMPACT_RESULT = {
    "session_id":    SESSION_ID,
    "doctor_name":   "Dr. Sharma",
    "department":    "Cardiology",
    "total_waiting": 4,
    "patients": [
        {"token_id": "t1", "token_number": 1, "patient_name": "Ravi Kumar",
         "patient_phone": "911111111111", "age": 72,
         "is_elderly": True,  "location": "Nellore",  "is_outstation": True,
         "distance_km": 170,  "distance_confidence": "high"},
        {"token_id": "t2", "token_number": 2, "patient_name": "Priya Nair",
         "patient_phone": "912222222222", "age": 35,
         "is_elderly": False, "location": "Chennai",  "is_outstation": False,
         "distance_km": 0,    "distance_confidence": "high"},
        {"token_id": "t3", "token_number": 3, "patient_name": "Mohan Rao",
         "patient_phone": "913333333333", "age": 65,
         "is_elderly": True,  "location": "Bangalore", "is_outstation": True,
         "distance_km": 350,  "distance_confidence": "high"},
        {"token_id": "t4", "token_number": 4, "patient_name": "Anjali Singh",
         "patient_phone": "914444444444", "age": 28,
         "is_elderly": False, "location": "Chennai",  "is_outstation": False,
         "distance_km": 0,    "distance_confidence": "high"},
    ],
}

AVAILABLE_DOCTORS_RESULT = {
    "specialization": "Cardiology",
    "available_doctors": [
        {"doctor_id": "dr-rao--chn", "doctor_name": "Dr. Rao",
         "session_id": ALT_SESSION_ID, "current_queue": 4,
         "started_at": "09:00:00", "estimated_session_end": "12:40"},
        {"doctor_id": "dr-mehta--chn", "doctor_name": "Dr. Mehta",
         "session_id": "sess-mehta-0828", "current_queue": 7,
         "started_at": "09:00:00", "estimated_session_end": "13:15"},
    ],
}

PLAN_ACTIONS = [
    {"action_type": "REASSIGN", "token_id": "t1", "patient_name": "Ravi Kumar",
     "patient_phone": "911111111111", "doctor_name": "Dr. Sharma",
     "new_doctor_id": "dr-rao--chn", "new_doctor_name": "Dr. Rao",
     "new_session_id": ALT_SESSION_ID,
     "notification_message": "Hi Ravi Kumar, your appointment has been moved to Dr. Rao. New token: #?. Apologies for the inconvenience."},
    {"action_type": "REASSIGN", "token_id": "t3", "patient_name": "Mohan Rao",
     "patient_phone": "913333333333", "doctor_name": "Dr. Sharma",
     "new_doctor_id": "dr-rao--chn", "new_doctor_name": "Dr. Rao",
     "new_session_id": ALT_SESSION_ID,
     "notification_message": "Hi Mohan Rao, your appointment has been moved to Dr. Rao. New token: #?. Apologies for the inconvenience."},
    {"action_type": "SHIFT", "token_id": "t2", "patient_name": "Priya Nair",
     "patient_phone": "912222222222", "doctor_name": "Dr. Sharma",
     "session_id": SESSION_ID, "delay_minutes": 60,
     "notification_message": "Hi Priya Nair, Dr. Sharma's session is running approximately 60 minutes late. Your estimated time has been updated."},
    {"action_type": "SHIFT", "token_id": "t4", "patient_name": "Anjali Singh",
     "patient_phone": "914444444444", "doctor_name": "Dr. Sharma",
     "session_id": SESSION_ID, "delay_minutes": 60,
     "notification_message": "Hi Anjali Singh, Dr. Sharma's session is running approximately 60 minutes late. Your estimated time has been updated."},
]

# ── Mock LLM ───────────────────────────────────────────────────────────────────
# Simulates the 3-step tool call sequence the real LLM would produce.

class MockLLMAdapter:
    """
    Turn 1 response sequence:
      call 1 → get_session_impact
      call 2 → find_available_doctors
      call 3 → execute_plan
    Turn 2 (YES) is handled by the orchestrator directly — LLM not called again.
    """
    def __init__(self):
        self._call_index = 0
        self._tool_sequence = [
            ("get_session_impact",    {"doctor_id": "dr-sharma--chn", "date": "2026-08-28"}),
            ("find_available_doctors", {"specialization": "Cardiology",  "date": "2026-08-28"}),
            ("execute_plan",          {"actions": PLAN_ACTIONS,
                                       "summary": "4 patients affected by Dr. Sharma 60-min delay"}),
        ]

    def run_agent(self, history, tool_schemas, system_prompt):
        from models.session import AgentResponse, AgentResponseType, ToolCall

        # If the last history entry is a tool_result for execute_plan, return a text response
        for turn in reversed(history):
            if turn.tool_call and turn.tool_call.tool_name == "execute_plan":
                return AgentResponse(type=AgentResponseType.TEXT,
                                     text="Plan submitted for your approval.")

        if self._call_index < len(self._tool_sequence):
            name, args = self._tool_sequence[self._call_index]
            self._call_index += 1
            return AgentResponse(
                type=AgentResponseType.TOOL_CALL,
                tool_call=ToolCall(tool_name=name, args=args),
            )

        return AgentResponse(type=AgentResponseType.TEXT, text="Done.")

# ── Mock DB tools ──────────────────────────────────────────────────────────────

_db_calls: list[str] = []

def mock_bulk_reschedule(actions, hospital_id):
    from tools.bulk_ops import BulkResult
    from models.session import PlanAction
    _db_calls.append(f"bulk_reschedule: {len(actions)} actions")
    # Simulate assigned token numbers for REASSIGN actions
    token_counter = 5
    succeeded = []
    for a in actions:
        if a.action_type == "REASSIGN":
            a.new_token_number = token_counter
            token_counter += 1
            a.notification_message = a.notification_message.replace("#?", f"#{a.new_token_number}")
        succeeded.append(a)
    return BulkResult(succeeded=succeeded, rolled_back=False)

# ── Notifier that captures messages ───────────────────────────────────────────

class CapturingNotifier:
    def __init__(self):
        self.messages: list[tuple[str, str]] = []

    def send(self, phone: str, text: str):
        self.messages.append((phone, text))

    def drain(self) -> list[tuple[str, str]]:
        out, self.messages = self.messages, []
        return out

# ── Test helpers ───────────────────────────────────────────────────────────────

PASS = "\033[92m✓\033[0m"
FAIL = "\033[91m✗\033[0m"

def check(label: str, condition: bool, detail: str = ""):
    icon = PASS if condition else FAIL
    line = f"  {icon}  {label}"
    if detail:
        line += f"  ({detail})"
    print(line)
    return condition

# ── Main test ──────────────────────────────────────────────────────────────────

def run():
    from orchestrator import WhatsAppOrchestrator, WAMessage, InMemoryRepository
    from models.session import SessionState, Role

    notifier   = CapturingNotifier()
    repository = InMemoryRepository(
        doctors=[],
        admins=[{"phone": ADMIN_PHONE, "name": "Admin"}],
    )
    llm = MockLLMAdapter()

    orc = WhatsAppOrchestrator(
        llm=llm,
        notifier=notifier,
        repository=repository,
    )

    failures = 0

    def wa(text: str) -> WAMessage:
        return WAMessage(from_number=ADMIN_PHONE, message_id=str(uuid.uuid4()),
                         text=text, hospital_id=HOSPITAL_ID)

    # ── TURN 1: Admin reports the delay ───────────────────────────────────────
    print("\n" + "=" * 60)
    print("  TURN 1 — Admin reports Dr. Sharma is 60 minutes late")
    print("=" * 60)

    with patch("tools.session_impact.get_session_impact", return_value=IMPACT_RESULT), \
         patch("tools.session_impact.find_available_doctors", return_value=AVAILABLE_DOCTORS_RESULT), \
         patch("tools.bulk_ops.bulk_reschedule", side_effect=mock_bulk_reschedule):

        orc.handle_message(wa("Dr. Sharma is running 1 hour late today"))

    msgs = notifier.drain()
    session = repository.get_session(HOSPITAL_ID, ADMIN_PHONE)

    if not check("Admin session exists", session is not None): failures += 1
    if not check("Role is ADMIN", session and session.role == Role.ADMIN): failures += 1
    if not check("State is AWAITING_CONFIRM",
                 session and session.state == SessionState.AWAITING_CONFIRM): failures += 1
    if not check("pending_plan is set", session and session.pending_plan is not None): failures += 1
    if not check("pending_tool is None", session and session.pending_tool is None): failures += 1

    plan = session.pending_plan if session else []
    if not check("Plan has 4 actions", len(plan) == 4,
                 f"got {len(plan)}"): failures += 1
    reassigns = [a for a in plan if a.action_type == "REASSIGN"]
    shifts    = [a for a in plan if a.action_type == "SHIFT"]
    retains   = [a for a in plan if a.action_type == "RETAIN"]
    if not check("2 REASSIGN actions (elderly+outstation)", len(reassigns) == 2,
                 f"got {len(reassigns)}"): failures += 1
    if not check("2 SHIFT actions (local patients)", len(shifts) == 2,
                 f"got {len(shifts)}"): failures += 1
    if not check("0 RETAIN actions", len(retains) == 0,
                 f"got {len(retains)}"): failures += 1

    if not check("Admin received plan summary", len(msgs) == 1): failures += 1
    if msgs:
        summary = msgs[0][1]
        if not check("Summary mentions REASSIGN", "reassigned" in summary.lower()): failures += 1
        if not check("Summary asks YES/NO", "YES" in summary and "NO" in summary): failures += 1
        print(f"\n  Plan summary sent to admin:\n")
        for line in summary.splitlines():
            print(f"    {line}")

    # ── TURN 2: Admin replies YES ──────────────────────────────────────────────
    print("\n" + "=" * 60)
    print("  TURN 2 — Admin replies YES")
    print("=" * 60)

    with patch("tools.session_impact.get_session_impact", return_value=IMPACT_RESULT), \
         patch("tools.session_impact.find_available_doctors", return_value=AVAILABLE_DOCTORS_RESULT), \
         patch("tools.bulk_ops.bulk_reschedule", side_effect=mock_bulk_reschedule):

        orc.handle_message(wa("YES"))

    msgs    = notifier.drain()
    session = repository.get_session(HOSPITAL_ID, ADMIN_PHONE)
    time.sleep(0.15)   # let background notification thread finish

    if not check("State reset to IDLE",
                 session and session.state == SessionState.IDLE): failures += 1
    if not check("pending_plan cleared",
                 session and session.pending_plan is None): failures += 1
    if not check("Admin received execution confirmation", len(msgs) == 1): failures += 1

    if msgs:
        confirm = msgs[0][1]
        if not check("Confirmation contains ✅",      "✅"   in confirm): failures += 1
        if not check("Confirmation mentions job_id",  "Job"  in confirm): failures += 1
        if not check("Confirmation shows 4/4 done",   "4/4"  in confirm): failures += 1
        print(f"\n  Confirmation sent to admin:\n    {confirm}")

    if not check("bulk_reschedule was called",
                 any("bulk_reschedule" in c for c in _db_calls)): failures += 1

    # ── TURN 3: Simulate admin replies NO (fresh session) ─────────────────────
    print("\n" + "=" * 60)
    print("  TURN 3 — Admin cancels a plan (replies NO)")
    print("=" * 60)

    llm2 = MockLLMAdapter()
    repo2 = InMemoryRepository(
        doctors=[],
        admins=[{"phone": ADMIN_PHONE, "name": "Admin"}],
    )
    notifier2 = CapturingNotifier()
    orc2 = WhatsAppOrchestrator(llm=llm2, notifier=notifier2, repository=repo2)

    with patch("tools.session_impact.get_session_impact", return_value=IMPACT_RESULT), \
         patch("tools.session_impact.find_available_doctors", return_value=AVAILABLE_DOCTORS_RESULT), \
         patch("tools.bulk_ops.bulk_reschedule", side_effect=mock_bulk_reschedule):
        orc2.handle_message(wa("Dr. Sharma is running 1 hour late today"))

    notifier2.drain()   # discard summary
    sess2 = repo2.get_session(HOSPITAL_ID, ADMIN_PHONE)
    if not check("Plan set before NO", sess2 and sess2.pending_plan is not None): failures += 1

    with patch("tools.bulk_ops.bulk_reschedule", side_effect=mock_bulk_reschedule):
        orc2.handle_message(wa("NO"))

    cancel_msgs = notifier2.drain()
    sess2 = repo2.get_session(HOSPITAL_ID, ADMIN_PHONE)

    if not check("State reset to IDLE after NO",
                 sess2 and sess2.state == SessionState.IDLE): failures += 1
    if not check("pending_plan cleared after NO",
                 sess2 and sess2.pending_plan is None): failures += 1
    if not check("Admin notified of cancellation", len(cancel_msgs) == 1): failures += 1
    if cancel_msgs:
        if not check("Cancel message says 'cancelled'",
                     "cancel" in cancel_msgs[0][1].lower()): failures += 1

    # ── Summary ────────────────────────────────────────────────────────────────
    print("\n" + "=" * 60)
    if failures == 0:
        print(f"  {PASS}  All checks passed")
    else:
        print(f"  {FAIL}  {failures} check(s) failed")
    print("=" * 60 + "\n")
    return failures


if __name__ == "__main__":
    import sys
    sys.exit(run())
