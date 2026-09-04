import uuid
import json
import time
import logging
from datetime import date, datetime, timedelta, timezone
from typing import List

from models.session import (
    Session, SessionState, Role, ChatRole, ChatTurn,
    AgentResponseType, GateStatus, GateResult, OrchestratorContext, WAMessage,
)
from orchestrator.schemas import (
    PATIENT_TOOLS, PATIENT_TOOLS_WARMUP, DOCTOR_TOOLS, ADMIN_TOOLS, ROLE_PERMISSIONS,
)
from orchestrator.utils import detect_booking_intent, is_affirmative, is_negative, looks_like_english
from orchestrator.formatters import format_booking_result, chunk_text
from orchestrator.llm import translate_static, translate_text, translate_labels, normalize_to_english
from orchestrator import gates
from prompts.system import PATIENT_SYSTEM_PROMPT, DOCTOR_SYSTEM_PROMPT, ADMIN_SYSTEM_PROMPT
from orchestrator.tracing import traced, add_metadata

logger = logging.getLogger(__name__)


import os

class WhatsAppOrchestrator:
    def __init__(
        self,
        llm,
        notifier,
        repository,
        fallback_text:     str = "I'm sorry, I couldn't process that. Please try again.",
        max_iterations:    int = int(os.getenv("MAX_ITERATIONS", "5")),
        max_history_turns: int = 10,
        session_idle_reset_hours: float = float(os.getenv("SESSION_IDLE_RESET_HOURS", "6")),
    ):
        self.llm               = llm
        self.notifier          = notifier
        self.repository        = repository
        self.fallback_text     = fallback_text
        self.max_iterations    = max_iterations
        self.max_history_turns = max_history_turns
        self.session_idle_reset = timedelta(hours=session_idle_reset_hours)

    @traced("WhatsAppOrchestrator.handle_message", run_type="chain", tags=["orchestrator", "agent"])
    def handle_message(self, wa_message: WAMessage):
        add_metadata(
            hospital_id=wa_message.hospital_id,
            from_number=wa_message.from_number,
        )
        try:
            self._handle_message_inner(wa_message)
        except Exception as exc:
            logger.error("Unhandled error in handle_message: %s", exc, exc_info=True)
            text = self.fallback_text
            try:
                text = translate_static(self.llm, self.fallback_text, wa_message.language_code)
            except Exception:
                pass
            try:
                self.notifier.send(wa_message.from_number, text)
            except Exception:
                pass

    def _handle_message_inner(self, wa_message: WAMessage):
        context = self._hydrate(wa_message)
        session = context.session
        add_metadata(role=session.role.value, state=session.state.value)

        if session.state == SessionState.AWAITING_CONFIRM:
            self._handle_awaiting_confirm(wa_message, context)
            return

        if session.turn_count == 0 and session.role == Role.PATIENT:
            session.booking_intent = detect_booking_intent(wa_message.text)
        session.turn_count += 1
        session.history.append(ChatTurn(role=ChatRole.USER, content=wa_message.text))

        system_prompt, tool_schemas = self._build_prompt_and_tools(wa_message, session)
        self._react_loop(context, system_prompt, tool_schemas)

    def _handle_awaiting_confirm(self, wa_message, context):
        session = context.session

        # ── Plan-gate (admin) ──────────────────────────────────────────────────
        if session.pending_plan is not None:
            if is_affirmative(wa_message.text):
                plan = session.pending_plan
                session.pending_plan = None
                session.state        = SessionState.IDLE
                self.repository.save_session(session)
                gates.execute_approved_plan(plan, context, self.notifier)
            elif is_negative(wa_message.text):
                session.pending_plan = None
                session.state        = SessionState.IDLE
                self.repository.save_session(session)
                self._responder("Plan cancelled.", context)
            else:
                self._responder("Please reply *YES* to execute the plan or *NO* to cancel.", context)
            return

        # ── Single-tool gate (booking / delay) ─────────────────────────────────
        pending = session.pending_tool
        is_cancel_action = (
            pending and
            pending.tool_name == "appointment" and
            pending.args.get("action") == "CANCEL"
        )
        text_is_affirmative = is_affirmative(wa_message.text) or (
            is_cancel_action and "cancel" in wa_message.text.strip().lower()
        )
        text_is_negative = is_negative(wa_message.text) and not (
            is_cancel_action and "cancel" in wa_message.text.strip().lower()
        )

        if text_is_affirmative:
            tool_call            = session.pending_tool
            session.pending_tool = None
            session.state        = SessionState.IDLE
            self.repository.save_session(session)
            self._log_tool(tool_call)
            try:
                result = self._execute_tool(tool_call, context)
            except Exception as tool_exc:
                logger.error("Tool %s failed at confirm-gate: %s", tool_call.tool_name, tool_exc, exc_info=True)
                text = translate_static(self.llm, "Sorry, something went wrong completing that. Please try again.", session.language_code)
                self._responder(text, context)
                return
            session.history.append(ChatTurn(
                role=ChatRole.TOOL_RESULT,
                content=json.dumps(result),
                tool_call=tool_call,
            ))
            if tool_call.tool_name == "report_delay":
                sent   = result.get("patients_notified", 0)
                failed = result.get("patients_failed", 0)
                delay  = tool_call.args.get("preview", {}).get("delay_minutes", "?")
                msg    = f"✅ Done. Session shifted by {delay} mins. {sent} patient(s) notified."
                if failed:
                    msg += f" ({failed} could not be reached — no phone on record.)"
                self._responder(msg, context)
                return
            if tool_call.tool_name == "appointment":
                formatted = self._format_appointment_response(result, tool_call.args, session)
                if formatted:
                    self._responder(formatted, context)
                    return
            # Tool result in history — let LLM generate the response
            system_prompt, tool_schemas = self._build_prompt_and_tools(wa_message, session)
            self._react_loop(context, system_prompt, tool_schemas)

        elif text_is_negative:
            self._resolve_pending_tool_as_not_executed(session, "Cancelled — the patient declined.")
            session.state = SessionState.IDLE
            session.history.append(ChatTurn(role=ChatRole.USER, content=wa_message.text))
            self._responder(
                translate_static(self.llm, "Understood. Your request has been cancelled.", session.language_code),
                context,
            )

        else:
            # Not a plain yes/no — treat it as new information (e.g. a
            # correction like "tomorrow instead") and let the LLM, which
            # still has the pending request in its own history, reconsider
            # rather than mechanically replaying a possibly-wrong tool call.
            self._resolve_pending_tool_as_not_executed(
                session, "Not executed — the patient replied with something other than a plain yes/no."
            )
            session.state = SessionState.IDLE
            session.history.append(ChatTurn(role=ChatRole.USER, content=wa_message.text))
            system_prompt, tool_schemas = self._build_prompt_and_tools(wa_message, session)
            self._react_loop(context, system_prompt, tool_schemas)

    def _build_prompt_and_tools(self, wa_message, session):
        if session.role == Role.ADMIN:
            self.max_iterations = 10
            tool_schemas  = ADMIN_TOOLS
            system_prompt = ADMIN_SYSTEM_PROMPT + f"\n\nToday's date is {date.today().isoformat()}."
            cfg = self.repository.get_admin_config(wa_message.from_number)
            known_name = cfg["name"] if cfg else None

        elif session.role == Role.DOCTOR:
            tool_schemas  = DOCTOR_TOOLS
            system_prompt = DOCTOR_SYSTEM_PROMPT
            cfg = self.repository.get_doctor_config(wa_message.from_number)
            known_name = cfg["name"] if cfg else None

        else:
            use_full_tools = session.booking_intent or session.turn_count >= 3
            tool_schemas   = PATIENT_TOOLS if use_full_tools else PATIENT_TOOLS_WARMUP
            if not session.memory_loaded:
                self._preload_memory(session, wa_message)
            system_prompt = PATIENT_SYSTEM_PROMPT + f"\n\nToday's date is {date.today().isoformat()}."
            if session.memory_context:
                system_prompt += f"\n\nPATIENT CONTEXT (from DB):\n{session.memory_context}"
            known_name = None

        session_info = f"\n\nSESSION INFO:\n- Turn: {session.turn_count}"
        if known_name:
            session_info += f"\n- Name: {known_name}"
        return system_prompt + session_info, tool_schemas

    def _react_loop(self, context, system_prompt, tool_schemas):
        session    = context.session
        final_text = self.fallback_text
        # Only our own hardcoded fallback strings need translating at the end —
        # LLM-generated text already matches the patient's language, and the
        # booking card is already localized via translate_labels() below.
        needs_translation = True
        kg_empty_streak = 0

        for _ in range(self.max_iterations):
            print("  [Thinking...]", flush=True)
            agent_response = self._llm_call_with_retry(session.history, tool_schemas, system_prompt)
            if agent_response is None:
                break

            if agent_response.type == AgentResponseType.TEXT:
                final_text = agent_response.text
                needs_translation = False
                # The system prompt asks the model to always reply in the
                # patient's language, but that's a soft instruction it can
                # (and occasionally does) ignore — catch a plain-English
                # reply in a non-English session and translate it ourselves
                # rather than letting it slip through untranslated.
                lang_prefix = (session.language_code or "en").split("-")[0].lower()
                if lang_prefix != "en" and looks_like_english(final_text):
                    final_text = translate_text(self.llm, final_text, session.language_code)
                break

            session.history.append(ChatTurn(
                role=ChatRole.ASSISTANT,
                content="",
                tool_call=agent_response.tool_call,
            ))

            if agent_response.tool_call.tool_name == "execute_plan":
                gates.interrupt_plan(agent_response.tool_call.args, context, self.repository, self.notifier)
                return

            if agent_response.tool_call.tool_name == "report_delay":
                doc_cfg = self.repository.get_doctor_config(context.wa_message.from_number)
                if not doc_cfg:
                    self._responder("Could not find your doctor profile. Please contact admin.", context)
                    return
                gates.interrupt_delay(agent_response.tool_call.args, context, doc_cfg, self.repository, self.notifier)
                return

            if agent_response.tool_call.tool_name == "appointment":
                self._normalize_appointment_args(agent_response.tool_call)

            gate_result = self._gate(agent_response.tool_call, context)
            if gate_result.status == GateStatus.FORBIDDEN:
                text = translate_static(self.llm, "Sorry, you don't have permission to perform this action.", session.language_code)
                self._responder(text, context)
                return
            if gate_result.status == GateStatus.CONFIRM_REQUIRED:
                gates.interrupt_tool(agent_response.tool_call, context, self.repository, self.notifier, self.llm)
                return

            self._log_tool(agent_response.tool_call)
            print("  [Executing tool, waiting for result...]", flush=True)
            try:
                result = self._execute_tool(agent_response.tool_call, context)
            except Exception as tool_exc:
                logger.error("Tool %s failed: %s", agent_response.tool_call.tool_name, tool_exc, exc_info=True)
                result = {"error": str(tool_exc)}
            session.history.append(ChatTurn(
                role=ChatRole.TOOL_RESULT,
                content=json.dumps(result),
                tool_call=agent_response.tool_call,
            ))

            if agent_response.tool_call.tool_name == "kg_retriever":
                if not result.get("doctors"):
                    kg_empty_streak += 1
                    if kg_empty_streak >= 2:
                        final_text = (
                            "I wasn't able to find a matching doctor at this hospital. "
                            "Could you try a different specialty or doctor name?"
                        )
                        break
                else:
                    kg_empty_streak = 0

            if agent_response.tool_call.tool_name == "appointment":
                formatted = self._format_appointment_response(result, agent_response.tool_call.args, session)
                if formatted:
                    final_text = formatted
                    needs_translation = False
                    break

        if needs_translation:
            final_text = translate_static(self.llm, final_text, session.language_code)
        self._responder(final_text, context)

    def _hydrate(self, wa_message: WAMessage) -> OrchestratorContext:
        session = self.repository.get_session(wa_message.hospital_id, wa_message.from_number)
        now = datetime.now(timezone.utc)
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
        elif now - session.last_active_at > self.session_idle_reset:
            # The session store has no concept of "today" vs. "last week" —
            # a phone number that messaged once, ever, stays "mid-conversation"
            # forever (turn_count never resets), so the one-time greeting
            # never fires again and the bot jumps straight into follow-up
            # mode even though the patient is starting a fresh chat. Treat a
            # long enough gap as the start of a new conversation.
            session.state          = SessionState.IDLE
            session.history        = []
            session.pending_tool   = None
            session.pending_plan   = None
            session.turn_count     = 0
            session.booking_intent = False
            session.memory_loaded  = False
            session.memory_context = ""
        session.last_active_at = now
        if wa_message.language_code:
            session.language_code = wa_message.language_code
        self.repository.save_session(session)
        return OrchestratorContext(wa_message, session)

    def _preload_memory(self, session, wa_message: WAMessage):
        try:
            from tools.memory_tool import fetch_patient_context
            _, context_str = fetch_patient_context(wa_message.from_number, wa_message.hospital_id)
            session.memory_context = context_str
            session.memory_loaded  = True
        except Exception as exc:
            logger.warning("memory preload failed: %s", exc)
            session.memory_loaded = True

    def _resolve_pending_tool_as_not_executed(self, session, reason: str):
        """Close out session.pending_tool with an explicit NOT_EXECUTED result
        instead of just dropping it.

        The tool call was already written into session.history as an
        unpaired assistant tool-call turn back when _react_loop first
        proposed it (before the confirm-gate deferred it) — see
        _react_loop's history.append() right before the CONFIRM_REQUIRED
        check. If we clear pending_tool without ever resolving that turn,
        the next LLM call sees its own "I'm calling book()" turn with no
        result and, left to guess, tends to assume it succeeded and
        narrates a fabricated confirmation instead of a real one — nothing
        gets written to the database, but the patient is told it was
        booked. Appending this turn keeps the tool-call/tool-result pairing
        intact and tells the model explicitly that nothing happened.

        Only safe for "appointment": interrupt_tool() (gates.py) hands
        pending_tool the *same* ToolCall object the LLM proposed, so its
        tool_use_id matches the dangling history turn. report_delay's
        pending_tool (built fresh in interrupt_delay() with a new random
        tool_use_id) does NOT match its original history turn, so
        resolving it here would append an orphaned tool-result the API
        would reject — for that case we fall back to the old plain clear."""
        tool_call = session.pending_tool
        session.pending_tool = None
        if tool_call and tool_call.tool_name == "appointment":
            session.history.append(ChatTurn(
                role=ChatRole.TOOL_RESULT,
                content=json.dumps({"status": "NOT_EXECUTED", "reason": reason}),
                tool_call=tool_call,
            ))

    def _normalize_appointment_args(self, tool_call):
        """Store/lookup patient data in English regardless of what language it
        was spoken in — keeps hospital records consistent and lets
        family-member lookups (matched by name) work across conversations
        in different languages."""
        for field in ("patient_name", "patient_location", "symptoms"):
            if tool_call.args.get(field):
                tool_call.args[field] = normalize_to_english(self.llm, tool_call.args[field])

    def _format_appointment_response(self, result: dict, tool_args: dict, session) -> str | None:
        """Render a confirmation card for a successful booking or cancellation,
        translated into the session's language. Returns None for any other
        outcome (errors, etc.) so the caller lets the LLM narrate it instead.

        Both BOOK and CANCEL go through here so neither one falls back to the
        LLM freely summarizing raw tool JSON itself — that fallback isn't
        guaranteed to include every field (hospital, fee, reporting time) or
        to stay in the patient's language."""
        if result.get("action") in ("BOOK", "CANCEL"):
            session.memory_loaded = False

        labels    = translate_labels(self.llm, session.language_code)
        formatted = format_booking_result(result, tool_args, labels)
        if formatted:
            return formatted

        booking = result.get("result", {})
        if result.get("action") == "CANCEL" and booking.get("status") == "CANCELLED":
            return translate_text(self.llm, booking["message"], session.language_code)

        return None

    @traced("orchestrator._gate", run_type="chain", tags=["gate"])
    def _gate(self, tool_call, context: OrchestratorContext) -> GateResult:
        allowed = ROLE_PERMISSIONS.get(tool_call.tool_name, {Role.PATIENT, Role.DOCTOR})
        if context.session.role not in allowed:
            return GateResult(GateStatus.FORBIDDEN)
        if tool_call.tool_name == "appointment" and context.session.state != SessionState.AWAITING_CONFIRM:
            return GateResult(GateStatus.CONFIRM_REQUIRED)
        return GateResult(GateStatus.OK)

    @traced("orchestrator._execute_tool", run_type="tool", tags=["tool"])
    def _execute_tool(self, tool_call, context: OrchestratorContext) -> dict:
        name = tool_call.tool_name
        if name == "appointment":
            from tools.appointment import handle_request
            return handle_request({
                **tool_call.args,
                "hospital_id":     context.wa_message.hospital_id,
                "requester_phone": context.wa_message.from_number,
                "patient_phone":   tool_call.args.get("patient_phone") or context.wa_message.from_number,
            })
        if name == "list_appointments":
            from tools.appointment import list_appointments
            return list_appointments(
                hospital_id=context.wa_message.hospital_id,
                requester_phone=context.wa_message.from_number,
                patient_name=tool_call.args.get("patient_name"),
            )
        if name == "kg_retriever":
            from tools.kg_retriever import retrieve_context
            return retrieve_context(**tool_call.args)
        if name == "memory_tool":
            from tools.memory_tool import run as memory_run
            return memory_run(phone=context.wa_message.from_number, hospital_id=context.wa_message.hospital_id)
        if name == "query_data":
            if context.session.role == Role.ADMIN:
                from tools.query_data import run_admin_query
                return run_admin_query(question=tool_call.args["question"], hospital_id=context.wa_message.hospital_id)
            from tools.query_data import run_query
            return run_query(question=tool_call.args["question"], doctor_phone=context.wa_message.from_number, repository=self.repository)
        if name == "get_session_impact":
            from tools.session_impact import get_session_impact
            return get_session_impact(doctor_id=tool_call.args["doctor_id"], date=tool_call.args["date"], hospital_id=context.wa_message.hospital_id)
        if name == "find_available_doctors":
            from tools.session_impact import find_available_doctors
            return find_available_doctors(specialization=tool_call.args["specialization"], date=tool_call.args["date"], hospital_id=context.wa_message.hospital_id)
        if name == "report_delay":
            from tools.delay_report import execute_delay_report
            return execute_delay_report(tool_call.args["preview"], self.notifier)
        return {"error": "unknown tool"}

    def _llm_call_with_retry(self, history, tool_schemas, system_prompt, max_retries: int = 3):
        delay = 2
        for attempt in range(1, max_retries + 1):
            try:
                return self.llm.run_agent(history, tool_schemas, system_prompt)
            except Exception as exc:
                msg = str(exc)
                is_transient = "503" in msg or "429" in msg or "UNAVAILABLE" in msg or "rate" in msg.lower()
                if is_transient and attempt < max_retries:
                    print(f"  [LLM transient error, retrying in {delay}s... ({attempt}/{max_retries})]", flush=True)
                    time.sleep(delay)
                    delay *= 2
                else:
                    logger.error("LLM error: %s", exc)
                    return None

    @traced("orchestrator._responder", run_type="chain", tags=["responder"])
    def _responder(self, text: str, context: OrchestratorContext):
        for chunk in chunk_text(text, max_chars=1000):
            try:
                self.notifier.send(context.wa_message.from_number, chunk)
            except Exception:
                pass
        context.session.history.append(ChatTurn(role=ChatRole.ASSISTANT, content=text))
        context.session.history = context.session.history[-self.max_history_turns:]
        self.repository.save_session(context.session)

    def _log_tool(self, tool_call):
        print(f"\n  [TOOL] {tool_call.tool_name} -> {json.dumps(tool_call.args, indent=2)}\n")
