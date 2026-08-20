import uuid
import json
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, List
import anthropic

logger = logging.getLogger(__name__)

# ── Enums ──────────────────────────────────────────────────────────────────────

class SessionState(Enum):
    IDLE             = "IDLE"
    AWAITING_CONFIRM = "AWAITING_CONFIRM"
    AWAITING_AUTH    = "AWAITING_AUTH"

class ChatRole(Enum):
    USER        = "user"
    ASSISTANT   = "assistant"
    TOOL_RESULT = "tool_result"

class AgentResponseType(Enum):
    TOOL_CALL = "tool_call"
    TEXT      = "text"

class GateStatus(Enum):
    OK               = "OK"
    AUTH_REQUIRED    = "AUTH_REQUIRED"
    CONFIRM_REQUIRED = "CONFIRM_REQUIRED"

# ── Data Models ────────────────────────────────────────────────────────────────

@dataclass
class ToolCall:
    tool_name:   str
    args:        dict
    tool_use_id: str = field(default_factory=lambda: f"toolu_{uuid.uuid4().hex[:12]}")

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
    patient_id:   Optional[str]
    state:        SessionState
    history:      List[ChatTurn]
    pending_tool: Optional[ToolCall]

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
    patient_id: Optional[str]

# ── Tool Schemas (Anthropic format) ────────────────────────────────────────────

TOOL_SCHEMAS = [
    {
        "name": "appointment",
        "description": "Book or cancel a doctor appointment for the patient.",
        "input_schema": {
            "type": "object",
            "properties": {
                "action":      {"type": "string", "enum": ["book", "cancel"]},
                "doctor_name": {"type": "string", "description": "Name of the doctor"},
                "date":        {"type": "string", "description": "Date e.g. 2026-08-20"},
            },
            "required": ["action"],
        },
    },
    {
        "name": "kg_retriever",
        "description": "Answer questions about doctors, departments, timings, and hospital procedures.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "The patient's question"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "query_data",
        "description": "Fetch the patient's own records: appointments, test results, prescriptions, or medications.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query_type": {
                    "type": "string",
                    "enum": ["appointments", "test_results", "prescriptions", "medications"],
                },
                "filters": {"type": "object"},
            },
            "required": ["query_type"],
        },
    },
]

SYSTEM_PROMPT = (
    "You are a helpful hospital WhatsApp assistant. Help patients with:\n"
    "- Booking and cancelling doctor appointments\n"
    "- Questions about doctors, departments, timings, and procedures\n"
    "- Fetching their own medical records, test results, and prescriptions\n\n"
    "Be polite, concise, and professional. Use tools to retrieve accurate data. "
    "Never fabricate information."
)

# ── In-Memory Repository ───────────────────────────────────────────────────────

class InMemoryRepository:
    """Stores sessions in memory. known_patients maps from_number → patient_id."""

    def __init__(self, known_patients: dict = None):
        self._sessions: dict = {}
        self._patients: dict = known_patients or {}

    def get_session(self, hospital_id: str, from_number: str) -> Optional[Session]:
        return self._sessions.get(f"{hospital_id}:{from_number}")

    def save_session(self, session: Session):
        self._sessions[f"{session.hospital_id}:{session.from_number}"] = session

    def get_patient_id_by_phone(self, from_number: str, hospital_id: str) -> Optional[str]:
        return self._patients.get(from_number)

# ── Claude LLM Adapter ─────────────────────────────────────────────────────────

