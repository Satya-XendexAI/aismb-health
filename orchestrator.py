import sys
import uuid
import json
import logging
import os
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, List
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()  # reads .env from current directory

# Make appoint_tool importable (its internal imports are module-relative)
_APPOINT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "appoint_tool")
if _APPOINT_DIR not in sys.path:
    sys.path.insert(0, _APPOINT_DIR)

logger = logging.getLogger(__name__)

# ── Enums ──────────────────────────────────────────────────────────────────────

class SessionState(Enum):
    IDLE             = "IDLE"
    AWAITING_CONFIRM = "AWAITING_CONFIRM"

class Role(Enum):
    PATIENT = "patient"
    DOCTOR  = "doctor"

class ChatRole(Enum):
    USER        = "user"
    ASSISTANT   = "assistant"
    TOOL_RESULT = "tool_result"

class AgentResponseType(Enum):
    TOOL_CALL = "tool_call"
    TEXT      = "text"

class GateStatus(Enum):
    OK               = "OK"
    CONFIRM_REQUIRED = "CONFIRM_REQUIRED"
    FORBIDDEN        = "FORBIDDEN"

# ── Data Models ────────────────────────────────────────────────────────────────

@dataclass
class ToolCall:
    tool_name:        str
    args:             dict
    tool_use_id:      str           = field(default_factory=lambda: f"toolu_{uuid.uuid4().hex[:12]}")
    thought_signature: str | None   = None

@dataclass
class ChatTurn:
    role:      ChatRole
    content:   str
    tool_call: Optional["ToolCall"] = None

@dataclass
class WAMessage:
    from_number: str
    message_id:  str
    text:        str
    hospital_id: str

@dataclass
class Session:
    session_id:   str
    hospital_id:  str
    from_number:  str
    state:        SessionState
    history:      List[ChatTurn]
    pending_tool: Optional[ToolCall]
    role:         Role = Role.PATIENT

@dataclass
class AgentResponse:
    type:      AgentResponseType
    tool_call: Optional[ToolCall] = None
    text:      Optional[str]      = None

@dataclass
class GateResult:
    status: GateStatus

@dataclass
class OrchestratorContext:
    wa_message: WAMessage
    session:    Session

# ── Tool Schemas ───────────────────────────────────────────────────────────────

_appointment_schema = {
    "type": "function",
    "function": {
        "name": "appointment",
        "description": (
            "Book or cancel a token (queue-based) appointment for the patient. "
            "Always call kg_retriever first to get the doctor_id and department. "
            "Ask for patient_name before calling this tool if not already known."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action":           {"type": "string", "enum": ["BOOK", "CANCEL"],
                                     "description": "BOOK to book a new token, CANCEL to cancel existing"},
                "doctor_id":        {"type": "string",
                                     "description": "Doctor's ID from kg_retriever results (sql_id field)"},
                "department":       {"type": "string",
                                     "description": "Doctor's department or specialization"},
                "patient_name":     {"type": "string",
                                     "description": "Patient's full name"},
                "patient_age":      {"type": "integer",
                                     "description": "Patient's age (optional)"},
                "patient_location": {"type": "string",
                                     "description": "Patient's city or location (optional)"},
                "symptoms":         {"type": "string",
                                     "description": "Patient's symptoms (optional)"},
                "date":             {"type": "string",
                                     "description": "Appointment date in YYYY-MM-DD format (optional, defaults to today)"},
            },
            "required": ["action", "doctor_id", "department", "patient_name"],
        },
    },
}

_kg_retriever_schema = {
    "type": "function",
    "function": {
        "name": "kg_retriever",
        "description": "Find doctors by symptoms, specialization, name, language, or experience. Use for any query about finding or getting info on doctors.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "The user's query in their own words"},
            },
            "required": ["query"],
        },
    },
}

_query_data_schema = {
    "type": "function",
    "function": {
        "name": "query_data",
        "description": "Query your patients' appointment and token data using a natural language question.",
        "parameters": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "Natural language question about your patients or tokens",
                },
            },
            "required": ["question"],
        },
    },
}

PATIENT_TOOLS = [_appointment_schema, _kg_retriever_schema]
DOCTOR_TOOLS  = [_kg_retriever_schema, _query_data_schema]

