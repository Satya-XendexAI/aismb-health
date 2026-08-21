import json
import os
import sqlite3
from pathlib import Path

import numpy as np
from dotenv import load_dotenv
from neo4j import GraphDatabase
from openai import OpenAI

load_dotenv()

# ── Neo4j ──────────────────────────────────────────────────────────────────────

_URI      = os.getenv("NEO4J_URI",      "bolt://localhost:7687")
_USER     = os.getenv("NEO4J_USERNAME", "neo4j")
_PASSWORD = os.getenv("NEO4J_PASSWORD", "")
_DB       = os.getenv("NEO4J_DATABASE", "neo4j")

_driver = GraphDatabase.driver(_URI, auth=(_USER, _PASSWORD))

# ── Gemini clients (OpenAI-compatible) ────────────────────────────────────────

_gemini_client = OpenAI(
    api_key=os.getenv("GEMINI_API_KEY", ""),
    base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
)

_embed_client = _gemini_client
_EMBED_MODEL  = os.getenv("GEMINI_EMBED_MODEL", "models/gemini-embedding-001")
_PARSE_MODEL  = os.getenv("GEMINI_PARSE_MODEL", "models/gemini-3.5-flash-lite")

# ── Config ─────────────────────────────────────────────────────────────────────

_SQLITE_PATH  = os.getenv("SQLITE_PATH",   str(Path(__file__).parent.parent / "data" / "slots.db"))
TENANT_ID     = os.getenv("TENANT_ID",     "glh-chn")
HOSPITAL_NAME = os.getenv("HOSPITAL_NAME", "Hospital")

# ── Small-LLM query parser ─────────────────────────────────────────────────────

_PARSE_SYSTEM = """You are a medical query parser. Extract entities from the patient's query.

Symptom → specialization mapping (use EXACT strings):
chest pain, heart → CARDIOLOGY
bone, joint, knee, hip, fracture → ORTHOPAEDICS
brain, headache, seizure, stroke, neuro → NEUROLOGY
skin, rash, acne → DERMATOLOGY
eye, vision → OPHTHALMOLOGY
ear, nose, throat, sinus → ENT EAR-NOSE-THROAT
child, baby, pediatric → PAEDIATRICS
diabetes, thyroid → Internal Medicine and Diabetology
stomach, gastro, liver, digestion → GASTRO ENTROLOGY
cancer, tumor → MEDICAL ONCOLOGY
kidney → NEPHROLOGY
breathing, lung, asthma → PULMONARY MEDICINE
mental, anxiety, depression → Mental Health
spine, disc, sciatica → SPINE SURGERY
urine, prostate → UROLOGY
pregnancy, women, gynae → OBSTETRICS & GYNAECOLOGY

Respond ONLY with JSON:
{"specializations": [...], "doctor_name": null, "language": null, "min_experience": null}"""


def _parse_query(query: str) -> dict:
    resp = _gemini_client.chat.completions.create(
        model=_PARSE_MODEL,
        messages=[
            {"role": "system", "content": _PARSE_SYSTEM},
            {"role": "user",   "content": query},
        ],
        response_format={"type": "json_object"},
        temperature=0.0,
        max_tokens=200,
    )
    try:
        parsed = json.loads(resp.choices[0].message.content)
    except json.JSONDecodeError:
        parsed = {}
    parsed.setdefault("specializations",  [])
    parsed.setdefault("doctor_name",      None)
    parsed.setdefault("language",         None)
    parsed.setdefault("min_experience",   None)
    return parsed


# ── Gemini embed function ──────────────────────────────────────────────────────

def embed(text: str) -> np.ndarray:
    resp = _embed_client.embeddings.create(input=text, model=_EMBED_MODEL)
    return np.array(resp.data[0].embedding, dtype=np.float32)

# ── Graph queries ──────────────────────────────────────────────────────────────

def find_doctors_by_specialization(spec: str, limit: int = 20, tenant_id: str | None = None) -> list[dict]:
    root   = spec[:5].lower() if len(spec) >= 5 else spec.lower()
    lucene = f"{spec} OR {root}*" if root != spec.lower() else spec
    where  = "WHERE d.tenant_id STARTS WITH $tenant_id" if tenant_id else ""
    with _driver.session(database=_DB) as s:
        res = s.run(
            f"""
            CALL db.index.fulltext.queryNodes('spec_ft', $spec) YIELD node AS s, score
            MATCH (d:Doctor)-[:SPECIALIZES_IN]->(s)
            {where}
            OPTIONAL MATCH (d)-[:PRACTICES_AT]->(h:Hospital)
            RETURN d.sql_id AS sql_id, d.id AS id, d.name AS name,
                   d.designation AS designation, d.experience_years AS experience_years,
                   d.consultation_fee AS consultation_fee, d.tenant_id AS tenant_id,
                   collect(DISTINCT s.name) AS specializations,
                   collect(DISTINCT h.name) AS hospitals,
                   collect(DISTINCT h.city)  AS cities
            ORDER BY d.experience_years DESC
            LIMIT $limit
            """,
            spec=lucene, limit=limit, tenant_id=tenant_id,
        )
        return [dict(r) for r in res]


