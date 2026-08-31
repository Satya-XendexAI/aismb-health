import uuid
import logging

from models.session import SessionState, PlanAction, ToolCall
from orchestrator.formatters import format_plan_summary, format_delay_preview, describe_tool

logger = logging.getLogger(__name__)


def interrupt_tool(tool_call, context, repository, notifier):
    """Enter AWAITING_CONFIRM for a single tool (e.g. appointment booking)."""
    context.session.pending_tool = tool_call
    context.session.state        = SessionState.AWAITING_CONFIRM
    repository.save_session(context.session)
    try:
        notifier.send(context.wa_message.from_number, f"Please confirm: {describe_tool(tool_call)}")
    except Exception:
        pass


def interrupt_plan(plan_args: dict, context, repository, notifier):
    """Enter AWAITING_CONFIRM for an admin action plan."""
    plan = build_pending_plan(plan_args)
    context.session.pending_plan = plan
    context.session.pending_tool = None
    context.session.state        = SessionState.AWAITING_CONFIRM
    repository.save_session(context.session)
    try:
        notifier.send(
            context.wa_message.from_number,
            format_plan_summary(plan, plan_args.get("summary", "")),
        )
    except Exception:
        pass


def interrupt_delay(args: dict, context, doc_config: dict, repository, notifier):
    """Enter AWAITING_CONFIRM for a doctor self-reported delay."""
    from tools.delay_report import get_delay_preview

    delay_minutes = int(args.get("delay_minutes", 0))
    preview = get_delay_preview(
        delay_minutes=delay_minutes,
        doctor_id=doc_config["doctor_id"],
        hospital_id=context.wa_message.hospital_id,
    )
    if "error" in preview:
        try:
            notifier.send(context.wa_message.from_number, preview["error"])
        except Exception:
            pass
        return

    context.session.pending_tool = ToolCall(
        tool_name="report_delay",
        args={"preview": preview},
        tool_use_id=str(uuid.uuid4()),
    )
    context.session.state = SessionState.AWAITING_CONFIRM
    repository.save_session(context.session)
    try:
        notifier.send(context.wa_message.from_number, format_delay_preview(preview))
    except Exception:
        pass


def build_pending_plan(plan_args: dict) -> list:
    return [
        PlanAction(
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
        )
        for a in plan_args.get("actions", [])
    ]


def execute_approved_plan(plan: list, context, notifier):
    """Execute a confirmed admin action plan and notify patients."""
    from tools.bulk_ops import bulk_reschedule
    from workers.notification_worker import notify_patients_bulk

    result = bulk_reschedule(plan, hospital_id=context.wa_message.hospital_id)

    if result.rolled_back:
        try:
            notifier.send(
                context.wa_message.from_number,
                "Execution failed — no changes were made. Please try again.",
            )
        except Exception:
            pass
        return

    for action in result.succeeded:
        if action.action_type == "REASSIGN" and action.new_token_number is not None:
            action.notification_message = action.notification_message.replace(
                "#?", f"#{action.new_token_number}"
            )

    job_id = notify_patients_bulk(result.succeeded, notifier)
    total  = len(plan)
    done   = len(result.succeeded)
    try:
        notifier.send(
            context.wa_message.from_number,
            f"✅ Executed. {done}/{total} DB updates done. "
            f"Notifying patients in background... (Job: {job_id})",
        )
    except Exception:
        pass
