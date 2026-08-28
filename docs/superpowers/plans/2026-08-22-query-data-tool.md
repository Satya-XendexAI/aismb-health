# query_data Tool Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a doctor-only `query_data` tool that translates natural-language questions into scoped PostgreSQL SELECT queries via a small LLM.

**Architecture:** `InMemoryRepository` carries rich doctor config (from `config/doctors.json`). The orchestrator injects `doctor_phone` + `repository` into `tools/query_data.run_query()`, which fetches schemas from `information_schema`, generates SQL via `gemini-3.5-flash-lite`, validates SELECT-only, executes scoped to `doctor_id`, and returns `{rows, columns, sql}`.

**Tech Stack:** Python 3.11, psycopg2-binary, openai (Gemini-compatible), python-dotenv

## Global Constraints

- All DB queries scoped to `doctor_id` from config — never query across doctors
- Only SELECT statements permitted — forbidden keywords: INSERT, UPDATE, DELETE, DROP, ALTER, TRUNCATE, GRANT, REVOKE, CREATE, EXEC
- Allowed tables per doctor come from `config/doctors.json["tables"]` — no other table may appear in the SQL prompt
- Model for SQL generation: `gemini-3.5-flash-lite`
- Credentials from `.env` at project root: `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, `DB_PASSWORD`

---

### Task 1: Update `config/doctors.json` + RBAC loading

**Files:**
- Modify: `config/doctors.json`
- Modify: `orchestrator.py:190-204` (InMemoryRepository)
- Modify: `run_manual_test.py:34-36` (DOCTORS loading)

**Interfaces:**
- Produces: `InMemoryRepository.get_doctor_config(phone: str) -> dict | None` returning `{doctor_id, phone, name, tables}`
- Produces: `InMemoryRepository.get_role(phone: str) -> Role` (updated to work with list of dicts)

- [ ] **Step 1: Update `config/doctors.json`**

Replace the flat phone array with a list of rich doctor objects:

```json
{
  "doctors": [
    {
      "doctor_id": "dr-karthick-anjaneyan-j-chn",
      "phone": "+91-9940142234",
      "name": "Dr. Karthick Anjaneyan J",
      "tables": ["patients", "tokens"]
    }
  ]
}
```

- [ ] **Step 2: Update `InMemoryRepository` in `orchestrator.py`**

Replace the existing `InMemoryRepository` class (lines ~190-204) with:

```python
class InMemoryRepository:
    """Stores sessions in memory. doctors is a list of doctor config dicts."""

    def __init__(self, doctors: list = None):
        self._sessions: dict = {}
        self._doctors: list  = doctors or []

    def get_session(self, hospital_id: str, from_number: str) -> Optional[Session]:
        return self._sessions.get(f"{hospital_id}:{from_number}")

    def save_session(self, session: Session):
        self._sessions[f"{session.hospital_id}:{session.from_number}"] = session

    def get_role(self, from_number: str) -> Role:
        return Role.DOCTOR if any(d["phone"] == from_number for d in self._doctors) else Role.PATIENT

    def get_doctor_config(self, from_number: str) -> dict | None:
        return next((d for d in self._doctors if d["phone"] == from_number), None)
```

- [ ] **Step 3: Update `run_manual_test.py` loader**

Change the doctors loading block (around line 34-36):
```python
# before
with open("config/doctors.json") as f:
    DOCTORS = set(json.load(f)["doctors"])

# after
with open("config/doctors.json") as f:
    DOCTORS = json.load(f)["doctors"]   # list of dicts
```

Also update the `print_banner` and `switch` command doctor check:
```python
# before
print(f"  Using: {from_number}  (doctor={from_number in DOCTORS})\n")
# after
print(f"  Using: {from_number}  (doctor={any(d['phone'] == from_number for d in DOCTORS)})\n")
```

Same for the switch block output line.

- [ ] **Step 4: Smoke-test manually**

```bash
source venv/bin/activate
python run_manual_test.py
```

Enter `+91-9940142234` as the number, type `status` — confirm role shows `doctor`. Enter a different number, confirm role shows `patient`.

- [ ] **Step 5: Commit**

```bash
git add config/doctors.json orchestrator.py run_manual_test.py
git commit -m "feat(health): update doctors.json to rich objects, add get_doctor_config"
```

---

### Task 2: Update `_query_data_schema` in `orchestrator.py`

**Files:**
- Modify: `orchestrator.py:143-160` (_query_data_schema)
- Modify: `orchestrator.py:449-453` (_execute_tool query_data branch)

**Interfaces:**
- Consumes: `tools.query_data.run_query(question: str, doctor_phone: str, repository) -> dict`
- Produces: updated schema with `question: str` replacing `query_type` enum + `filters`

- [ ] **Step 1: Replace `_query_data_schema`**

```python
_query_data_schema = {
    "type": "function",
    "function": {
        "name": "query_data",
        "description": "Query your patients' appointment and token data using a natural language question.",
        "parameters": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "Natural language question about your patients or tokens",
                },
            },
            "required": ["question"],
        },
    },
}
```

- [ ] **Step 2: Update `_execute_tool` query_data branch**

Replace the mock branch:
```python
# before
elif tool_call.tool_name == "query_data":
    return {"data": "Patient records query result (dummy)"}