def find_doctors_by_language(lang: str, limit: int = 20, tenant_id: str | None = None) -> list[dict]:
    where = "AND d.tenant_id STARTS WITH $tenant_id" if tenant_id else ""
    with _driver.session(database=_DB) as s:
        res = s.run(
            f"""
            MATCH (d:Doctor)-[:SPEAKS]->(l:Language)
            WHERE toLower(l.name) CONTAINS toLower($lang)
            {where}
            OPTIONAL MATCH (d)-[:SPECIALIZES_IN]->(sp:Specialization)
            OPTIONAL MATCH (d)-[:PRACTICES_AT]->(h:Hospital)
            RETURN d.sql_id AS sql_id, d.id AS id, d.name AS name,
                   d.designation AS designation, d.experience_years AS experience_years,
                   d.consultation_fee AS consultation_fee, d.tenant_id AS tenant_id,
                   collect(DISTINCT sp.name) AS specializations,
                   collect(DISTINCT h.name)  AS hospitals,
                   collect(DISTINCT h.city)  AS cities
            ORDER BY d.name
            LIMIT $limit
            """,
            lang=lang, limit=limit, tenant_id=tenant_id,
        )
        return [dict(r) for r in res]


def find_by_fulltext(keyword: str, limit: int = 20, tenant_id: str | None = None) -> list[dict]:
    term = keyword
    if " " not in keyword and len(keyword) >= 3:
        term = f"{keyword} OR {keyword}*"
    with _driver.session(database=_DB) as s:
        res = s.run(
            """
            CALL db.index.fulltext.queryNodes('doctor_ft', $term) YIELD node AS d, score
            WHERE $tenant_id IS NULL OR d.tenant_id STARTS WITH $tenant_id
            WITH d, score
            OPTIONAL MATCH (d)-[:SPECIALIZES_IN]->(sp:Specialization)
            OPTIONAL MATCH (d)-[:PRACTICES_AT]->(h:Hospital)
            OPTIONAL MATCH (d)-[:SPEAKS]->(l:Language)
            WITH d, score,
                 collect(DISTINCT sp.name) AS specializations,
                 collect(DISTINCT h.name)  AS hospitals,
                 collect(DISTINCT l.name)  AS languages
            RETURN d.sql_id AS sql_id, d.id AS id, d.name AS name,
                   d.designation AS designation, d.experience_years AS experience_years,
                   d.consultation_fee AS consultation_fee,
                   d.tenant_id AS tenant_id, specializations, hospitals, languages
            ORDER BY score DESC
            LIMIT $limit
            """,
            term=term, limit=limit, tenant_id=tenant_id,
        )
        return [dict(r) for r in res]

# ── Vector search against Neo4j-stored embeddings ─────────────────────────────

def semantic_search(query: str, n_results: int = 8, tenant_id: str | None = None) -> list[dict]:
    q_vec       = embed(query)
    tenant_filter = "AND d.tenant_id STARTS WITH $tenant_id" if tenant_id else ""
    with _driver.session(database=_DB) as s:
        res = s.run(
            f"""
            MATCH (d:Doctor)
            WHERE d.embedding IS NOT NULL
            {tenant_filter}
            OPTIONAL MATCH (d)-[:SPECIALIZES_IN]->(sp:Specialization)
            OPTIONAL MATCH (d)-[:PRACTICES_AT]->(h:Hospital)
            OPTIONAL MATCH (d)-[:SPEAKS]->(l:Language)
            RETURN d.sql_id AS sql_id, d.id AS id, d.name AS name,
                   d.designation AS designation, d.experience_years AS experience_years,
                   d.consultation_fee AS consultation_fee,
                   d.tenant_id AS tenant_id, d.embedding AS embedding,
                   collect(DISTINCT sp.name) AS specializations,
                   collect(DISTINCT h.name)  AS hospitals,
                   collect(DISTINCT l.name)  AS languages
            """,
            tenant_id=tenant_id,
        )
        rows = [dict(r) for r in res]

    if not rows:
        return []

    doc_vecs = np.array([r.pop("embedding") for r in rows], dtype=np.float32)
    norms    = np.linalg.norm(doc_vecs, axis=1, keepdims=True)
    doc_vecs = doc_vecs / np.where(norms == 0, 1, norms)
    q_norm   = q_vec / (np.linalg.norm(q_vec) or 1)
    scores   = doc_vecs @ q_norm

    ranked = sorted(zip(scores.tolist(), rows), key=lambda x: x[0], reverse=True)
    return [
        {"id": r.get("sql_id") or r.get("id", ""), "text": r.get("bio", ""),
         "metadata": r, "score": float(sc)}
        for sc, r in ranked[:n_results]
        if sc >= 0.35
    ]

