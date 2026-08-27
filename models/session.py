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
    tool_use_id:       str         = field(default_factory=lambda: f"toolu_{uuid.uuid4().hex[:12]}")
    thought_signature: str | None  = None

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
    session_id:      str
    hospital_id:     str
    from_number:     str
    state:           SessionState
    history:         List[ChatTurn]
    pending_tool:    Optional[ToolCall]
    role:            Role = Role.PATIENT
    turn_count:      int  = 0
    booking_intent:  bool = False
    memory_loaded:   bool = False
    memory_context:  str  = ""

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
