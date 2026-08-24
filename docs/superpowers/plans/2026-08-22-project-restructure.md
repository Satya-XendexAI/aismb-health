# Project Restructure Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Split the 518-line `orchestrator.py` monolith and `appoint_tool/` island into a clean modular layout with `models/`, `prompts/`, `orchestrator/`, and `tools/appointment/`.

**Architecture:** Pure data contracts in `models/`, prompt strings in `prompts/`, orchestration logic split across four focused files in `orchestrator/`, booking engine moved to `tools/appointment/`. `run_manual_test.py` import line is unchanged.

**Tech Stack:** Python 3.11, pydantic, psycopg2-binary, openai, python-dotenv

## Global Constraints

- `run_manual_test.py` must not change — `orchestrator/__init__.py` re-exports everything it imports
- No `sys.path.insert` hacks anywhere in the final codebase
- `appoint_tool/config.py` is deleted — `tools/appointment/database.py` uses `os.getenv()` + `load_dotenv()` directly
- All new files follow the existing no-comments-unless-non-obvious style

---

### Task 1: Create `models/` package

**Files:**
- Create: `models/__init__.py`
- Create: `models/session.py`
- Create: `models/appointment.py`

**Interfaces:**
- Produces: all enums and dataclasses consumed by orchestrator and tools

- [ ] **Step 1: Create `models/__init__.py`** (empty)

```python
```

- [ ] **Step 2: Create `models/session.py`**

```python
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, List


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


@dataclass
class ToolCall:
    tool_name:         str
    args:              dict
    tool_use_id:       str          = field(default_factory=lambda: f"toolu_{uuid.uuid4().hex[:12]}")
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
```

- [ ] **Step 3: Create `models/appointment.py`**

```python
from datetime import datetime
from typing import Literal, Optional, Union
from pydantic import BaseModel


class IncomingPayload(BaseModel):
    action:           Literal["BOOK", "CANCEL"]
    hospital_id:      str
    doctor_id:        str
    department:       str
    patient_name:     str
    patient_phone:    str
    patient_age:      Optional[int] = None
    patient_location: Optional[str] = None
    symptoms:         Optional[str] = None
    date:             Optional[str] = None  # YYYY-MM-DD


class BookingConfirmation(BaseModel):
    status:           Literal["CONFIRMED"]
    token_number:     int
    doctor_name:      str
    department:       str
    hospital_name:    str
    hospital_address: Optional[str]   = None
    fee:              Optional[float] = None
    estimated_time:   datetime


class CancellationResult(BaseModel):
    status:  Literal["CANCELLED", "PATIENT_NOT_FOUND", "NO_ACTIVE_BOOKING"]
    message: str


class ErrorResult(BaseModel):
    status:     Literal["ERROR"]
    error_code: str
    message:    str


class BookingResponse(BaseModel):
    action: Literal["BOOK", "CANCEL"]
    result: Union[BookingConfirmation, CancellationResult, ErrorResult]
```

- [ ] **Step 4: Smoke-test imports**

```bash
source venv/bin/activate
python -c "from models.session import Session, Role, WAMessage; from models.appointment import IncomingPayload, BookingResponse; print('OK')"
```

Expected: `OK`

- [ ] **Step 5: Commit**

```bash
git add models/
git commit -m "refactor(health): add models/ package — session + appointment contracts"
```

---

### Task 2: Create `prompts/` package

**Files:**
- Create: `prompts/__init__.py`
- Create: `prompts/system.py`

**Interfaces:**
- Produces: `PATIENT_SYSTEM_PROMPT: str`, `DOCTOR_SYSTEM_PROMPT: str`

- [ ] **Step 1: Create `prompts/__init__.py`** (empty)

```python
```

- [ ] **Step 2: Create `prompts/system.py`**

```python
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
```

- [ ] **Step 3: Smoke-test**

```bash
python -c "from prompts.system import PATIENT_SYSTEM_PROMPT, DOCTOR_SYSTEM_PROMPT; print('OK')"
```