# ── SQLite slot lookup ─────────────────────────────────────────────────────────

def next_available_slots(doctor_ids: list[str]) -> dict[str, dict]:
    ids = [i for i in doctor_ids if i]
    if not ids or not Path(_SQLITE_PATH).exists():
        return {}
    out: dict[str, dict] = {}
    conn = sqlite3.connect(_SQLITE_PATH)
    try:
        cur = conn.cursor()
        for did in ids:
            cur.execute(
                "SELECT date, time FROM time_slots WHERE doctor_id = ? AND available = 1 ORDER BY date, time LIMIT 1",
                (did,),
            )
            row = cur.fetchone()
            if row:
                out[did] = {"date": row[0], "time": row[1]}
    finally:
        conn.close()
    return out

# ── RRF fusion ─────────────────────────────────────────────────────────────────

def _fuse_results(vector_results: list[dict], graph_results: list[dict], n: int = 6) -> list[dict]:
    scores:   dict[str, float] = {}
    doc_data: dict[str, dict]  = {}
    k = 60
    for rank, r in enumerate(vector_results):
        did = r["metadata"].get("doctor_id") or r["metadata"].get("sql_id") or r["id"]
        scores[did] = scores.get(did, 0) + 1 / (k + rank + 1)
        if did not in doc_data:
            doc_data[did] = r["metadata"]
    for rank, r in enumerate(graph_results):
        did = r.get("sql_id") or r.get("id") or r.get("doctor_id") or ""
        if not did:
            continue
        scores[did] = scores.get(did, 0) + 1 / (k + rank + 1)
        if did not in doc_data:
            doc_data[did] = r
        else:
            doc_data[did].update({kk: vv for kk, vv in r.items() if vv})
    sorted_ids = sorted(scores, key=lambda x: scores[x], reverse=True)
    return [doc_data[did] for did in sorted_ids[:n] if did in doc_data]

# ── Context builder ────────────────────────────────────────────────────────────

def _build_context(fused: list[dict]) -> str:
    if not fused:
        return f"No matching doctors found in {HOSPITAL_NAME}."
    lines = [f"RELEVANT DOCTORS FROM {HOSPITAL_NAME.upper()}:\n"]
    for i, doc in enumerate(fused, 1):
        name  = doc.get("name", "Unknown")
        specs = doc.get("specializations", "")
        if isinstance(specs, list):
            specs = ", ".join(specs)
        exp   = doc.get("experience_years")
        langs = doc.get("languages", "")
        if isinstance(langs, list):
            langs = ", ".join(langs)
        desig = doc.get("designation", "Consultant")
        hosp  = doc.get("hospitals", HOSPITAL_NAME)
        if isinstance(hosp, list):
            hosp = ", ".join(str(h) for h in hosp)
        fee = doc.get("consultation_fee")
        lines.append(f"{i}. {name}")
        lines.append(f"   Designation: {desig}")
        lines.append(f"   Specialization: {specs}")
        if exp:
            lines.append(f"   Experience: {exp} years")
        if langs:
            lines.append(f"   Languages: {langs}")
        lines.append(f"   Hospital: {hosp}")
        if fee:
            lines.append(f"   Consultation Fee: ₹{fee}")
        nxt = doc.get("next_slot")
        if nxt:
            lines.append(f"   Next available: {nxt['date']} {nxt['time']}")
        lines.append("")
    return "\n".join(lines)

# ── Public API ─────────────────────────────────────────────────────────────────

def retrieve_context(query: str) -> dict:
    parsed = _parse_query(query)

    graph_results: list[dict] = []

    for spec in parsed["specializations"]:
        graph_results += find_doctors_by_specialization(spec, 8, TENANT_ID)

    if parsed["doctor_name"]:
        graph_results += find_by_fulltext(parsed["doctor_name"], 8, TENANT_ID)

    if parsed["language"]:
        graph_results += find_doctors_by_language(parsed["language"], 8, TENANT_ID)

    # Fallback: broad fulltext on raw query when no structured entities found
    if not graph_results:
        graph_results += find_by_fulltext(query, 8, TENANT_ID)

    vector_results = semantic_search(query, 8, TENANT_ID)
    fused = _fuse_results(vector_results, graph_results)

    slot_map = next_available_slots([d.get("sql_id") or d.get("id") for d in fused])
    for d in fused:
        key = d.get("sql_id") or d.get("id")
        if key in slot_map:
            d["next_slot"] = slot_map[key]

    context_text = _build_context(fused)

    return {
        "context_text": context_text,
        "doctors":      fused,
    }
