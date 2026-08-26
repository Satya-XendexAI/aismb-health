import uuid
import json
import logging
from datetime import date
from typing import List

from models.session import (
    Session, SessionState, Role, ChatRole, ChatTurn,
    AgentResponseType, GateStatus, GateResult, OrchestratorContext, WAMessage,
)
from orchestrator.schemas import PATIENT_TOOLS, PATIENT_TOOLS_WARMUP, DOCTOR_TOOLS, ROLE_PERMISSIONS
from prompts.system import PATIENT_SYSTEM_PROMPT, DOCTOR_SYSTEM_PROMPT

logger = logging.getLogger(__name__)

_BOOKING_KEYWORDS = {"book", "appointment", "cancel", "token", "schedule", "register", "slot"}
_AFFIRMATIVE      = {"yes", "y", "ok", "okay", "sure", "book", "confirm", "go ahead", "proceed", "yeah", "yep", "do it"}
_NEGATIVE         = {"no", "n", "cancel", "stop", "nope", "never mind", "nevermind", "nah", "don't"}

def _detect_booking_intent(text: str) -> bool:
    lowered = text.lower()
    return any(kw in lowered for kw in _BOOKING_KEYWORDS)

def _is_affirmative(text: str) -> bool:
    lowered = text.strip().lower()
    return lowered in _AFFIRMATIVE or any(kw in lowered for kw in {"yes", "book", "confirm", "go ahead", "proceed", "sure", "ok"})

def _is_negative(text: str) -> bool:
    lowered = text.strip().lower()
    return lowered in _NEGATIVE or any(kw in lowered for kw in {"no", "cancel", "stop", "don't"})


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
            if _is_affirmative(wa_message.text):
                tool_call            = session.pending_tool
                session.pending_tool = None
                session.state        = SessionState.IDLE
                self.repository.save_session(session)
                self._log_tool(tool_call)
                result = self._execute_tool(tool_call, context)
                session.history.append(ChatTurn(
                    role=ChatRole.TOOL_RESULT,
                    content=json.dumps(result),
                    tool_call=tool_call,              # carry tool_call so llm.py can always resolve name/id
                ))
                if tool_call.tool_name == "appointment":
                    formatted = self._format_booking_result(result, tool_call.args)
                    if formatted:
                        self._responder(formatted, context)
                        return

            elif _is_negative(wa_message.text):
                session.pending_tool = None
                session.state        = SessionState.IDLE
                session.history.append(ChatTurn(role=ChatRole.USER, content=wa_message.text))
                self._responder("Understood. Your request has been cancelled.", context)
                return

            else:
                # Stay in AWAITING_CONFIRM — don't reset, just re-prompt
                self._responder("Please reply *YES* to confirm or *NO* to cancel.", context)
                return

        else:
            if session.turn_count == 0 and session.role == Role.PATIENT:
                session.booking_intent = _detect_booking_intent(wa_message.text)
            session.turn_count += 1
            session.history.append(ChatTurn(role=ChatRole.USER, content=wa_message.text))

        if session.role == Role.DOCTOR:
            tool_schemas  = DOCTOR_TOOLS
            system_prompt = DOCTOR_SYSTEM_PROMPT
        else:
            use_full_tools = session.booking_intent or session.turn_count >= 3
            tool_schemas   = PATIENT_TOOLS if use_full_tools else PATIENT_TOOLS_WARMUP
            system_prompt  = PATIENT_SYSTEM_PROMPT + f"\n\nToday's date is {date.today().isoformat()}."
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
            if agent_response.tool_call.tool_name == "appointment":
                formatted = self._format_booking_result(result, agent_response.tool_call.args)
                if formatted:
                    final_text = formatted
                    break

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
                "hospital_id":      context.wa_message.hospital_id,
                "requester_phone":  context.wa_message.from_number,
                "patient_phone":    tool_call.args.get("patient_phone")
                                    or context.wa_message.from_number,
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
        message = f"Please confirm: {desc}"
        context.session.pending_tool = tool_call
        context.session.state        = SessionState.AWAITING_CONFIRM
        self.repository.save_session(context.session)
        try:
            self.notifier.send(context.wa_message.from_number, message)
        except Exception:
            pass

    @staticmethod
    def _doctor_display_name(args: dict) -> str:
        if args.get("doctor_name"):
            return args["doctor_name"]
        # Derive readable name from doctor_id e.g. "dr-ajit-yadav--chn" → "Dr. Ajit Yadav"
        raw = args.get("doctor_id", "the doctor")
        import re as _re
        raw = _re.sub(r"--[a-z]+$", "", raw)   # strip --chn suffix
        raw = _re.sub(r"-[a-z]{2,4}$", "", raw) # strip -chn suffix
        return raw.replace("-", " ").strip().title()

    def _describe_tool(self, tool_call) -> str:
        action   = tool_call.args.get("action", "BOOK").upper()
        doctor   = self._doctor_display_name(tool_call.args)
        dept     = tool_call.args.get("department", "")
        name     = tool_call.args.get("patient_name", "")
        relation = tool_call.args.get("relation_to_requester", "self")
        date     = tool_call.args.get("date", "today")
        desc     = f"{action} appointment with {doctor}"
        if dept:
            desc += f" ({dept})"
        if name:
            suffix = f" ({relation})" if relation and relation != "self" else ""
            desc += f" for {name}{suffix}"
        desc += f" on {date}"
        return desc

    @staticmethod
    def _format_booking_result(result: dict, tool_args: dict) -> str | None:
        if result.get("action") != "BOOK":
            return None
        booking = result.get("result", {})
        if booking.get("status") != "CONFIRMED":
            return None

        token    = booking.get("token_number", "?")
        doctor   = booking.get("doctor_name", tool_args.get("doctor_name", "the doctor"))
        dept     = booking.get("department", "")
        hospital = booking.get("hospital_name", "")
        address  = booking.get("hospital_address", "")
        fee      = booking.get("fee")
        eta      = booking.get("estimated_time", "")
        date_str = tool_args.get("date", "today")

        lines = ["✅ *Appointment Confirmed*\n"]
        lines.append(f"🎫 *Token:* #{token}")
        lines.append(f"👨‍⚕️ *Doctor:* {doctor}")
        if dept:
            lines.append(f"🏛 *Department:* {dept}")
        if hospital:
            lines.append(f"🏥 *Hospital:* {hospital}")
        if address:
            lines.append(f"📍 *Address:* {address}")
        lines.append(f"📅 *Date:* {date_str}")
        if eta and "T" in str(eta):
            lines.append(f"⏰ *Estimated Time:* {str(eta).split('T')[1][:5]}")
        if fee:
            lines.append(f"💰 *Fee:* ₹{int(fee)}")

        return "\n".join(lines)

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