- [ ] **Step 4: Commit**

```bash
git add prompts/
git commit -m "refactor(health): add prompts/ package — system prompt strings"
```

---

### Task 3: Create `orchestrator/` package

**Files:**
- Create: `orchestrator/__init__.py`
- Create: `orchestrator/schemas.py`
- Create: `orchestrator/session.py`
- Create: `orchestrator/llm.py`
- Create: `orchestrator/core.py`

**Interfaces:**
- Consumes: `models/session.py`, `models/appointment.py`, `prompts/system.py`
- Produces: `WhatsAppOrchestrator`, `InMemoryRepository`, `GeminiLLMAdapter`, `PrintWANotifier`

- [ ] **Step 1: Create `orchestrator/schemas.py`**

```python
from models.session import Role

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
```

- [ ] **Step 2: Create `orchestrator/session.py`**

```python
from typing import Optional
from models.session import Session, Role


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
```

- [ ] **Step 3: Create `orchestrator/llm.py`**

```python
import json
import os
from typing import List
from openai import OpenAI
from dotenv import load_dotenv

from models.session import (
    ChatTurn, ChatRole, AgentResponse, AgentResponseType, ToolCall,
)
from prompts.system import PATIENT_SYSTEM_PROMPT

load_dotenv()


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
                tool_name   = "unknown"
                for prev_turn in reversed(history[:i]):
                    if prev_turn.role == ChatRole.ASSISTANT and prev_turn.tool_call:
                        tool_use_id = prev_turn.tool_call.tool_use_id
                        tool_name   = prev_turn.tool_call.tool_name
                        break
                messages.append({
                    "role":         "tool",
                    "tool_call_id": tool_use_id,
                    "name":         tool_name,
                    "content":      turn.content,
                })

        return messages


class PrintWANotifier:
    def send(self, to_number: str, text: str):
        print(f"\n  Bot >> {text}\n")
```

- [ ] **Step 4: Create `orchestrator/core.py`**

```python
import uuid
import json
import logging
from typing import List

from models.session import (
    Session, SessionState, Role, ChatRole, ChatTurn,
    AgentResponseType, GateStatus, GateResult, OrchestratorContext, WAMessage,
)
from orchestrator.schemas import PATIENT_TOOLS, DOCTOR_TOOLS, ROLE_PERMISSIONS
from prompts.system import PATIENT_SYSTEM_PROMPT, DOCTOR_SYSTEM_PROMPT

logger = logging.getLogger(__name__)


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

        if session.state == SessionState.AWAITING_CONFIRM:
            reply = wa_message.text.strip().upper()

            if reply == "YES":
                tool_call            = session.pending_tool
                session.pending_tool = None
                session.state        = SessionState.IDLE
                self.repository.save_session(session)
                self._log_tool(tool_call)
                result = self._execute_tool(tool_call, context)
                session.history.append(ChatTurn(
                    role=ChatRole.TOOL_RESULT,
                    content=json.dumps(result),
                ))

            elif reply == "NO":
                session.pending_tool = None
                session.state        = SessionState.IDLE
                session.history.append(ChatTurn(role=ChatRole.USER, content=wa_message.text))
                self._responder("Understood. Your request has been cancelled.", context)
                return

            else:
                session.state        = SessionState.IDLE
                session.pending_tool = None
                session.history.append(ChatTurn(role=ChatRole.USER, content=wa_message.text))

        else:
            session.history.append(ChatTurn(role=ChatRole.USER, content=wa_message.text))

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

            self._log_tool(agent_response.tool_call)
            print("  [Executing tool, waiting for result...]", flush=True)
            result = self._execute_tool(agent_response.tool_call, context)
            session.history.append(ChatTurn(
                role=ChatRole.TOOL_RESULT,
                content=json.dumps(result),
            ))

        self._responder(final_text, context)

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

    def _gate(self, tool_call, context: OrchestratorContext) -> GateResult:
        allowed = ROLE_PERMISSIONS.get(tool_call.tool_name, {Role.PATIENT, Role.DOCTOR})
        if context.session.role not in allowed:
            return GateResult(GateStatus.FORBIDDEN)
        if tool_call.tool_name == "appointment" and context.session.state != SessionState.AWAITING_CONFIRM:
            return GateResult(GateStatus.CONFIRM_REQUIRED)
        return GateResult(GateStatus.OK)

    def _execute_tool(self, tool_call, context: OrchestratorContext) -> dict:
        if tool_call.tool_name == "appointment":
            from tools.appointment import handle_request
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

    def _log_tool(self, tool_call):
        print(f"\n  [TOOL] {tool_call.tool_name} -> {json.dumps(tool_call.args, indent=2)}\n")

    def _interrupt(self, tool_call, context: OrchestratorContext):
        desc    = self._describe_tool(tool_call)
        message = f"Please confirm: {desc}. Reply YES to proceed or NO to cancel."
        context.session.pending_tool = tool_call
        context.session.state        = SessionState.AWAITING_CONFIRM
        self.repository.save_session(context.session)
        try:
            self.notifier.send(context.wa_message.from_number, message)
        except Exception:
            pass

    def _describe_tool(self, tool_call) -> str:
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
```

