import json
import logging
import os

import psycopg2.extras
from dotenv import load_dotenv
from openai import OpenAI

from tools.appointment.database import get_connection
from tools.kg_retriever import find_doctors_by_specialization, TENANT_ID

load_dotenv()

logger = logging.getLogger(__name__)

_gemini_client = OpenAI(
    api_key=os.getenv("GEMINI_API_KEY", ""),
    base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
)
_PARSE_MODEL = os.getenv("GEMINI_PARSE_MODEL", "models/gemini-3.5-flash-lite")

_ELDERLY_AGE = 60


def _resolve_distance(patient_location: str, hospital_city: str) -> dict:
    prompt = (
        f"Given:\n"
        f"  Hospital location: {hospital_city}\n"
        f"  Patient location: {patient_location}\n\n"
        f"Return JSON with exactly these fields:\n"
        f"  estimated_km: integer (straight-line road distance estimate, or null if unknown)\n"
        f"  confidence: one of \"high\", \"medium\", \"low\"\n"
        f"  classification: one of \"local\", \"outstation\"\n\n"
        f"Rules:\n"
        f"- \"outstation\" if patient is from a different city or district\n"
        f"- \"local\" if within the same city/metro area\n"
        f"- confidence \"low\" if location string is vague (neighbourhood, landmark, etc.)"
    )
    try:
        resp = _gemini_client.chat.completions.create(
            model=_PARSE_MODEL,
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            temperature=0.0,
            max_tokens=100,
        )
        data = json.loads(resp.choices[0].message.content)
        confidence = data.get("confidence", "low")
        return {
            "estimated_km":      data.get("estimated_km"),
            "confidence":        confidence,
            "is_outstation":     data.get("classification") == "outstation",
            "distance_confidence": confidence,
        }
    except Exception as exc:
        logger.warning("_resolve_distance failed: %s", exc)
        is_outstation = (patient_location or "").lower().strip() != hospital_city.lower().strip()
        return {
            "estimated_km":      None,
            "confidence":        "low",
            "is_outstation":     is_outstation,
            "distance_confidence": "low",
        }


def get_session_impact(doctor_id: str, date: str, hospital_id: str) -> dict:
    sql = """
        SELECT
            ds.session_id,
            d.name           AS doctor_name,
            t.department     AS department,
            t.token_id,
            t.token_number,
            p.name           AS patient_name,
            p.phone          AS patient_phone,
            p.age,
            p.location
        FROM doctor_sessions ds
        JOIN doctors d  ON d.doctor_id  = ds.doctor_id
        JOIN tokens  t  ON t.session_id = ds.session_id AND t.status = 'WAITING'
        JOIN patients p ON p.patient_id = t.patient_id
        WHERE ds.doctor_id   = %s
          AND ds.date         = %s
          AND ds.hospital_id  = %s
        ORDER BY t.token_number ASC
    """
    with get_connection() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, (str(doctor_id), date, str(hospital_id)))
            rows = cur.fetchall()

    if not rows:
        return {"session_id": None, "doctor_name": None, "department": None,
                "total_waiting": 0, "patients": []}

    hospital_city = _get_hospital_city(hospital_id)

    # Cache distance results by location to avoid redundant Gemini calls
    dist_cache: dict = {}
    patients = []
    for row in rows:
        age = row["age"]
        location = row["location"] or ""
        if location:
            if location not in dist_cache:
                dist_cache[location] = _resolve_distance(location, hospital_city)
            dist = dist_cache[location]
        else:
            dist = {"estimated_km": None, "is_outstation": False, "distance_confidence": "low"}
        patients.append({
            "token_id":            str(row["token_id"]),
            "token_number":        row["token_number"],
            "patient_name":        row["patient_name"],
            "patient_phone":       row["patient_phone"] or "",
            "age":                 age,
            "is_elderly":          (age or 0) >= _ELDERLY_AGE,
            "location":            location,
            "distance_km":         dist["estimated_km"],
            "distance_confidence": dist["distance_confidence"],
            "is_outstation":       dist["is_outstation"],
        })

    first = rows[0]
    return {
        "session_id":    str(first["session_id"]),
        "doctor_name":   first["doctor_name"],
        "department":    first["department"],
        "total_waiting": len(patients),
        "patients":      patients,
    }


def _get_hospital_city(hospital_id: str) -> str:
    env_city = os.getenv("HOSPITAL_CITY", "").strip()
    if env_city:
        return env_city
    sql = "SELECT city FROM hospitals WHERE hospital_id = %s"
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (str(hospital_id),))
                row = cur.fetchone()
                return row[0] if row else ""
    except Exception:
        return ""


def find_available_doctors(specialization: str, date: str, hospital_id: str) -> dict:
    neo4j_docs = find_doctors_by_specialization(specialization, limit=50, tenant_id=TENANT_ID)
    doctor_ids = [d.get("sql_id") or d.get("id") for d in neo4j_docs if d.get("sql_id") or d.get("id")]

    if not doctor_ids:
        return {"specialization": specialization, "available_doctors": []}

    sql = """
        SELECT
            d.doctor_id,
            d.name                       AS doctor_name,
            ds.session_id,
            ds.started_at,
            d.avg_checkin_time,
            d.avg_consultation_minutes,
            COUNT(t.token_id)            AS current_queue,
            COALESCE(ds.started_at, NOW()::date + d.avg_checkin_time)
              + (COUNT(t.token_id) * d.avg_consultation_minutes * INTERVAL '1 minute')
              AS estimated_session_end
        FROM doctors d
        JOIN doctor_sessions ds ON d.doctor_id = ds.doctor_id
        LEFT JOIN tokens t      ON t.session_id = ds.session_id AND t.status = 'WAITING'
        WHERE d.doctor_id  = ANY(%s)
          AND ds.date       = %s
          AND ds.status     = 'OPEN'
          AND d.hospital_id = %s
        GROUP BY d.doctor_id, d.name, ds.session_id, ds.started_at,
                 d.avg_checkin_time, d.avg_consultation_minutes
        ORDER BY current_queue ASC
    """
    with get_connection() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, (doctor_ids, date, str(hospital_id)))
            rows = cur.fetchall()

    available = []
    for row in rows:
        end = row["estimated_session_end"]
        available.append({
            "doctor_id":            str(row["doctor_id"]),
            "doctor_name":          row["doctor_name"],
            "session_id":           str(row["session_id"]),
            "current_queue":        row["current_queue"],
            "started_at":           str(row["started_at"]) if row["started_at"] else None,
            "estimated_session_end": str(end.time())[:5] if end else None,
        })

    return {"specialization": specialization, "available_doctors": available}