class ClaudeLLMAdapter:
    def __init__(self, model: str = "claude-haiku-4-5-20251001"):
        self.client = anthropic.Anthropic()
        self.model  = model

    def run_agent(self, history: List[ChatTurn], tool_schemas: list) -> AgentResponse:
        messages = self._build_messages(history)
        response = self.client.messages.create(
            model=self.model,
            max_tokens=1024,
            system=SYSTEM_PROMPT,
            tools=tool_schemas,
            messages=messages,
        )

        if response.stop_reason == "tool_use":
            block = next(b for b in response.content if b.type == "tool_use")
            return AgentResponse(
                type=AgentResponseType.TOOL_CALL,
                tool_call=ToolCall(
                    tool_name=block.name,
                    args=block.input,
                    tool_use_id=block.id,
                ),
            )

        text_block = next((b for b in response.content if b.type == "text"), None)
        return AgentResponse(
            type=AgentResponseType.TEXT,
            text=text_block.text if text_block else "",
        )

    def _build_messages(self, history: List[ChatTurn]) -> list:
        messages = []
        for i, turn in enumerate(history):
            if turn.role == ChatRole.USER:
                # Merge consecutive user messages (safety guard)
                if messages and messages[-1]["role"] == "user":
                    prev = messages[-1]["content"]
                    if isinstance(prev, str):
                        messages[-1]["content"] = prev + "\n" + turn.content
                    else:
                        messages[-1]["content"].append({"type": "text", "text": turn.content})
                else:
                    messages.append({"role": "user", "content": turn.content})

            elif turn.role == ChatRole.ASSISTANT:
                if turn.tool_call:
                    messages.append({
                        "role": "assistant",
                        "content": [{
                            "type":  "tool_use",
                            "id":    turn.tool_call.tool_use_id,
                            "name":  turn.tool_call.tool_name,
                            "input": turn.tool_call.args,
                        }],
                    })
                else:
                    messages.append({"role": "assistant", "content": turn.content or " "})

            elif turn.role == ChatRole.TOOL_RESULT:
                # Locate the tool_use_id from the nearest preceding ASSISTANT tool_call
                tool_use_id = "unknown"
                for prev_turn in reversed(history[:i]):
                    if prev_turn.role == ChatRole.ASSISTANT and prev_turn.tool_call:
                        tool_use_id = prev_turn.tool_call.tool_use_id
                        break

                block = {
                    "type":        "tool_result",
                    "tool_use_id": tool_use_id,
                    "content":     turn.content,
                }
                # tool_result must be user-role; merge if previous is already a user list
                if messages and messages[-1]["role"] == "user" and isinstance(messages[-1]["content"], list):
                    messages[-1]["content"].append(block)
                else:
                    messages.append({"role": "user", "content": [block]})

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
                result = self._execute_tool(tool_call)
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

        elif session.state == SessionState.AWAITING_AUTH:
            patient_id = self.repository.get_patient_id_by_phone(
                wa_message.from_number, wa_message.hospital_id
            )
            if patient_id:
                session.patient_id = patient_id
                context.patient_id = patient_id
                session.state      = SessionState.IDLE
                self.repository.save_session(session)
            session.history.append(ChatTurn(role=ChatRole.USER, content=wa_message.text))

        else:
            session.history.append(ChatTurn(role=ChatRole.USER, content=wa_message.text))

        # ── Step 2: ReAct loop ─────────────────────────────────────────────────

        final_text = self.fallback_text

        for _ in range(self.max_iterations):
            try:
                agent_response = self.llm.run_agent(session.history, TOOL_SCHEMAS)
            except Exception as exc:
                logger.error("LLM error: %s", exc)
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

            if gate_result.status in (GateStatus.AUTH_REQUIRED, GateStatus.CONFIRM_REQUIRED):
                self._interrupt(gate_result, agent_response.tool_call, context)
                return

            # Gate OK — execute and loop
            self._log_tool(agent_response.tool_call)
            result = self._execute_tool(agent_response.tool_call)
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
                patient_id   = None,
                state        = SessionState.IDLE,
                history      = [],
                pending_tool = None,
            )

        if session.patient_id is None:
            patient_id = self.repository.get_patient_id_by_phone(
                wa_message.from_number, wa_message.hospital_id
            )
            if patient_id:
                session.patient_id = patient_id

        self.repository.save_session(session)
        return OrchestratorContext(wa_message, session, patient_id=session.patient_id)

    def _gate(self, tool_call: ToolCall, context: OrchestratorContext) -> GateResult:
        if tool_call.tool_name == "appointment":
            if context.patient_id is None:
                return GateResult(GateStatus.AUTH_REQUIRED)
            if context.session.state != SessionState.AWAITING_CONFIRM:
                return GateResult(GateStatus.CONFIRM_REQUIRED)
        return GateResult(GateStatus.OK)

    def _execute_tool(self, tool_call: ToolCall) -> dict:
        if tool_call.tool_name == "appointment":
            return {"status": "ok", "result": "Appointment tool executed (dummy)"}
        elif tool_call.tool_name == "kg_retriever":
            return {"facts": ["Apollo Hospital Cardiology OPD: Mon-Sat 9am-1pm (dummy)"]}
        elif tool_call.tool_name == "query_data":
            return {"data": "Patient records query result (dummy)"}
        return {"error": "unknown tool"}

    def _log_tool(self, tool_call: ToolCall):
        print(f"\n  [TOOL] {tool_call.tool_name} -> {json.dumps(tool_call.args, indent=2)}\n")

    def _interrupt(self, gate_result: GateResult, tool_call: ToolCall, context: OrchestratorContext):
        if gate_result.status == GateStatus.AUTH_REQUIRED:
            message = (
                "To proceed, I need to verify your identity. "
                "Please share your Patient ID."
            )
            context.session.state = SessionState.AWAITING_AUTH

        else:  # CONFIRM_REQUIRED
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
        if tool_call.tool_name == "appointment":
            action = tool_call.args.get("action", "appointment")
            doctor = tool_call.args.get("doctor_name", "the doctor")
            date   = tool_call.args.get("date", "")
            return f"{action} appointment with {doctor}" + (f" on {date}" if date else "")
        if tool_call.tool_name == "kg_retriever":
            return f"knowledge graph query: {tool_call.args.get('query', '')}"
        return f"data query: {tool_call.args.get('query_type', '')}"

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