- [ ] **Step 5: Create `orchestrator/__init__.py`**

```python
from orchestrator.core    import WhatsAppOrchestrator
from orchestrator.session import InMemoryRepository
from orchestrator.llm     import GeminiLLMAdapter, PrintWANotifier
from models.session       import WAMessage, SessionState

__all__ = [
    "WhatsAppOrchestrator",
    "InMemoryRepository",
    "GeminiLLMAdapter",
    "PrintWANotifier",
    "WAMessage",
    "SessionState",
]
```

- [ ] **Step 6: Smoke-test imports**

```bash
python -c "
from orchestrator import WhatsAppOrchestrator, InMemoryRepository, WAMessage, GeminiLLMAdapter, PrintWANotifier, SessionState
print('OK')
"
```

Expected: `OK`

- [ ] **Step 7: Commit**

```bash
git add orchestrator/
git commit -m "refactor(health): add orchestrator/ package — schemas, session, llm, core"
```

---

### Task 4: Create `tools/appointment/` package

**Files:**
- Create: `tools/appointment/__init__.py`
- Create: `tools/appointment/booking.py`
- Create: `tools/appointment/database.py`

**Interfaces:**
- Consumes: `models/appointment.py`
- Produces: `handle_request(payload_dict: dict) -> dict`

- [ ] **Step 1: Create `tools/appointment/database.py`**

Exact copy of `appoint_tool/database.py` with two changes:
1. Remove `from config import DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD`
2. Add `import os` + `from dotenv import load_dotenv` + `load_dotenv()` at top
3. Replace all `DB_HOST`, `DB_PORT` etc. references with `os.getenv()` calls

