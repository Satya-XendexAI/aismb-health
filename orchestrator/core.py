import uuid
import json
import time
import logging
from datetime import date
from typing import List

from models.session import (
    Session, SessionState, Role, ChatRole, ChatTurn,
    AgentResponseType, GateStatus, GateResult, OrchestratorContext, WAMessage,
    PlanAction,
)
from orchestrator.schemas import PATIENT_TOOLS, PATIENT_TOOLS_WARMUP, DOCTOR_TOOLS, ADMIN_TOOLS, ROLE_PERMISSIONS
from prompts.system import PATIENT_SYSTEM_PROMPT, DOCTOR_SYSTEM_PROMPT, ADMIN_SYSTEM_PROMPT

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
        try:
            self._handle_message_inner(wa_message)
        except Exception as exc:
            logger.error("Unhandled error in handle_message: %s", exc, exc_info=True)
            try:
                self.notifier.send(wa_message.from_number, self.fallback_text)
            except Exception:
                pass

    def _handle_message_inner(self, wa_message: WAMessage):
        context = self._hydrate(wa_message)
        session = context.session

        if session.state == SessionState.AWAITING_CONFIRM:
            # ── Plan-gate path (admin) ──────────────────────────────────────────
            if session.pending_plan is not None:
                if _is_affirmative(wa_message.text):
                    plan = session.pending_plan
                    session.pending_plan = None
                    session.state        = SessionState.IDLE
                    self.repository.save_session(session)
                    self._execute_plan(plan, context)
                elif _is_negative(wa_message.text):
                    session.pending_plan = None
                    session.state        = SessionState.IDLE
                    self.repository.save_session(session)
                    self._responder("Plan cancelled.", context)
                else:
                    self._responder("Please reply *YES* to execute the plan or *NO* to cancel.", context)
                return

            # ── Single-tool confirm path (patient booking) ─────────────────────
            pending = session.pending_tool
            is_cancel_action = (
                pending and
                pending.tool_name == "appointment" and
                pending.args.get("action") == "CANCEL"
            )
            # When confirming a CANCEL appointment, "cancel" means YES — not abort
            text_is_affirmative = _is_affirmative(wa_message.text) or (
                is_cancel_action and "cancel" in wa_message.text.strip().lower()
            )
            text_is_negative = _is_negative(wa_message.text) and not (
                is_cancel_action and "cancel" in wa_message.text.strip().lower()
            )

            if text_is_affirmative:
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
                    if result.get("action") in ("BOOK", "CANCEL"):
                        session.memory_loaded = False
                    formatted = self._format_booking_result(result, tool_call.args)
                    if formatted:
                        self._responder(formatted, context)
                        return

            elif text_is_negative:
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

        if session.role == Role.ADMIN:
            tool_schemas    = ADMIN_TOOLS
            system_prompt   = ADMIN_SYSTEM_PROMPT + f"\n\nToday's date is {date.today().isoformat()}."
            self.max_iterations = 10   # admin flow needs kg_retriever + impact + available + plan
        elif session.role == Role.DOCTOR:
            tool_schemas  = DOCTOR_TOOLS
            system_prompt = DOCTOR_SYSTEM_PROMPT
        else:
            use_full_tools = session.booking_intent or session.turn_count >= 3
            tool_schemas   = PATIENT_TOOLS if use_full_tools else PATIENT_TOOLS_WARMUP
            if not session.memory_loaded:
                self._preload_memory(session, context.wa_message)
            system_prompt = PATIENT_SYSTEM_PROMPT + f"\n\nToday's date is {date.today().isoformat()}."
            if session.memory_context:
                system_prompt += f"\n\nPATIENT CONTEXT (from DB):\n{session.memory_context}"
        final_text = self.fallback_text

        kg_empty_streak = 0

        for _ in range(self.max_iterations):
            print("  [Thinking...]", flush=True)
            agent_response = self._llm_call_with_retry(session.history, tool_schemas, system_prompt)
            if agent_response is None:
                break

            if agent_response.type == AgentResponseType.TEXT:
                final_text = agent_response.text
                break

            session.history.append(ChatTurn(
                role=ChatRole.ASSISTANT,
                content="",
                tool_call=agent_response.tool_call,
            ))

            # Plan gate: intercept execute_plan before the normal gate
            if agent_response.tool_call.tool_name == "execute_plan":
                self._interrupt_plan(agent_response.tool_call.args, context)
                return

            # Delay gate: intercept report_delay, build preview, ask doctor to confirm
            if agent_response.tool_call.tool_name == "report_delay":
                self._interrupt_delay(agent_response.tool_call.args, context)
                return

            gate_result = self._gate(agent_response.tool_call, context)

            if gate_result.status == GateStatus.FORBIDDEN:
                self._responder("Sorry, you don't have permission to perform this action.", context)
                return

            if gate_result.status == GateStatus.CONFIRM_REQUIRED:
                self._interrupt(agent_response.tool_call, context)
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
                if result.get("action") in ("BOOK", "CANCEL"):
                    session.memory_loaded = False
                formatted = self._format_booking_result(result, agent_response.tool_call.args)
                if formatted:
                    final_text = formatted
                    break

        self._responder(final_text, context)

    def _llm_call_with_retry(self, history, tool_schemas, system_prompt, max_retries: int = 3):
        """Call the LLM, retrying on transient 503/429 errors with exponential backoff."""
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
                    print(f"  [LLM error: {exc}]")
                    return None

    def _preload_memory(self, session, wa_message: WAMessage):
        try:
            from tools.memory_tool import fetch_patient_context
            _, context_str = fetch_patient_context(wa_message.from_number, wa_message.hospital_id)
            session.memory_context = context_str
            session.memory_loaded  = True
        except Exception as exc:
            logger.warning("memory preload failed: %s", exc)
            session.memory_loaded = True   # avoid retrying on every turn if DB is down

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
        elif tool_call.tool_name == "list_appointments":
            from tools.appointment import list_appointments
            return list_appointments(
                hospital_id=context.wa_message.hospital_id,
                requester_phone=context.wa_message.from_number,
                patient_name=tool_call.args.get("patient_name"),
            )
        elif tool_call.tool_name == "kg_retriever":
            from tools.kg_retriever import retrieve_context
            return retrieve_context(**tool_call.args)
        elif tool_call.tool_name == "memory_tool":
            from tools.memory_tool import run as memory_run
            return memory_run(
                phone=context.wa_message.from_number,
                hospital_id=context.wa_message.hospital_id,
            )
        elif tool_call.tool_name == "query_data":
            if context.session.role == Role.ADMIN:
                from tools.query_data import run_admin_query
                return run_admin_query(
                    question=tool_call.args["question"],
                    hospital_id=context.wa_message.hospital_id,
                )
            from tools.query_data import run_query
            return run_query(
                question=tool_call.args["question"],
                doctor_phone=context.wa_message.from_number,
                repository=self.repository,
            )
        elif tool_call.tool_name == "get_session_impact":
            from tools.session_impact import get_session_impact
            return get_session_impact(
                doctor_id=tool_call.args["doctor_id"],
                date=tool_call.args["date"],
                hospital_id=context.wa_message.hospital_id,
            )
        elif tool_call.tool_name == "find_available_doctors":
            from tools.session_impact import find_available_doctors
            return find_available_doctors(
                specialization=tool_call.args["specialization"],
                date=tool_call.args["date"],
                hospital_id=context.wa_message.hospital_id,
            )
        elif tool_call.tool_name == "report_delay":
            from tools.delay_report import execute_delay_report
            return execute_delay_report(tool_call.args["preview"], self.notifier)
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

    def _interrupt_plan(self, plan_args: dict, context: OrchestratorContext):
        plan = self._build_pending_plan(plan_args)
        context.session.pending_plan = plan
        context.session.pending_tool = None
        context.session.state        = SessionState.AWAITING_CONFIRM
        self.repository.save_session(context.session)
        summary = self._format_plan_summary(plan, plan_args.get("summary", ""))
        try:
            self.notifier.send(context.wa_message.from_number, summary)
        except Exception:
            pass

    @staticmethod
    def _build_pending_plan(plan_args: dict) -> list:
        actions = []
        for a in plan_args.get("actions", []):
            actions.append(PlanAction(
                action_type=a["action_type"],
                token_id=a["token_id"],
                patient_name=a["patient_name"],
                patient_phone=a["patient_phone"],
                doctor_name=a["doctor_name"],
                notification_message=a["notification_message"],
                new_doctor_id=a.get("new_doctor_id"),
                new_doctor_name=a.get("new_doctor_name"),
                new_session_id=a.get("new_session_id"),
                session_id=a.get("session_id"),
                delay_minutes=a.get("delay_minutes"),
            ))
        return actions

    @staticmethod
    def _format_plan_summary(plan: list, summary_line: str) -> str:
        from collections import defaultdict
        sections = []

        sections.append(f"📋 *ACTION PLAN*\n{summary_line}")

        # REASSIGN: group by target doctor
        reassign: dict[str, list] = defaultdict(list)
        for a in plan:
            if a.action_type == "REASSIGN":
                target = a.new_doctor_name or "another doctor"
                reassign[target].append(a.patient_name)
        for doctor, patients in reassign.items():
            block = [f"🔄 *Reassigned → Dr. {doctor}* ({len(patients)} patients)"]
            for i, name in enumerate(patients, 1):
                block.append(f"{i}. {name}")
            sections.append("\n".join(block))

        # SHIFT: group by delay_minutes
        shifts: dict[int, list] = defaultdict(list)
        for a in plan:
            if a.action_type == "SHIFT":
                shifts[a.delay_minutes or 0].append(a.patient_name)
        for delay, patients in sorted(shifts.items()):
            label = f"{delay} min" if delay else "unknown duration"
            block = [f"⏰ *Shifted +{label}* ({len(patients)} patients)"]
            for i, name in enumerate(patients, 1):
                block.append(f"{i}. {name}")
            sections.append("\n".join(block))

        # RETAIN
        retains = [a.patient_name for a in plan if a.action_type == "RETAIN"]
        if retains:
            block = [f"✅ *No change* ({len(retains)} patients — on schedule)"]
            for i, name in enumerate(retains, 1):
                block.append(f"{i}. {name}")
            sections.append("\n".join(block))

        sections.append("Reply *YES* to execute or *NO* to cancel.")
        return "\n\n".join(sections)

    def _interrupt_delay(self, args: dict, context: OrchestratorContext):
        from tools.delay_report import get_delay_preview
        delay_minutes = int(args.get("delay_minutes", 0))
        doc_config = self.repository.get_doctor_config(context.wa_message.from_number)
        if not doc_config:
            self._responder("Could not find your doctor profile. Please contact admin.", context)
            return
        preview = get_delay_preview(
            delay_minutes=delay_minutes,
            doctor_id=doc_config["doctor_id"],
            hospital_id=context.wa_message.hospital_id,
        )
        if "error" in preview:
            self._responder(preview["error"], context)
            return
        from models.session import ToolCall as TC
        context.session.pending_tool = TC(
            tool_name="report_delay",
            args={"preview": preview},
            tool_use_id=str(uuid.uuid4()),
        )
        context.session.state = SessionState.AWAITING_CONFIRM
        self.repository.save_session(context.session)
        try:
            self.notifier.send(context.wa_message.from_number, self._format_delay_preview(preview))
        except Exception:
            pass

    @staticmethod
    def _format_delay_preview(preview: dict) -> str:
        delay   = preview["delay_minutes"]
        doctor  = preview["doctor_name"]
        patients = preview["patients"]
        sections = [
            f"📋 *Delay Notification Preview*\nDr. {doctor} — {delay}-min delay · {len(patients)} patients waiting"
        ]
        lines = [f"{p['token_number']}. {p['patient_name']} — Est. {p['estimated_time']}"
                 for p in patients]
        sections.append("\n".join(lines))
        sections.append("Reply *YES* to send notifications or *NO* to cancel.")
        return "\n\n".join(sections)

    def _execute_plan(self, plan: list, context: OrchestratorContext):
        from tools.bulk_ops import bulk_reschedule
        from workers.notification_worker import notify_patients_bulk

        result = bulk_reschedule(plan, hospital_id=context.wa_message.hospital_id)

        if result.rolled_back:
            self._responder(
                "Execution failed — no changes were made. Please try again.",
                context,
            )
            return

        # Patch REASSIGN notification messages with the real token numbers
        for action in result.succeeded:
            if action.action_type == "REASSIGN" and action.new_token_number is not None:
                action.notification_message = action.notification_message.replace(
                    "#?", f"#{action.new_token_number}"
                )

        job_id = notify_patients_bulk(result.succeeded, self.notifier)
        total  = len(plan)
        done   = len(result.succeeded)
        self._responder(
            f"✅ Executed. {done}/{total} DB updates done. "
            f"Notifying patients in background... (Job: {job_id})",
            context,
        )

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
            lines.append("🏥 *Hospital:* Hospital name")
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
