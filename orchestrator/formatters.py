import re
from collections import defaultdict
from datetime import datetime
from typing import List


def _format_time_12h(raw: str) -> str:
    """'09:30:00' -> '9:30 AM'. Falls back to the raw string if it's not
    in the expected HH:MM:SS shape, rather than raising on unexpected input."""
    try:
        return datetime.strptime(raw, "%H:%M:%S").strftime("%I:%M %p").lstrip("0")
    except (ValueError, TypeError):
        return raw


def doctor_display_name(args: dict) -> str:
    if args.get("doctor_name"):
        return args["doctor_name"]
    raw = args.get("doctor_id", "the doctor")
    raw = re.sub(r"--[a-z]+$", "", raw)
    raw = re.sub(r"-[a-z]{2,4}$", "", raw)
    return raw.replace("-", " ").strip().title()


def describe_tool(tool_call) -> str:
    action   = tool_call.args.get("action", "BOOK").upper()
    doctor   = doctor_display_name(tool_call.args)
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


def format_booking_result(result: dict, tool_args: dict, labels: dict | None = None) -> str | None:
    """Render the booking-confirmation card.

    `labels` optionally supplies translated field names (see
    orchestrator.llm.CARD_LABELS for the expected keys); any missing key
    falls back to its English default.
    """
    if result.get("action") not in ("BOOK", "RESCHEDULE"):
        return None
    booking = result.get("result", {})
    if booking.get("status") != "CONFIRMED":
        return None

    # SlotBookingConfirmation has appointment_id, BookingConfirmation has
    # token_number — the two shapes never overlap, so this alone tells us
    # which mode produced the result without needing it passed in separately.
    if "appointment_id" in booking:
        return _format_slot_confirmation(booking, tool_args, labels)
    return _format_token_confirmation(booking, tool_args, labels)


def _patient_name_line(booking: dict, tool_args: dict, L) -> str | None:
    """Shared by both confirmation cards so the two layouts stay in sync —
    same field, same position, same relation-suffix rule as describe_tool().
    Returns None (omit the line entirely) when there's no name to show,
    rather than a line with a blank value."""
    name = booking.get("patient_name", tool_args.get("patient_name", ""))
    if not name:
        return None
    relation = booking.get("relation_to_requester", tool_args.get("relation_to_requester", "self"))
    suffix   = f" ({relation})" if relation and relation != "self" else ""
    return f"🙍 *{L('patient', 'Patient Name')}:* {name}{suffix}"


def _format_token_confirmation(booking: dict, tool_args: dict, labels: dict | None = None) -> str:
    labels = labels or {}
    L = lambda key, default: labels.get(key, default)

    token    = booking.get("token_number", "?")
    doctor   = booking.get("doctor_name", tool_args.get("doctor_name", "the doctor"))
    dept     = booking.get("department", "")
    hospital = booking.get("hospital_name", "")
    address  = booking.get("hospital_address", "")
    fee      = booking.get("fee")
    eta      = booking.get("estimated_time", "")
    date_str = tool_args.get("date", "today")

    lines = [f"✅ *{L('appointment_confirmed', 'Appointment Confirmed')}*\n"]
    lines.append(f"🎫 *{L('token', 'Token')}:* #{token}")
    patient_line = _patient_name_line(booking, tool_args, L)
    if patient_line:
        lines.append(patient_line)
    lines.append(f"👨‍⚕️ *{L('doctor', 'Doctor')}:* {doctor}")
    if dept:
        lines.append(f"🏛 *{L('department', 'Department')}:* {dept}")
    if hospital:
        lines.append(f"🏥 *{L('hospital', 'Hospital')}:* {hospital}")
    if address:
        lines.append(f"📍 *{L('address', 'Address')}:* {address}")
    lines.append(f"📅 *{L('date', 'Date')}:* {date_str}")
    if eta and "T" in str(eta):
        lines.append(f"⏰ *{L('reporting_time', 'Reporting Time')}:* {str(eta).split('T')[1][:5]}")
    if fee:
        lines.append(f"💰 *{L('fee', 'Fee')}:* ₹{int(fee)}")
    return "\n".join(lines)