```python
import os
import psycopg2
import psycopg2.extras
from contextlib import contextmanager
from dotenv import load_dotenv

load_dotenv()


@contextmanager
def get_connection():
    conn = psycopg2.connect(
        host=os.getenv("DB_HOST", "localhost"),
        port=int(os.getenv("DB_PORT", 5432)),
        dbname=os.getenv("DB_NAME", "postgres"),
        user=os.getenv("DB_USER", "postgres"),
        password=os.getenv("DB_PASSWORD", ""),
    )
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_hospital(conn, hospital_id):
    sql = """
        SELECT hospital_id, name, booking_mode, address, city
        FROM hospitals
        WHERE hospital_id = %s
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, (str(hospital_id),))
        return cur.fetchone()


def get_doctor(conn, doctor_id, hospital_id):
    sql = """
        SELECT doctor_id, hospital_id, name, is_active,
               avg_checkin_time, avg_consultation_minutes, fee
        FROM doctors
        WHERE doctor_id = %s
          AND hospital_id = %s
          AND is_active = true
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, (str(doctor_id), str(hospital_id)))
        return cur.fetchone()


def find_patient(conn, phone, hospital_id):
    sql = """
        SELECT patient_id, hospital_id, name, phone, age, location, diagnosis
        FROM patients
        WHERE phone = %s
          AND hospital_id = %s
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, (phone, str(hospital_id)))
        return cur.fetchone()


def insert_patient(conn, hospital_id, name, phone, age, location, diagnosis):
    sql = """
        INSERT INTO patients (hospital_id, name, phone, age, location, diagnosis)
        VALUES (%s, %s, %s, %s, %s, %s)
        RETURNING patient_id, hospital_id, name, phone, age, location, diagnosis
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, (str(hospital_id), name, phone, age, location, diagnosis))
        return cur.fetchone()


def get_or_create_today_session(conn, doctor_id, hospital_id, date=None):
    if date:
        insert_sql = """
            INSERT INTO doctor_sessions (doctor_id, hospital_id, date, status)
            VALUES (%s, %s, %s, 'OPEN')
            ON CONFLICT (hospital_id, doctor_id, date) DO NOTHING
            RETURNING session_id, doctor_id, hospital_id, date, status, started_at
        """
        select_sql = """
            SELECT session_id, doctor_id, hospital_id, date, status, started_at
            FROM doctor_sessions
            WHERE doctor_id = %s AND hospital_id = %s AND date = %s
        """
        insert_params = (str(doctor_id), str(hospital_id), date)
        select_params = (str(doctor_id), str(hospital_id), date)
    else:
        insert_sql = """
            INSERT INTO doctor_sessions (doctor_id, hospital_id, date, status)
            VALUES (%s, %s, CURRENT_DATE, 'OPEN')
            ON CONFLICT (hospital_id, doctor_id, date) DO NOTHING
            RETURNING session_id, doctor_id, hospital_id, date, status, started_at
        """
        select_sql = """
            SELECT session_id, doctor_id, hospital_id, date, status, started_at
            FROM doctor_sessions
            WHERE doctor_id = %s AND hospital_id = %s AND date = CURRENT_DATE
        """
        insert_params = (str(doctor_id), str(hospital_id))
        select_params = (str(doctor_id), str(hospital_id))

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(insert_sql, insert_params)
        row = cur.fetchone()
        if row is None:
            cur.execute(select_sql, select_params)
            row = cur.fetchone()
        return row


def insert_token(conn, session_id, patient_id, doctor_id, hospital_id, department):
    lock_sql = "SELECT session_id FROM doctor_sessions WHERE session_id = %s FOR UPDATE"
    next_number_sql = """
        SELECT COALESCE(MAX(token_number), 0) + 1 AS next_number
        FROM tokens WHERE session_id = %s
    """
    insert_sql = """
        INSERT INTO tokens (session_id, patient_id, doctor_id, hospital_id,
                            department, token_number, status)
        VALUES (%s, %s, %s, %s, %s, %s, 'WAITING')
        RETURNING token_id, session_id, patient_id, doctor_id, hospital_id,
                  department, token_number, status
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(lock_sql, (str(session_id),))
        cur.execute(next_number_sql, (str(session_id),))
        next_number = cur.fetchone()["next_number"]
        cur.execute(insert_sql, (
            str(session_id), str(patient_id), str(doctor_id),
            str(hospital_id), department, next_number,
        ))
        return cur.fetchone()


def count_patients_ahead(conn, session_id, token_number):
    sql = """
        SELECT COUNT(*) AS count FROM tokens
        WHERE session_id = %s AND token_number < %s AND status = 'WAITING'
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, (str(session_id), token_number))
        return cur.fetchone()["count"]


def find_active_token(conn, patient_id, doctor_id, date=None):
    if date:
        sql = """
            SELECT t.token_id, t.session_id, t.patient_id, t.doctor_id,
                   t.hospital_id, t.department, t.token_number, t.status
            FROM tokens t
            JOIN doctor_sessions ds ON t.session_id = ds.session_id
            WHERE t.patient_id = %s AND t.doctor_id = %s
              AND t.status = 'WAITING' AND ds.date = %s
        """
        params = (str(patient_id), str(doctor_id), date)
    else:
        sql = """
            SELECT t.token_id, t.session_id, t.patient_id, t.doctor_id,
                   t.hospital_id, t.department, t.token_number, t.status
            FROM tokens t
            JOIN doctor_sessions ds ON t.session_id = ds.session_id
            WHERE t.patient_id = %s AND t.doctor_id = %s
              AND t.status = 'WAITING' AND ds.date = CURRENT_DATE
        """
        params = (str(patient_id), str(doctor_id))
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, params)
        return cur.fetchone()


def cancel_token(conn, token_id):
    sql = "UPDATE tokens SET status = 'CANCELLED' WHERE token_id = %s"
    with conn.cursor() as cur:
        cur.execute(sql, (str(token_id),))
```