ROLE_PERMISSIONS = {
    "appointment":  {Role.PATIENT},
    "query_data":   {Role.DOCTOR},
    "kg_retriever": {Role.PATIENT, Role.DOCTOR},
}

PATIENT_SYSTEM_PROMPT = (
    "You are a helpful hospital WhatsApp assistant. Help patients with:\n"
    "- Booking and cancelling doctor appointments\n"
    "- Questions about doctors, departments, timings, and procedures\n"
    "- Fetching their own medical records, test results, and prescriptions\n\n"
    "Be polite, concise, and professional. Use tools to retrieve accurate data. "
    "Never fabricate information."
)

DOCTOR_SYSTEM_PROMPT = (
    "You are a hospital assistant for medical staff. You can help with:\n"
    "- Searching for doctors by specialization, symptom, language, or name\n"
    "- Querying hospital data: appointments, test results, prescriptions, medications\n\n"
    "You cannot book or cancel appointments — that is handled by patients directly. "
    "Be concise and professional. Use tools to retrieve accurate data. Never fabricate information."
)

# ── In-Memory Repository ───────────────────────────────────────────────────────

class InMemoryRepository:
    """Stores sessions in memory. doctors is a list of doctor config dicts."""

    def __init__(self, doctors: list = None):
        self._sessions: dict = {}
        self._doctors:  list = doctors or []

    def get_session(self, hospital_id: str, from_number: str) -> Optional[Session]:
        return self._sessions.get(f"{hospital_id}:{from_number}")

    def save_session(self, session: Session):
        self._sessions[f"{session.hospital_id}:{session.from_number}"] = session

    def get_role(self, from_number: str) -> Role:
        return Role.DOCTOR if any(d["phone"] == from_number for d in self._doctors) else Role.PATIENT

    def get_doctor_config(self, from_number: str) -> dict | None:
        return next((d for d in self._doctors if d["phone"] == from_number), None)

# ── Gemini LLM Adapter ────────────────────────────────────────────────────────

class GeminiLLMAdapter:
    def __init__(
        self,
        model:    str = os.getenv("GEMINI_MODEL", "gemini-3.6-flash"),
        base_url: str = "https://generativelanguage.googleapis.com/v1beta/openai/",
        api_key:  str = os.getenv("GEMINI_API_KEY", ""),
    ):
        self.model  = model
        self.client = OpenAI(base_url=base_url, api_key=api_key, timeout=120.0)

    def run_agent(self, history: List[ChatTurn], tool_schemas: list, system_prompt: str = PATIENT_SYSTEM_PROMPT) -> AgentResponse:
        messages = [{"role": "system", "content": system_prompt}] + self._build_messages(history)

        completion = self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            tools=tool_schemas,
            tool_choice="auto",
            temperature=1.0,
            max_tokens=8192,
            stream=False,
        )

        choice        = completion.choices[0]
        finish_reason = choice.finish_reason
        message       = choice.message

        if finish_reason == "tool_calls" and message.tool_calls:
            tc  = message.tool_calls[0]
            sig = (tc.extra_content or {}).get("google", {}).get("thought_signature")
            return AgentResponse(
                type=AgentResponseType.TOOL_CALL,
                tool_call=ToolCall(
                    tool_name=tc.function.name,
                    args=json.loads(tc.function.arguments),
                    tool_use_id=tc.id,
                    thought_signature=sig,
                ),
            )

        return AgentResponse(
            type=AgentResponseType.TEXT,
            text=message.content or "",
        )

    def _build_messages(self, history: List[ChatTurn]) -> list:
        messages = []
        for i, turn in enumerate(history):
            if turn.role == ChatRole.USER:
                if messages and messages[-1]["role"] == "user":
                    messages[-1]["content"] += "\n" + turn.content
                else:
                    messages.append({"role": "user", "content": turn.content})

            elif turn.role == ChatRole.ASSISTANT:
                if turn.tool_call:
                    tc_entry = {
                        "id":       turn.tool_call.tool_use_id,
                        "type":     "function",
                        "function": {
                            "name":      turn.tool_call.tool_name,
                            "arguments": json.dumps(turn.tool_call.args),
                        },
                    }
                    if turn.tool_call.thought_signature:
                        tc_entry["extra_content"] = {
                            "google": {"thought_signature": turn.tool_call.thought_signature}
                        }
                    messages.append({
                        "role":       "assistant",
                        "content":    None,
                        "tool_calls": [tc_entry],
                    })
                else:
                    messages.append({"role": "assistant", "content": turn.content or " "})

            elif turn.role == ChatRole.TOOL_RESULT:
                tool_use_id = "unknown"
                for prev_turn in reversed(history[:i]):
                    if prev_turn.role == ChatRole.ASSISTANT and prev_turn.tool_call:
                        tool_use_id = prev_turn.tool_call.tool_use_id
                        break

                messages.append({
                    "role":         "tool",
                    "tool_call_id": tool_use_id,
                    "content":      turn.content,
                })

        return messages

