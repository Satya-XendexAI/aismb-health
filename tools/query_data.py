"""
query_data.py — Text-to-SQL tool for doctors and admins.

One unified pipeline. Callers pass the table list, bind param name, and bind
value — the SQL generation and validation prompts adapt accordingly.

Doctor : tables from config, filtered by doctor_id
Admin  : all 4 tables (doctors, doctor_sessions, patients, tokens), filtered by
         hospital_id, defaults to CURRENT_DATE when no date is specified
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
from prompts.query_data import sql_generate, sql_validate, ADMIN_TABLES

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


def _is_safe(sql: str) -> bool:
    stripped = sql.strip().lstrip("-– \t\n")
    if not stripped.upper().startswith("SELECT"):
        return False
    if _FORBIDDEN_KW.search(sql):
        return False
    return True


def _serialize_row(row: dict) -> dict:
    result = {}
    for k, v in row.items():
        if isinstance(v, (datetime.datetime, datetime.date, datetime.time)):
            result[k] = v.isoformat()
        elif isinstance(v, decimal.Decimal):
            result[k] = float(v)
        else:
            result[k] = v
    return result


def _run(question: str, tables: list, bind_param: str, bind_value: str,
         extra_rules: list[str] | None = None) -> dict:
    """
    Core pipeline shared by doctor and admin paths.

    tables      — which tables the LLM may query
    bind_param  — name of the filter column used as %(bind_param)s in SQL
    bind_value  — the actual value substituted at execution time
    extra_rules — additional prompt rules injected into sql_generate
    """
    try:
        conn = _db_connection()
    except Exception as exc:
        logger.error("DB connection failed: %s", exc)
        return {"error": "Database temporarily unavailable"}

    try:
        schema_text = _fetch_schemas(conn, tables)

        try:
            prompt = sql_generate(question, schema_text, tables, bind_param, extra_rules)
            client = _llm_client()
            resp   = client.chat.completions.create(
                model=_SMALL_MODEL,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.0,
                max_tokens=512,
            )
            sql = resp.choices[0].message.content or ""
            sql = re.sub(r"^```[a-z]*\n?", "", sql.strip(), flags=re.IGNORECASE)
            sql = re.sub(r"\n?```$", "", sql.strip())
            sql = sql.strip()
        except Exception as exc:
            logger.error("LLM SQL generation failed: %s", exc)
            return {"error": "Could not generate query"}

        try:
            v_prompt = sql_validate(sql, schema_text, bind_param)
            v_resp   = client.chat.completions.create(
                model=_SMALL_MODEL,
                messages=[{"role": "user", "content": v_prompt}],
                temperature=0.0,
                max_tokens=512,
            )
            validated = v_resp.choices[0].message.content or sql
            validated = re.sub(r"^```[a-z]*\n?", "", validated.strip(), flags=re.IGNORECASE)
            validated = re.sub(r"\n?```$", "", validated.strip())
            sql = validated.strip()
        except Exception as exc:
            logger.warning("SQL validation LLM failed, proceeding with original: %s", exc)

        if not _is_safe(sql):
            logger.warning("Unsafe SQL rejected: %s", sql)
            return {"error": "Only SELECT queries are permitted"}

        try:
            # Replace every named placeholder with positional to avoid mixing formats
            placeholder = f"%({bind_param})s"
            count    = sql.count(placeholder)
            sql_exec = sql.replace(placeholder, "%s")
            params   = (bind_value,) * count   # one value per occurrence
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(sql_exec, params)
                raw_rows = cur.fetchall()
        except Exception as exc:
            logger.error("Query execution failed: %s", exc)
            return {"error": f"Query failed: {exc}"}

        rows    = [_serialize_row(dict(r)) for r in raw_rows]
        columns = list(rows[0].keys()) if rows else []
        return {"rows": rows, "columns": columns, "sql": sql}

    finally:
        conn.close()


def run_query(question: str, doctor_phone: str, repository) -> dict:
    doctor_config = repository.get_doctor_config(doctor_phone)
    if not doctor_config:
        return {"error": "Doctor profile not found"}
    return _run(
        question=question,
        tables=doctor_config.get("tables", []),
        bind_param="doctor_id",
        bind_value=doctor_config["doctor_id"],
    )


def run_admin_query(question: str, hospital_id: str) -> dict:
    return _run(
        question=question,
        tables=ADMIN_TABLES,
        bind_param="hospital_id",
        bind_value=hospital_id,
        extra_rules=["- If no date is specified in the question, default to CURRENT_DATE"],
    )