- [ ] **Step 2: Create `tools/appointment/booking.py`**

Exact copy of `appoint_tool/booking.py` with import changed from `import database as db` to `from tools.appointment import database as db` and `from models import ...` to `from models.appointment import ...`:

```python
from datetime import datetime, date, timedelta
import tools.appointment.database as db
from models.appointment import BookingConfirmation, CancellationResult, ErrorResult

SUPPORTED_BOOKING_MODE = "TOKEN"


def _check_supported_mode(hospital) -> ErrorResult | None:
    if hospital["booking_mode"] != SUPPORTED_BOOKING_MODE:
        return ErrorResult(
            status="ERROR",
            error_code="UNSUPPORTED_BOOKING_MODE",
            message=(
                f"This module only supports {SUPPORTED_BOOKING_MODE} mode; "
                f"hospital is configured for {hospital['booking_mode']}."
            ),
        )
    return None


def calculate_eta(session, doctor, patients_ahead):
    if session["started_at"] is not None:
        anchor_time = session["started_at"]
    else:
        session_date = session["date"] if session.get("date") else date.today()
        if isinstance(session_date, str):
            from datetime import date as date_cls
            session_date = date_cls.fromisoformat(session_date)
        anchor_time = datetime.combine(session_date, doctor["avg_checkin_time"])
    wait_minutes = patients_ahead * doctor["avg_consultation_minutes"]
    return anchor_time + timedelta(minutes=wait_minutes)


def book(conn, payload):
    hospital = db.get_hospital(conn, payload.hospital_id)
    if not hospital:
        return ErrorResult(status="ERROR", error_code="HOSPITAL_NOT_FOUND",
                           message=f"Hospital {payload.hospital_id} not found.")
    mode_error = _check_supported_mode(hospital)
    if mode_error:
        return mode_error

    doctor = db.get_doctor(conn, payload.doctor_id, payload.hospital_id)
    if not doctor:
        return ErrorResult(status="ERROR", error_code="DOCTOR_NOT_FOUND",
                           message=f"Doctor {payload.doctor_id} not found or inactive.")

    patient = db.find_patient(conn, payload.patient_phone, payload.hospital_id)
    if not patient:
        patient = db.insert_patient(
            conn,
            hospital_id=payload.hospital_id,
            name=payload.patient_name,
            phone=payload.patient_phone,
            age=payload.patient_age,
            location=payload.patient_location,
            diagnosis=payload.symptoms,
        )

    session = db.get_or_create_today_session(
        conn, payload.doctor_id, payload.hospital_id, payload.date
    )

    existing_token = db.find_active_token(
        conn, patient["patient_id"], payload.doctor_id, payload.date
    )
    if existing_token:
        return ErrorResult(status="ERROR", error_code="DUPLICATE_BOOKING",
                           message=f"You already have token #{existing_token['token_number']} with this doctor.")

    token = db.insert_token(
        conn,
        session_id=session["session_id"],
        patient_id=patient["patient_id"],
        doctor_id=payload.doctor_id,
        hospital_id=payload.hospital_id,
        department=payload.department,
    )

    patients_ahead = db.count_patients_ahead(conn, session["session_id"], token["token_number"])
    estimated_time = calculate_eta(session, doctor, patients_ahead)

    return BookingConfirmation(
        status="CONFIRMED",
        token_number=token["token_number"],
        doctor_name=doctor["name"],
        department=payload.department,
        hospital_name=hospital["name"],
        hospital_address=hospital.get("address"),
        fee=doctor.get("fee"),
        estimated_time=estimated_time,
    )


def cancel(conn, payload):
    hospital = db.get_hospital(conn, payload.hospital_id)
    if not hospital:
        return ErrorResult(status="ERROR", error_code="HOSPITAL_NOT_FOUND",
                           message=f"Hospital {payload.hospital_id} not found.")
    mode_error = _check_supported_mode(hospital)
    if mode_error:
        return mode_error

    patient = db.find_patient(conn, payload.patient_phone, payload.hospital_id)
    if not patient:
        return CancellationResult(status="PATIENT_NOT_FOUND",
                                  message="No patient record found for this number.")

    active = db.find_active_token(conn, patient["patient_id"], payload.doctor_id, payload.date)
    if not active:
        return CancellationResult(status="NO_ACTIVE_BOOKING",
                                  message="No active booking found to cancel.")

    db.cancel_token(conn, active["token_id"])
    return CancellationResult(status="CANCELLED",
                              message=f"Your token #{active['token_number']} has been cancelled.")
```

