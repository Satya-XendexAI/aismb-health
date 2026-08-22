# query_data Tool — Design Spec
**Date:** 2026-08-22
**Status:** Approved

---

## Overview

`query_data` is a doctor-only tool that lets medical staff ask natural-language questions about their own patients' appointment and token data. A small LLM (`gemini-3.5-flash-lite`) translates the question into a safe, scoped SQL query against the existing PostgreSQL database, executes it, and returns structured results to the orchestrator LLM for formatting.

---

## Architecture

```
Doctor WhatsApp message
        │
        ▼
WhatsAppOrchestrator (_execute_tool)
        │  injects doctor_phone + repository
        ▼
tools/query_data.py :: run_query()
        │
        ├─ 1. Lookup doctor config from repository (phone → doctor_id, tables)
        │
        ├─ 2. Connect to PostgreSQL (.env credentials)
        │
        ├─ 3. Fetch column schemas from information_schema for allowed tables
        │
        ├─ 4. Build prompt: schemas + scope rule + question
        │
        ├─ 5. gemini-3.5-flash-lite → generates SELECT SQL
        │
        ├─ 6. Safety check: SELECT-only validation
        │
        ├─ 7. Execute query with doctor_id as bind variable
        │
        └─ 8. Return {rows, columns, sql} or {error: ...}
```

---

## config/doctors.json (updated format)

The existing `config/doctors.json` is updated from a flat phone array to a list of rich doctor objects. The `query_data` tool reads allowed tables from here; the RBAC system reads phone numbers from here.

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

**Fields:**
- `doctor_id` — matches the `doctor_id` column in PostgreSQL (used to scope queries)
- `phone` — WhatsApp number used for role detection (RBAC)
- `name` — display name (may be included in LLM prompt for context)
- `tables` — explicit allowlist of tables this doctor can query; no table outside this list appears in the SQL prompt

---

## Database Access

Credentials are read from `.env` (already present for `appoint_tool`):
```
DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD
```

Table schemas are fetched at query time from `information_schema.columns`, filtered to `allowed_tables`. This means no schema file needs to be maintained — the real DB is the source of truth.

**Scoping rule:** Every generated query must include a `WHERE` clause that filters by `doctor_id = %(doctor_id)s`. The prompt instructs the LLM to enforce this; the bind variable is always injected at execution time regardless of what SQL the LLM produces.

---

## SQL Generation (Small LLM)

**Model:** `gemini-3.5-flash-lite` (same model used by `kg_retriever` parse step)

**Prompt structure:**
```
You are a SQL assistant for a hospital database.
Generate a single read-only SELECT query to answer the question.
Rules:
- Only query these tables: {table list}
- Always filter by doctor_id = %(doctor_id)s
- Return only SELECT statements — no INSERT, UPDATE, DELETE, DROP, or DDL
- Use standard PostgreSQL syntax

Table schemas:
{column names and types for each allowed table}

Question: {doctor's natural language question}

Return only the SQL query, nothing else.
```

---

## Safety Check

Before execution, `run_query` validates:
1. The SQL string starts with `SELECT` (case-insensitive, after stripping whitespace/comments)
2. No forbidden keywords anywhere in the statement: `INSERT`, `UPDATE`, `DELETE`, `DROP`, `ALTER`, `TRUNCATE`, `GRANT`, `REVOKE`, `CREATE`, `EXEC`

If validation fails, return `{"error": "Only SELECT queries are permitted"}` without executing.

---

## Orchestrator Changes

### `_query_data_schema` (updated)

Drops the old `query_type` enum and `filters` object. Replaces with a single natural-language field:

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

### `_execute_tool` (updated)

```python
elif tool_call.tool_name == "query_data":
    from tools.query_data import run_query
    return run_query(
        question=tool_call.args["question"],
        doctor_phone=context.wa_message.from_number,
        repository=self.repository,
    )
```

### `InMemoryRepository` (updated)

```python
def __init__(self, doctors: list = None):
    self._sessions: dict = {}
    self._doctors: list  = doctors or []

def get_role(self, from_number: str) -> Role:
    return Role.DOCTOR if any(d["phone"] == from_number for d in self._doctors) else Role.PATIENT

def get_doctor_config(self, from_number: str) -> dict | None:
    return next((d for d in self._doctors if d["phone"] == from_number), None)
```

### `run_manual_test.py` (updated)

```python
with open("config/doctors.json") as f:
    DOCTORS = json.load(f)["doctors"]   # list of dicts

repository = InMemoryRepository(doctors=DOCTORS)
```

Banner doctor check: `any(d["phone"] == from_number for d in DOCTORS)`

---

## `tools/query_data.py` — Full Flow

```
run_query(question, doctor_phone, repository)
    │
    ├─ doctor_config = repository.get_doctor_config(doctor_phone)
    │   └─ None → return {"error": "Doctor profile not found"}
    │
    ├─ connect to PostgreSQL via DB_* env vars
    │   └─ failure → return {"error": "Database temporarily unavailable"}
    │
    ├─ fetch column schemas from information_schema
    │   for each table in doctor_config["tables"]
    │
    ├─ call gemini-3.5-flash-lite with schemas + question
    │   └─ failure → return {"error": "Could not generate query"}
    │
    ├─ safety check (SELECT-only)
    │   └─ fail → return {"error": "Only SELECT queries are permitted"}
    │
    ├─ execute: cur.execute(sql, {"doctor_id": doctor_config["doctor_id"]})
    │   └─ pg error → return {"error": "Query failed: <message>"}
    │
    └─ return {"rows": [...], "columns": [...], "sql": "<generated sql>"}
```

---

## Error Handling

| Failure point | Returned dict |
|---|---|
| Doctor not found in config | `{"error": "Doctor profile not found"}` |
| DB connection failure | `{"error": "Database temporarily unavailable"}` |
| LLM call failure | `{"error": "Could not generate query"}` |
| Non-SELECT SQL generated | `{"error": "Only SELECT queries are permitted"}` |
| Query execution error | `{"error": "Query failed: <pg error>"}` |
| 0 rows returned | `{"rows": [], "columns": [...], "sql": "..."}` |

All errors are returned as dicts to the orchestrator LLM, which formats the human-readable WhatsApp reply.

---

## Files Changed

| File | Change |
|---|---|
| `config/doctors.json` | Phone array → list of rich doctor objects |
| `tools/query_data.py` | New file implementing `run_query()` |
| `orchestrator.py` | Update `_query_data_schema`, `_execute_tool`, `InMemoryRepository` |
| `run_manual_test.py` | Load `list[dict]` instead of `set` |