# after
elif tool_call.tool_name == "query_data":
    from tools.query_data import run_query
    return run_query(
        question=tool_call.args["question"],
        doctor_phone=context.wa_message.from_number,
        repository=self.repository,
    )
```

- [ ] **Step 3: Commit**

```bash
git add orchestrator.py
git commit -m "feat(health): wire query_data schema and _execute_tool to real tool"
```

---

### Task 3: Implement `tools/query_data.py`

**Files:**
- Create: `tools/query_data.py`

**Interfaces:**
- Consumes: `repository.get_doctor_config(phone) -> dict | None`
- Consumes: DB env vars: `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, `DB_PASSWORD`
- Produces: `run_query(question: str, doctor_phone: str, repository) -> dict`
  - Success: `{"rows": list[dict], "columns": list[str], "sql": str}`
  - Error: `{"error": str}`

- [ ] **Step 1: Create `tools/query_data.py`**

```python
"""
query_data.py — Text-to-SQL tool for doctors.

Translates a natural-language question into a scoped SELECT query
against the hospital PostgreSQL DB, using gemini-3.5-flash-lite for
SQL generation and information_schema for live schema introspection.
"""

import os
import re
import logging
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


def _fetch_schemas(conn, tables: list[str]) -> str:
    """Return a human-readable DDL summary for allowed tables only."""
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


def _generate_sql(question: str, schema_text: str, tables: list[str], doctor_name: str) -> str:
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
    # Strip markdown fences if model adds them
    sql = re.sub(r"^```[a-z]*\n?", "", sql.strip(), flags=re.IGNORECASE)
    sql = re.sub(r"\n?```$", "", sql.strip())
    return sql.strip()


def _is_safe(sql: str) -> bool:
    stripped = sql.strip().lstrip("-– \t\n")
    if not stripped.upper().startswith("SELECT"):
        return False
    if _FORBIDDEN_KW.search(sql):
        return False
    return True


def run_query(question: str, doctor_phone: str, repository) -> dict:
    """
    Translates a natural-language question into a scoped SQL query and executes it.
    Returns {"rows": [...], "columns": [...], "sql": "..."} or {"error": "..."}.
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
        # 3. Fetch schemas
        schema_text = _fetch_schemas(conn, tables)

        # 4. Generate SQL
        try:
            sql = _generate_sql(question, schema_text, tables, doctor_name)
        except Exception as exc:
            logger.error("LLM SQL generation failed: %s", exc)
            return {"error": "Could not generate query"}

        # 5. Safety check
        if not _is_safe(sql):
            logger.warning("Unsafe SQL rejected: %s", sql)
            return {"error": "Only SELECT queries are permitted"}

        # 6. Execute with doctor_id scoping
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(sql, {"doctor_id": doctor_id})
                raw_rows = cur.fetchall()
        except Exception as exc:
            logger.error("Query execution failed: %s", exc)
            return {"error": f"Query failed: {exc}"}

        rows    = [dict(r) for r in raw_rows]
        columns = list(rows[0].keys()) if rows else []
        return {"rows": rows, "columns": columns, "sql": sql}

    finally:
        conn.close()
```

- [ ] **Step 2: Manual smoke-test as a doctor**

```bash
source venv/bin/activate
python run_manual_test.py
```

1. Enter `+91-9940142234` as the phone number
2. Ask: `how many patients do I have today?`
3. Confirm the bot returns real data (not the dummy string)
4. Ask: `show me all waiting tokens`
5. Confirm rows are returned and scoped to this doctor

- [ ] **Step 3: Commit**

```bash
git add tools/query_data.py
git commit -m "feat(health): implement query_data text-to-SQL tool for doctors"
```
