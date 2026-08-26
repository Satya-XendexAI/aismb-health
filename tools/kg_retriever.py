import json
import os
import re
import ssl
import difflib
import sqlite3
from pathlib import Path

import numpy as np
from dotenv import load_dotenv
from neo4j import GraphDatabase
from openai import OpenAI
from prompts.kg_retriever import KG_PARSE_SYSTEM

load_dotenv()

# ── Neo4j ──────────────────────────────────────────────────────────────────────

_URI      = os.getenv("NEO4J_URI",      "bolt://localhost:7687")
_USER     = os.getenv("NEO4J_USERNAME", "neo4j")
_PASSWORD = os.getenv("NEO4J_PASSWORD", "")
_DB       = os.getenv("NEO4J_DATABASE", "neo4j")
_INSECURE = os.getenv("NEO4J_INSECURE", "false").lower() in ("1", "true", "yes", "on")


def _make_driver():
    uri = _URI
    if _INSECURE:
        # Self-signed / TLS-intercepting proxy: keep encryption but skip
        # cert verification. For +s schemes use the +ssc variant (which the
        # driver forbids combining with an explicit ssl_context).
        if "+s://" in uri:
            uri = uri.replace("+s://", "+ssc://", 1)
            return GraphDatabase.driver(uri, auth=(_USER, _PASSWORD))
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return GraphDatabase.driver(uri, auth=(_USER, _PASSWORD), ssl_context=ctx)
    return GraphDatabase.driver(uri, auth=(_USER, _PASSWORD))


_driver = _make_driver()

# ── Gemini clients (OpenAI-compatible) ────────────────────────────────────────

_gemini_client = OpenAI(
    api_key=os.getenv("GEMINI_API_KEY", ""),
    base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
)

_embed_client = _gemini_client
_EMBED_MODEL  = os.getenv("GEMINI_EMBED_MODEL", "models/gemini-embedding-001")

# ── Config ─────────────────────────────────────────────────────────────────────

_SQLITE_PATH  = os.getenv("SQLITE_PATH",   str(Path(__file__).parent.parent / "data" / "slots.db"))
TENANT_ID     = os.getenv("TENANT_ID",     "glh-chn")
HOSPITAL_NAME = os.getenv("HOSPITAL_NAME", "Hospital")

# ── Query parameter resolver (no LLM, no hardcoded symptom map) ───────────────────
# The main agent LLM is expected to extract the specialization / language from the
# patient's words and pass them as STRUCTURED arguments to kg_retriever. This module
# only RESOLVES those free-text values onto the live knowledge graph via a dynamic
# lookup (matching against real node names). There is intentionally NO symptom ->
# specialization table hardcoded here.

_SPEC_NAMES_CACHE = None


def _specialization_names() -> list[str]:
    global _SPEC_NAMES_CACHE
    if _SPEC_NAMES_CACHE is None:
        try:
            with _driver.session(database=_DB) as s:
                rows = s.run("MATCH (sp:Specialization) RETURN sp.name AS name").data()
            _SPEC_NAMES_CACHE = [r["name"] for r in rows]
        except Exception:
            _SPEC_NAMES_CACHE = []
    return _SPEC_NAMES_CACHE


def _resolve_specializations(specs) -> list[str]:
    """Map LLM-provided specialization strings onto real graph node names.

    No symptom->specialization mapping: we match the given text against the actual
    specialization node names via (a) exact substring either direction, then (b) a
    fuzzy close-match (difflib) so spelling variants like 'Orthopedics' vs
    'Orthopaedics' still resolve. Fully dynamic — nothing hardcoded."""
    if isinstance(specs, str):
        specs = [specs]
    names = _specialization_names()
    lower_names = [n.lower() for n in names]
    resolved: list[str] = []
    for sp in specs or []:
        sp_l = (sp or "").lower().strip()
        if not sp_l:
            continue
        exact = [n for n, nl in zip(names, lower_names) if sp_l in nl or nl in sp_l]
        if exact:
            resolved.extend(exact)
            continue
        close = difflib.get_close_matches(sp_l, lower_names, n=1, cutoff=0.75)
        if close:
            resolved.append(names[lower_names.index(close[0])])
        else:
            resolved.append(sp)  # raw fallback; fulltext may still match
    # de-duplicate, preserve order
    seen, out = set(), []
    for r in resolved:
        if r not in seen:
            seen.add(r)
            out.append(r)
    return out


