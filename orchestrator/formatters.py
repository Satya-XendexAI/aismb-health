import re
from collections import defaultdict
from typing import List


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


def format_booking_result(result: dict, tool_args: dict) -> str | None:
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
        lines.append(f"⏰ *Reporting Time:* {str(eta).split('T')[1][:5]}")
    if fee:
        lines.append(f"💰 *Fee:* ₹{int(fee)}")
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
