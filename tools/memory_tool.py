"""
memory_tool.py — Patient profile + family + appointment context loader.

Fetches the patient's own record, all registered family members, and their
recent/active appointments in a single JOIN query. Returns both a structured
dict (for the LLM tool call response) and a formatted string (for system
prompt injection).
"""

import os
import logging
import psycopg2
import psycopg2.extras
from datetime import date
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)


def _db_connection():
    return psycopg2.connect(
        host=os.getenv("DB_HOST", "localhost"),
        port=int(os.getenv("DB_PORT", 5432)),
        dbname=os.getenv("DB_NAME", "postgres"),
        user=os.getenv("DB_USER", "postgres"),
        password=os.getenv("DB_PASSWORD", ""),
    )


def _fetch_raw(conn, phone: str, hospital_id: str) -> list[dict]:
    """TOKEN-mode bookings (patients LEFT JOIN tokens) UNION ALL SLOT-mode
    bookings (patients JOIN appointments) — the first branch's LEFT JOIN is
    what still surfaces a family member with no booking at all; the second
    branch uses a plain JOIN so it only ever *adds* slot rows on top of that,
    never a duplicate no-booking row (both branches get de-duplicated by
    relation in _build_output anyway, so an overlap would be harmless, but
    a plain JOIN avoids it outright)."""
    sql = """
        SELECT * FROM (
            SELECT
                p.patient_id,
                p.name                  AS patient_name,
                p.age,
                p.location,
                p.phone                 AS patient_phone,
                p.relation_to_requester AS relation,
                t.token_number,
                NULL::text              AS slot_time,
                t.status                AS booking_status,
                ds.date::text           AS appt_date,
                d.name                  AS doctor_name,
                t.department
            FROM patients p
            LEFT JOIN tokens t
                ON  t.patient_id = p.patient_id
            LEFT JOIN doctor_sessions ds
                ON  ds.session_id = t.session_id
            LEFT JOIN doctors d
                ON  d.doctor_id = t.doctor_id
            WHERE p.hospital_id        = %s
              AND p.requested_by_phone = %s
              AND (
                  t.token_id IS NULL
                  OR t.status = 'WAITING'
                  OR ds.date >= (CURRENT_DATE - INTERVAL '30 days')
              )

            UNION ALL

            SELECT
                p.patient_id,
                p.name                   AS patient_name,
                p.age,
                p.location,
                p.phone                  AS patient_phone,
                p.relation_to_requester  AS relation,
                NULL::integer            AS token_number,
                s.start_time::text       AS slot_time,
                a.status                 AS booking_status,
                a.appointment_date::text AS appt_date,
                d.name                   AS doctor_name,
                a.department
            FROM patients p
            JOIN appointments a ON a.patient_id = p.patient_id
            JOIN slots s        ON a.slot_id    = s.slot_id
            JOIN doctors d      ON d.doctor_id  = a.doctor_id
            WHERE p.hospital_id        = %s
              AND p.requested_by_phone = %s
              AND (
                  a.status = 'SCHEDULED'
                  OR a.appointment_date >= (CURRENT_DATE - INTERVAL '30 days')
              )
        ) combined
        ORDER BY relation, appt_date DESC NULLS LAST
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, (str(hospital_id), phone, str(hospital_id), phone))
        return [dict(r) for r in cur.fetchall()]


def _build_output(rows: list[dict]) -> dict:
    self_record        = None
    family: list       = []
    active_appts: list = []
    recent_appts: list = []
    seen_members: dict = {}   # relation → member dict (de-duplicate)

    today = date.today().isoformat()

    for r in rows:
        relation = (r["relation"] or "self").lower()
        name     = r["patient_name"]
        key      = relation

        if key not in seen_members:
            member = {
                "relation": relation,
                "name":     name,
                "age":      r["age"],
                "phone":    r["patient_phone"],
            }
            seen_members[key] = member
            if relation == "self":
                self_record = {"name": name, "age": r["age"], "location": r["location"]}
            else:
                family.append(member)

        if r["token_number"] is not None or r["slot_time"] is not None:
            appt = {
                "patient":    name,
                "relation":   relation,
                "doctor":     r["doctor_name"],
                "department": r["department"],
                "date":       r["appt_date"],
                "token":      r["token_number"],   # None for SLOT-mode rows
                "time":       r["slot_time"],       # None for TOKEN-mode rows
                "status":     r["booking_status"],
            }
            if r["booking_status"] in ("WAITING", "SCHEDULED") and r["appt_date"] >= today:
                active_appts.append(appt)
            else:
                recent_appts.append(appt)

    return {
        "self":                 self_record,
        "family":               family,
        "active_appointments":  active_appts,
        "recent_appointments":  recent_appts,
    }


def _format_context(data: dict) -> str:
    lines = []

    if data["self"]:
        s = data["self"]
        parts = [s["name"]]
        if s.get("age"):
            parts.append(str(s["age"]))
        if s.get("location"):
            parts.append(s["location"])
        lines.append(f"• Self: {', '.join(parts)}")

    if data["family"]:
        members = []
        for m in data["family"]:
            detail = f"{m['relation']}={m['name']}"
            if m.get("age"):
                detail += f"({m['age']}"
                detail += f", {m['phone']}" if m.get("phone") else ", no phone"
                detail += ")"
            members.append(detail)
        lines.append(f"• Family: {', '.join(members)}")

    if data["active_appointments"]:
        lines.append("• Active appointments:")
        for a in data["active_appointments"]:
            rel = f" ({a['relation']})" if a["relation"] != "self" else ""
            slot_label = f"Token #{a['token']}" if a["token"] is not None else f"{a['time']}"
            lines.append(
                f"  - {a['patient']}{rel} → {a['doctor']}, {a['department']}, "
                f"{a['date']}, {slot_label}"
            )

    if data["recent_appointments"]:
        lines.append("• Recent (last 30 days):")
        for a in data["recent_appointments"]:
            rel = f" ({a['relation']})" if a["relation"] != "self" else ""
            slot_label = f"Token #{a['token']}" if a["token"] is not None else f"{a['time']}"
            lines.append(
                f"  - {a['patient']}{rel} → {a['doctor']}, {a['department']}, "
                f"{a['date']}, {slot_label} [{a['status']}]"
            )

    return "\n".join(lines) if lines else ""


def fetch_patient_context(phone: str, hospital_id: str) -> tuple[dict, str]:
    """
    Returns (structured_dict, formatted_string).
    structured_dict  → sent as tool call result to the LLM
    formatted_string → injected into system prompt header
    """
    try:
        conn = _db_connection()
        try:
            rows = _fetch_raw(conn, phone, hospital_id)
        finally:
            conn.close()
    except Exception as exc:
        logger.error("memory_tool DB error: %s", exc)
        return {}, ""

    data       = _build_output(rows)
    context_str = _format_context(data)
    return data, context_str


def run(phone: str, hospital_id: str) -> dict:
    """Entry point when called as an LLM tool."""
    data, _ = fetch_patient_context(phone, hospital_id)
    if not data:
        return {"message": "No profile found. This may be your first visit."}
    return data