- [ ] **Step 3: Create `tools/appointment/__init__.py`**

```python
from models.appointment import IncomingPayload, BookingResponse, ErrorResult
from tools.appointment import booking, database


def handle_request(payload_dict: dict) -> dict:
    try:
        payload = IncomingPayload(**payload_dict)
    except Exception as e:
        return BookingResponse(
            action=payload_dict.get("action", "BOOK"),
            result=ErrorResult(
                status="ERROR",
                error_code="INVALID_PAYLOAD",
                message=f"Invalid payload: {e}",
            ),
        ).model_dump(mode="json")

    with database.get_connection() as conn:
        if payload.action == "BOOK":
            result = booking.book(conn, payload)
        else:
            result = booking.cancel(conn, payload)

    return BookingResponse(action=payload.action, result=result).model_dump(mode="json")
```

- [ ] **Step 4: Smoke-test appointment tool**

```bash
python -c "from tools.appointment import handle_request; print('OK')"
```

Expected: `OK`

- [ ] **Step 5: Commit**

```bash
git add tools/appointment/
git commit -m "refactor(health): add tools/appointment/ package — booking engine moved from appoint_tool/"
```

---

### Task 5: Delete old files and verify end-to-end

**Files:**
- Delete: `orchestrator.py`
- Delete: `appoint_tool/` (entire directory)

- [ ] **Step 1: Delete old files**

```bash
rm orchestrator.py
rm -rf appoint_tool/
```

- [ ] **Step 2: Verify run_manual_test.py imports resolve**

```bash
python -c "
import json
from orchestrator import (
    WhatsAppOrchestrator, WAMessage, GeminiLLMAdapter,
    PrintWANotifier, InMemoryRepository,
)
from orchestrator import SessionState
with open('config/doctors.json') as f:
    DOCTORS = json.load(f)['doctors']
repo = InMemoryRepository(doctors=DOCTORS)
print('role:', repo.get_role('+91-9940142234'))
print('OK')
"
```

Expected:
```
role: Role.DOCTOR
OK
```

- [ ] **Step 3: Full end-to-end import smoke-test**

```bash
python -c "
from tools.appointment import handle_request
from tools.kg_retriever import retrieve_context
from tools.query_data import run_query
from orchestrator import WhatsAppOrchestrator, InMemoryRepository
print('All imports OK')
"
```

Expected: `All imports OK`

- [ ] **Step 4: Commit**

```bash
git add -A
git commit -m "refactor(health): delete orchestrator.py and appoint_tool/ — fully migrated to modular layout"
```

---

### Task 6: Push to remote

- [ ] **Step 1: Push subtree to aismb remote**

```bash
git subtree push --prefix=ai_smb_health aismb main
```