# ── Small-LLM query parser (generic; NO hardcoded symptom -> specialty map) ──────
# A cheap dedicated LLM extracts structured entities; it relies on the model's own
# medical knowledge (no fixed list baked into the prompt). The values are then
# resolved dynamically onto graph nodes by _resolve_specializations, so the main
# agent system prompt stays clean and there is no hardcoded mapping to maintain.

_PARSE_MODEL = os.getenv("GEMINI_PARSE_MODEL", "models/gemini-3.5-flash-lite")


def _parse_query(query: str) -> dict:
    resp = _gemini_client.chat.completions.create(
        model=_PARSE_MODEL,
        messages=[
            {"role": "system", "content": KG_PARSE_SYSTEM},
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
    # The spec is already resolved to an exact graph node name by
    # _resolve_specializations, so match precisely. A loose fulltext query with a
    # prefix (e.g. "inte*") wrongly pulls in unrelated specialties like
    # "Interventional Pulmonology" for "Internal Medicine".
    where = "AND d.tenant_id STARTS WITH $tenant_id" if tenant_id else ""
    with _driver.session(database=_DB) as s:
        res = s.run(
            f"""
            MATCH (d:Doctor)-[:SPECIALIZES_IN]->(s:Specialization)
            WHERE toLower(s.name) CONTAINS toLower($spec)
            {where}
            OPTIONAL MATCH (d)-[:PRACTICES_AT]->(h:Hospital)
            OPTIONAL MATCH (d)-[:SPEAKS]->(l:Language)
            RETURN d.sql_id AS sql_id, d.id AS id, d.name AS name,
                   d.designation AS designation, d.experience_years AS experience_years,
                   d.consultation_fee AS consultation_fee, d.tenant_id AS tenant_id,
                   collect(DISTINCT s.name) AS specializations,
                   collect(DISTINCT h.name) AS hospitals,
                   collect(DISTINCT h.city)  AS cities,
                   collect(DISTINCT l.name) AS languages
            ORDER BY d.experience_years DESC
            LIMIT $limit
            """,
            spec=spec, limit=limit, tenant_id=tenant_id,
        )
        return [dict(r) for r in res]


def find_doctors_by_language(lang: str, limit: int = 20, tenant_id: str | None = None) -> list[dict]:
    where = "AND d.tenant_id STARTS WITH $tenant_id" if tenant_id else ""
    with _driver.session(database=_DB) as s:
        res = s.run(
            f"""
            MATCH (d:Doctor)-[:SPEAKS]->(l:Language)
            WHERE toLower(l.name) CONTAINS toLower($lang)
               OR toLower($lang) CONTAINS toLower(l.name)
            {where}
            OPTIONAL MATCH (d)-[:SPECIALIZES_IN]->(sp:Specialization)
            OPTIONAL MATCH (d)-[:PRACTICES_AT]->(h:Hospital)
            RETURN d.sql_id AS sql_id, d.id AS id, d.name AS name,
                   d.designation AS designation, d.experience_years AS experience_years,
                   d.consultation_fee AS consultation_fee, d.tenant_id AS tenant_id,
                   collect(DISTINCT sp.name) AS specializations,
                   collect(DISTINCT h.name)  AS hospitals,
                   collect(DISTINCT h.city)  AS cities,
                   collect(DISTINCT l.name) AS languages
            ORDER BY d.name
            LIMIT $limit
            """,
            lang=lang, limit=limit, tenant_id=tenant_id,
        )
        return [dict(r) for r in res]


def _sanitize_lucene(keyword: str) -> str:
    keyword = keyword.strip()
    # LLM sometimes returns a Python list repr — extract individual names and OR them
    if keyword.startswith("[") and keyword.endswith("]"):
        names = re.findall(r"'([^']+)'|\"([^\"]+)\"", keyword)
        flat  = [n[0] or n[1] for n in names if n[0] or n[1]]
        if flat:
            return " OR ".join(f'"{n}"' for n in flat)
    # Strip Lucene special characters that would break the query parser
    return re.sub(r'[+\-!(){}\[\]^"~*?:\\]', " ", keyword).strip()


def find_by_fulltext(keyword: str, limit: int = 20, tenant_id: str | None = None) -> list[dict]:
    keyword = _sanitize_lucene(keyword)
    term    = keyword
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
        desig = doc.get("designation", "Consultant")
        fee = doc.get("consultation_fee")
        lines.append(f"{i}. {name}")
        lines.append(f"   Designation: {desig}")
        lines.append(f"   Specialization: {specs}")
        if exp:
            lines.append(f"   Experience: {exp} years")
        if fee:
            lines.append(f"   Consultation Fee: ₹{fee}")
        nxt = doc.get("next_slot")
        if nxt:
            lines.append(f"   Next available: {nxt['date']} {nxt['time']}")
        lines.append("")
    return "\n".join(lines)

def _no_language_response(language: str, specs: list[str]) -> dict:
    """No doctor in the hospital speaks the requested language at all."""
    spec_label = ", ".join(specs) if specs else "this specialty"
    text = (
        f"No doctors who speak {language} are available"
        + (f" for {spec_label}" if specs else "")
        + f". Ask the patient whether they would like to be matched with doctors "
          f"in other languages (for example English or Hindi) before proceeding."
    )
    return {"context_text": text, "doctors": [], "needs_language_broadening": True}


def _no_language_for_specialty_response(language: str, specs: list[str], specialty_results: list[dict]) -> dict:
    """Specialty exists but no doctor of the requested language practices it."""
    avail = set()
    for d in specialty_results:
        for l in d.get("languages", []) or []:
            if l and l.lower() != language.lower():
                avail.add(l)
    avail = sorted(avail)
    spec_label = ", ".join(specs)
    if avail:
        langs_str = ", ".join(avail)
        text = (
            f"No doctors who speak {language} are available for {spec_label}. "
            f"However, {spec_label} is available in these languages: {langs_str}. "
            f"Ask the patient if they would like to see options in those languages instead."
        )
    else:
        text = (
            f"No doctors who speak {language} are available for {spec_label}, "
            f"and this specialty is not listed under any other language in our records. "
            f"Ask the patient how they would like to proceed."
        )
    return {"context_text": text, "doctors": [], "needs_language_broadening": True}


# ── Public API ─────────────────────────────────────────────────────────────────

def retrieve_context(query: str) -> dict:
    """Retrieve doctors for a free-text patient query.

    A small dedicated LLM parses the query into structured entities (no hardcoded
    map). Those free-text values are resolved dynamically onto graph nodes, and
    language is enforced as an INTERSECTION constraint."""
    parsed = _parse_query(query)
    specs = _resolve_specializations(parsed.get("specializations", []))

    # Specialty-matched doctors (any language) — kept separately so we can detect
    # the "specialty exists but no doctor speaks the requested language" case.
    specialty_results: list[dict] = []
    for spec in specs:
        specialty_results += find_doctors_by_specialization(spec, 8, TENANT_ID)

    graph_results: list[dict] = list(specialty_results)

    if parsed.get("doctor_name"):
        graph_results += find_by_fulltext(parsed["doctor_name"], 8, TENANT_ID)

    vector_results = semantic_search(query, 8, TENANT_ID)

    # Language is an INTERSECTION constraint, not an extra result set:
    # every returned doctor must actually speak the requested language.
    language = parsed.get("language")
    if language:
        lang_docs = find_doctors_by_language(language, 100, TENANT_ID)

        # No doctor in the whole hospital speaks the requested language at all.
        if not lang_docs:
            return _no_language_response(language, specs)

        lang_ids = {d.get("sql_id") or d.get("id") for d in lang_docs}

        # Specialty was requested, exists in the graph, but no doctor of that
        # language practices it. Do NOT substitute an unrelated doctor found by
        # semantic search — ask the user to broaden the language instead.
        specialty_lang_match = [
            d for d in specialty_results
            if (d.get("sql_id") or d.get("id")) in lang_ids
        ]
        if specs and specialty_results and not specialty_lang_match:
            return _no_language_for_specialty_response(language, specs, specialty_results)

        # Otherwise enforce the language as an intersection on both result sets.
        if graph_results:
            graph_results = [d for d in graph_results
                             if (d.get("sql_id") or d.get("id")) in lang_ids]
        if vector_results:
            vector_results = [v for v in vector_results
                              if (v.get("metadata", {}).get("sql_id")
                                  or v.get("metadata", {}).get("id")
                                  or v.get("id")) in lang_ids]

        # Only-language queries (no specialty) still honour the language.
        if not graph_results and not vector_results:
            graph_results = lang_docs

    # Safety net: nothing structured matched at all
    if not graph_results and not vector_results:
        graph_results += find_by_fulltext(query, 8, TENANT_ID)

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
