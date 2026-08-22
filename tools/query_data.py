"""
query_data.py — Text-to-SQL tool for doctors.

Translates a natural-language question into a scoped SELECT query
against the hospital PostgreSQL DB, using gemini-3.5-flash-lite for
SQL generation and information_schema for live schema introspection.
"""

import os
import re
import logging
import decimal
import datetime
import psycopg2
import psycopg2.extras
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

_SMALL_MODEL  = "gemini-3.5-flash-lite"
_GEMINI_URL   = "https://generativelanguage.googleapis.com/v1beta/openai/"
_FORBIDDEN_KW = re.compile(
    r'\b(INSERT|UPDATE|DELETE|DROP|ALTER|TRUNCATE|GRANT|REVOKE|CREATE|EXEC)\b',
    re.IGNORECASE,
)


def _llm_client() -> OpenAI:
    return OpenAI(
        base_url=_GEMINI_URL,
        api_key=os.getenv("GEMINI_API_KEY", ""),
        timeout=30.0,
    )


def _db_connection():
    return psycopg2.connect(
        host=os.getenv("DB_HOST", "localhost"),
        port=int(os.getenv("DB_PORT", 5432)),
        dbname=os.getenv("DB_NAME", "postgres"),
        user=os.getenv("DB_USER", "postgres"),
        password=os.getenv("DB_PASSWORD", ""),
    )


def _fetch_schemas(conn, tables: list) -> str:
    """Return a human-readable column summary for the allowed tables."""
    if not tables:
        return ""
    placeholders = ",".join(["%s"] * len(tables))
    sql = f"""
        SELECT table_name, column_name, data_type
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name IN ({placeholders})
        ORDER BY table_name, ordinal_position
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, tables)
        rows = cur.fetchall()

    schema_text = ""
    current_table = None
    for row in rows:
        if row["table_name"] != current_table:
            current_table = row["table_name"]
            schema_text += f"\nTable: {current_table}\n"
        schema_text += f"  {row['column_name']} ({row['data_type']})\n"
    return schema_text.strip()


def _generate_sql(question: str, schema_text: str, tables: list, doctor_name: str) -> str:
    """Call small LLM to generate a SELECT query for the given question."""
    prompt = (
        f"You are a SQL assistant for a hospital database.\n"
        f"Generate a single read-only SELECT query to answer the question for doctor: {doctor_name}.\n\n"
        f"Rules:\n"
        f"- Only query these tables: {', '.join(tables)}\n"
        f"- Always filter by doctor_id = %(doctor_id)s (use this exact placeholder)\n"
        f"- Return only a SELECT statement — no INSERT, UPDATE, DELETE, DROP, or DDL\n"
        f"- Use standard PostgreSQL syntax\n"
        f"- Return only the SQL, no explanation, no markdown fences\n\n"
        f"Table schemas:\n{schema_text}\n\n"
        f"Question: {question}"
    )
    client = _llm_client()
    response = client.chat.completions.create(
        model=_SMALL_MODEL,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.0,
        max_tokens=512,
    )
    sql = response.choices[0].message.content or ""
    # Strip markdown fences if the model wraps the SQL
    sql = re.sub(r"^```[a-z]*\n?", "", sql.strip(), flags=re.IGNORECASE)
    sql = re.sub(r"\n?```$", "", sql.strip())
    return sql.strip()


def _validate_sql(sql: str, schema_text: str) -> str:
    """
    Second LLM call: verify all tables and columns in the SQL exist in the schema.
    Returns the original SQL if valid, or a corrected SQL if not.
    """
    prompt = (
        "You are a SQL validator for a hospital database.\n"
        "Given the table schemas below and a SQL query, check whether every table "
        "and every column referenced in the query actually exists in the schema.\n\n"
        "If the SQL is fully valid (all tables and columns exist), return it unchanged.\n"
        "If the SQL references any non-existent table or column, rewrite it using only "
        "the tables and columns that are listed in the schema.\n\n"
        "Rules:\n"
        "- Keep the WHERE doctor_id = %%(doctor_id)s filter exactly as-is\n"
        "- Return only the SQL query, no explanation, no markdown fences\n\n"
        f"Schema:\n{schema_text}\n\n"
        f"SQL:\n{sql}"
    )
    client = _llm_client()
    response = client.chat.completions.create(
        model=_SMALL_MODEL,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.0,
        max_tokens=512,
    )
    validated = response.choices[0].message.content or sql
    validated = re.sub(r"^```[a-z]*\n?", "", validated.strip(), flags=re.IGNORECASE)
    validated = re.sub(r"\n?```$", "", validated.strip())
    return validated.strip()


def _is_safe(sql: str) -> bool:
    """Return True only if the SQL is a plain SELECT with no forbidden keywords."""
    stripped = sql.strip().lstrip("-– \t\n")
    if not stripped.upper().startswith("SELECT"):
        return False
    if _FORBIDDEN_KW.search(sql):
        return False
    return True


def _serialize_row(row: dict) -> dict:
    """Convert Postgres types that json.dumps can't handle (datetime, Decimal, etc.)."""
    result = {}
    for k, v in row.items():
        if isinstance(v, (datetime.datetime, datetime.date, datetime.time)):
            result[k] = v.isoformat()
        elif isinstance(v, decimal.Decimal):
            result[k] = float(v)
        else:
            result[k] = v
    return result


def run_query(question: str, doctor_phone: str, repository) -> dict:
    """
    Translate a natural-language question into a scoped SQL query and execute it.

    Returns:
        {"rows": list[dict], "columns": list[str], "sql": str}  on success
        {"error": str}                                            on failure
    """
    # 1. Look up doctor config
    doctor_config = repository.get_doctor_config(doctor_phone)
    if not doctor_config:
        return {"error": "Doctor profile not found"}

    doctor_id   = doctor_config["doctor_id"]
    doctor_name = doctor_config.get("name", "Unknown")
    tables      = doctor_config.get("tables", [])

    # 2. Connect to DB
    try:
        conn = _db_connection()
    except Exception as exc:
        logger.error("DB connection failed: %s", exc)
        return {"error": "Database temporarily unavailable"}

    try:
        # 3. Fetch live schemas from information_schema
        schema_text = _fetch_schemas(conn, tables)

        # 4. Generate SQL via small LLM
        try:
            sql = _generate_sql(question, schema_text, tables, doctor_name)
        except Exception as exc:
            logger.error("LLM SQL generation failed: %s", exc)
            return {"error": "Could not generate query"}

        # 4b. Validate + fix hallucinated columns/tables via second LLM call
        try:
            sql = _validate_sql(sql, schema_text)
        except Exception as exc:
            logger.warning("SQL validation LLM failed, proceeding with original: %s", exc)

        # 5. Safety check — SELECT only, no forbidden keywords
        if not _is_safe(sql):
            logger.warning("Unsafe SQL rejected: %s", sql)
            return {"error": "Only SELECT queries are permitted"}

        # 6. Execute with doctor_id scoping via bind variable
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(sql, {"doctor_id": doctor_id})
                raw_rows = cur.fetchall()
        except Exception as exc:
            logger.error("Query execution failed: %s", exc)
            return {"error": f"Query failed: {exc}"}

        rows    = [_serialize_row(dict(r)) for r in raw_rows]
        columns = list(rows[0].keys()) if rows else []
        return {"rows": rows, "columns": columns, "sql": sql}

    finally:
        conn.close()