# ── Print WA Notifier ──────────────────────────────────────────────────────────

class PrintWANotifier:
    def send(self, to_number: str, text: str):
        print(f"\n  Bot >> {text}\n")

# ── WhatsApp Orchestrator ──────────────────────────────────────────────────────

class WhatsAppOrchestrator:
    def __init__(
        self,
        llm,
        notifier,
        repository,
        fallback_text:     str = "I'm sorry, I couldn't process that. Please try again.",
        max_iterations:    int = 5,
        max_history_turns: int = 10,
    ):
        self.llm               = llm
        self.notifier          = notifier
        self.repository        = repository
        self.fallback_text     = fallback_text
        self.max_iterations    = max_iterations
        self.max_history_turns = max_history_turns

    def handle_message(self, wa_message: WAMessage):
        context = self._hydrate(wa_message)
        session = context.session

        # ── Step 1: Handle suspended states before appending user message ──────

        if session.state == SessionState.AWAITING_CONFIRM:
            reply = wa_message.text.strip().upper()

            if reply == "YES":
                tool_call = session.pending_tool
                session.pending_tool = None
                session.state        = SessionState.IDLE
                self.repository.save_session(session)
                self._log_tool(tool_call)
                result = self._execute_tool(tool_call, context)
                session.history.append(ChatTurn(
                    role=ChatRole.TOOL_RESULT,
                    content=json.dumps(result),
                ))
                # Fall through to ReAct loop — agent will generate final response

            elif reply == "NO":
                session.pending_tool = None
                session.state        = SessionState.IDLE
                session.history.append(ChatTurn(role=ChatRole.USER, content=wa_message.text))
                self._responder("Understood. Your request has been cancelled.", context)
                return

            else:
                # Patient changed intent — reset and treat as new message
                session.state        = SessionState.IDLE
                session.pending_tool = None
                session.history.append(ChatTurn(role=ChatRole.USER, content=wa_message.text))

        else:
            session.history.append(ChatTurn(role=ChatRole.USER, content=wa_message.text))

        # ── Step 2: ReAct loop ─────────────────────────────────────────────────

        if session.role == Role.DOCTOR:
            tool_schemas  = DOCTOR_TOOLS
            system_prompt = DOCTOR_SYSTEM_PROMPT
        else:
            tool_schemas  = PATIENT_TOOLS
            system_prompt = PATIENT_SYSTEM_PROMPT
        final_text = self.fallback_text

        for _ in range(self.max_iterations):
            print("  [Thinking...]", flush=True)
            try:
                agent_response = self.llm.run_agent(session.history, tool_schemas, system_prompt)
            except Exception as exc:
                logger.error("LLM error: %s", exc)
                print(f"  [LLM error: {exc}]")
                break

            if agent_response.type == AgentResponseType.TEXT:
                final_text = agent_response.text
                break

            # Tool call path
            session.history.append(ChatTurn(
                role=ChatRole.ASSISTANT,
                content="",
                tool_call=agent_response.tool_call,
            ))

            gate_result = self._gate(agent_response.tool_call, context)

            if gate_result.status == GateStatus.FORBIDDEN:
                self._responder("Sorry, you don't have permission to perform this action.", context)
                return

            if gate_result.status == GateStatus.CONFIRM_REQUIRED:
                self._interrupt(agent_response.tool_call, context)
                return

            # Gate OK — execute and loop
            self._log_tool(agent_response.tool_call)
            print("  [Executing tool, waiting for result...]", flush=True)
            result = self._execute_tool(agent_response.tool_call, context)
            session.history.append(ChatTurn(
                role=ChatRole.TOOL_RESULT,
                content=json.dumps(result),
            ))

        self._responder(final_text, context)

    # ── Private nodes ──────────────────────────────────────────────────────────

    def _hydrate(self, wa_message: WAMessage) -> OrchestratorContext:
        session = self.repository.get_session(wa_message.hospital_id, wa_message.from_number)

        if session is None:
            session = Session(
                session_id   = str(uuid.uuid4()),
                hospital_id  = wa_message.hospital_id,
                from_number  = wa_message.from_number,
                state        = SessionState.IDLE,
                history      = [],
                pending_tool = None,
                role         = self.repository.get_role(wa_message.from_number),
            )

        self.repository.save_session(session)
        return OrchestratorContext(wa_message, session)

    def _gate(self, tool_call: ToolCall, context: OrchestratorContext) -> GateResult:
        allowed = ROLE_PERMISSIONS.get(tool_call.tool_name, {Role.PATIENT, Role.DOCTOR})
        if context.session.role not in allowed:
            return GateResult(GateStatus.FORBIDDEN)
        if tool_call.tool_name == "appointment" and context.session.state != SessionState.AWAITING_CONFIRM:
            return GateResult(GateStatus.CONFIRM_REQUIRED)
        return GateResult(GateStatus.OK)

    def _execute_tool(self, tool_call: ToolCall, context: OrchestratorContext) -> dict:
        if tool_call.tool_name == "appointment":
            from app import handle_request
            payload = {
                **tool_call.args,
                "hospital_id":   context.wa_message.hospital_id,
                "patient_phone": context.wa_message.from_number,
            }
            return handle_request(payload)
        elif tool_call.tool_name == "kg_retriever":
            from tools.kg_retriever import retrieve_context
            return retrieve_context(**tool_call.args)
        elif tool_call.tool_name == "query_data":
            from tools.query_data import run_query
            return run_query(
                question=tool_call.args["question"],
                doctor_phone=context.wa_message.from_number,
                repository=self.repository,
            )
        return {"error": "unknown tool"}

    def _log_tool(self, tool_call: ToolCall):
        print(f"\n  [TOOL] {tool_call.tool_name} -> {json.dumps(tool_call.args, indent=2)}\n")

    def _interrupt(self, tool_call: ToolCall, context: OrchestratorContext):
        desc    = self._describe_tool(tool_call)
        message = f"Please confirm: {desc}. Reply YES to proceed or NO to cancel."
        context.session.pending_tool = tool_call
        context.session.state        = SessionState.AWAITING_CONFIRM

        self.repository.save_session(context.session)
        try:
            self.notifier.send(context.wa_message.from_number, message)
        except Exception:
            pass

    def _describe_tool(self, tool_call: ToolCall) -> str:
        action = tool_call.args.get("action", "BOOK").upper()
        doctor = tool_call.args.get("doctor_id", "the doctor")
        dept   = tool_call.args.get("department", "")
        name   = tool_call.args.get("patient_name", "")
        date   = tool_call.args.get("date", "today")
        desc   = f"{action} appointment with {doctor}"
        if dept:
            desc += f" ({dept})"
        if name:
            desc += f" for {name}"
        desc += f" on {date}"
        return desc

    def _responder(self, text: str, context: OrchestratorContext):
        for chunk in self._chunk(text, max_chars=1000):
            try:
                self.notifier.send(context.wa_message.from_number, chunk)
            except Exception:
                pass

        context.session.history.append(ChatTurn(role=ChatRole.ASSISTANT, content=text))
        context.session.history = context.session.history[-self.max_history_turns:]
        self.repository.save_session(context.session)

    @staticmethod
    def _chunk(text: str, max_chars: int) -> List[str]:
        if len(text) <= max_chars:
            return [text]
        chunks = []
        while len(text) > max_chars:
            split_at = text.rfind(". ", 0, max_chars)
            split_at = split_at + 1 if split_at != -1 else max_chars
            chunks.append(text[:split_at].strip())
            text = text[split_at:].strip()
        if text:
            chunks.append(text)
        return chunks
