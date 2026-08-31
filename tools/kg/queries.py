import re
import sqlite3
from pathlib import Path

import numpy as np

from tools.kg.client import driver, database, embed_client, EMBED_MODEL, TENANT_ID
from orchestrator.tracing import traced, record_usage

_SQLITE_PATH = Path(__file__).parent.parent.parent / "data" / "slots.db"


@traced("kg_retriever.embed", run_type="llm", tags=["kg", "embedding"])
def embed(text: str) -> np.ndarray:
    resp = embed_client.embeddings.create(input=text, model=EMBED_MODEL)
    record_usage(resp, model=EMBED_MODEL)
    return np.array(resp.data[0].embedding, dtype=np.float32)


def find_doctors_by_specialization(spec: str, limit: int = 20, tenant_id: str | None = None) -> list[dict]:
    where = "AND d.tenant_id STARTS WITH $tenant_id" if tenant_id else ""

    def _tx(tx):
        return [dict(r) for r in tx.run(
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
        )]

    with driver.session(database=database) as s:
        return s.execute_read(_tx)


def find_doctors_by_language(lang: str, limit: int = 20, tenant_id: str | None = None) -> list[dict]:
    where = "AND d.tenant_id STARTS WITH $tenant_id" if tenant_id else ""

    def _tx(tx):
        return [dict(r) for r in tx.run(
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
        )]

    with driver.session(database=database) as s:
        return s.execute_read(_tx)


def _sanitize_lucene(keyword: str) -> str:
    keyword = keyword.strip()
    if keyword.startswith("[") and keyword.endswith("]"):
        names = re.findall(r"'([^']+)'|\"([^\"]+)\"", keyword)
        flat  = [n[0] or n[1] for n in names if n[0] or n[1]]
        if flat:
            return " OR ".join(f'"{n}"' for n in flat)
    return re.sub(r'[+\-!(){}\[\]^"~*?:\\]', " ", keyword).strip()


def find_by_fulltext(keyword: str, limit: int = 20, tenant_id: str | None = None) -> list[dict]:
    keyword = _sanitize_lucene(keyword)
    term    = keyword
    if " " not in keyword and len(keyword) >= 3:
        term = f"{keyword} OR {keyword}*"

    def _tx(tx):
        return [dict(r) for r in tx.run(
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
        )]

    with driver.session(database=database) as s:
        return s.execute_read(_tx)


def semantic_search(query: str, n_results: int = 8, tenant_id: str | None = None) -> list[dict]:
    q_vec         = embed(query)
    tenant_filter = "AND d.tenant_id STARTS WITH $tenant_id" if tenant_id else ""

    def _tx(tx):
        return [dict(r) for r in tx.run(
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
        )]

    with driver.session(database=database) as s:
        rows = s.execute_read(_tx)

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


def next_available_slots(doctor_ids: list[str]) -> dict[str, dict]:
    ids = [i for i in doctor_ids if i]
    if not ids or not _SQLITE_PATH.exists():
        return {}
    out: dict[str, dict] = {}
    conn = sqlite3.connect(str(_SQLITE_PATH))
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