def _format_slot_confirmation(booking: dict, tool_args: dict, labels: dict | None = None) -> str:
    """Same field order/layout as _format_token_confirmation — the only
    structural difference is this card's identifier line is the slot's
    own time instead of a queue token number, since in SLOT mode the
    booked time *is* the reporting time (no separate ETA to show)."""
    labels = labels or {}
    L = lambda key, default: labels.get(key, default)

    doctor    = booking.get("doctor_name", tool_args.get("doctor_name", "the doctor"))
    dept      = booking.get("department", "")
    hospital  = booking.get("hospital_name", "")
    address   = booking.get("hospital_address", "")
    slot_date = booking.get("slot_date", tool_args.get("date", "today"))
    slot_time = booking.get("slot_time", "")
    fee       = booking.get("fee")
    header    = (L("appointment_rescheduled", "Appointment Rescheduled") if booking.get("was_rescheduled")
                 else L("appointment_confirmed", "Appointment Confirmed"))

    lines = [f"✅ *{header}*\n"]
    lines.append(f"⏰ *{L('time', 'Time')}:* {_format_time_12h(slot_time)}")
    patient_line = _patient_name_line(booking, tool_args, L)
    if patient_line:
        lines.append(patient_line)
    lines.append(f"👨‍⚕️ *{L('doctor', 'Doctor')}:* {doctor}")
    if dept:
        lines.append(f"🏛 *{L('department', 'Department')}:* {dept}")
    if hospital:
        lines.append(f"🏥 *{L('hospital', 'Hospital')}:* {hospital}")
    if address:
        lines.append(f"📍 *{L('address', 'Address')}:* {address}")
    lines.append(f"📅 *{L('date', 'Date')}:* {slot_date}")
    if fee:
        lines.append(f"💰 *{L('fee', 'Fee')}:* ₹{int(fee)}")
    return "\n".join(lines)


def format_plan_summary(plan: list, summary_line: str) -> str:
    sections = [f"📋 *ACTION PLAN*\n{summary_line}"]

    reassign: dict[str, list] = defaultdict(list)
    for a in plan:
        if a.action_type == "REASSIGN":
            reassign[a.new_doctor_name or "another doctor"].append(a.patient_name)
    for doctor, patients in reassign.items():
        block = [f"🔄 *Reassigned → Dr. {doctor}* ({len(patients)} patients)"]
        block += [f"{i}. {n}" for i, n in enumerate(patients, 1)]
        sections.append("\n".join(block))

    shifts: dict[int, list] = defaultdict(list)
    for a in plan:
        if a.action_type == "SHIFT":
            shifts[a.delay_minutes or 0].append(a.patient_name)
    for delay, patients in sorted(shifts.items()):
        label = f"{delay} min" if delay else "unknown duration"
        block = [f"⏰ *Shifted +{label}* ({len(patients)} patients)"]
        block += [f"{i}. {n}" for i, n in enumerate(patients, 1)]
        sections.append("\n".join(block))

    retains = [a.patient_name for a in plan if a.action_type == "RETAIN"]
    if retains:
        block = [f"✅ *No change* ({len(retains)} patients — on schedule)"]
        block += [f"{i}. {n}" for i, n in enumerate(retains, 1)]
        sections.append("\n".join(block))

    sections.append("Reply *YES* to execute or *NO* to cancel.")
    return "\n\n".join(sections)


def format_delay_preview(preview: dict) -> str:
    delay    = preview["delay_minutes"]
    doctor   = preview["doctor_name"]
    patients = preview["patients"]
    lines    = [f"{p['token_number']}. {p['patient_name']} — Reporting time: {p['estimated_time']}"
                for p in patients]
    return "\n\n".join([
        f"📋 *Delay Notification Preview*\nDr. {doctor} — {delay}-min delay · {len(patients)} patients waiting",
        "\n".join(lines),
        "Reply *YES* to send notifications or *NO* to cancel.",
    ])


def chunk_text(text: str, max_chars: int) -> List[str]:
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
