# Project Restructure — Design Spec
**Date:** 2026-08-22
**Status:** Approved

---

## Goal

Split the 518-line `orchestrator.py` monolith and the standalone `appoint_tool/` package into a clean, modular layout. Every module has one responsibility, a clear public interface, and no path hacks.

---

## Final Directory Layout

```
ai_smb_health/
├── run_manual_test.py          # unchanged interface
├── requirements.txt
├── .env
├── config/
│   └── doctors.json
│
├── models/                     # shared data contracts — no logic
│   ├── __init__.py
│   ├── session.py              # WAMessage, Session, ChatTurn, ToolCall, AgentResponse,
│   │                           # Role, SessionState, ChatRole, GateStatus, GateResult,
│   │                           # AgentResponseType, OrchestratorContext  (dataclasses + enums)
│   └── appointment.py          # IncomingPayload, BookingConfirmation, CancellationResult,
│                               # ErrorResult, BookingResponse  (Pydantic models)
│
├── prompts/                    # all LLM system prompt strings
│   ├── __init__.py
│   └── system.py               # PATIENT_SYSTEM_PROMPT, DOCTOR_SYSTEM_PROMPT
│
├── orchestrator/               # WhatsApp orchestration layer
│   ├── __init__.py             # re-exports WhatsAppOrchestrator, InMemoryRepository,
│   │                           # WAMessage, GeminiLLMAdapter, PrintWANotifier, SessionState
│   ├── core.py                 # WhatsAppOrchestrator: handle_message + ReAct loop
│   ├── session.py              # InMemoryRepository
│   ├── llm.py                  # GeminiLLMAdapter + _build_messages + PrintWANotifier
│   └── schemas.py              # tool JSON schema dicts, PATIENT_TOOLS, DOCTOR_TOOLS,
│                               # ROLE_PERMISSIONS
│
└── tools/                      # all tool implementations
    ├── __init__.py
    ├── appointment/            # appoint_tool/ renamed + moved
    │   ├── __init__.py         # exposes handle_request(payload_dict) -> dict
    │   ├── booking.py          # book(), cancel(), calculate_eta()
    │   └── database.py         # all Postgres operations
    ├── kg_retriever.py         # unchanged
    └── query_data.py           # unchanged
```

---

## What Is Deleted

| Path | Replacement |
|---|---|
| `orchestrator.py` | `orchestrator/` package |
| `appoint_tool/` directory | `tools/appointment/` |
| `appoint_tool/config.py` | Deleted — `database.py` reads `os.getenv()` directly |
| `appoint_tool/app.py` | `tools/appointment/__init__.py` |
| `appoint_tool/models.py` | `models/appointment.py` |
| All `sys.path.insert` hacks | Removed — standard package imports work throughout |

---

## Module Responsibilities

### `models/session.py`
Pure dataclasses and enums. No imports from this project. Zero logic.

Exports:
- Enums: `SessionState`, `Role`, `ChatRole`, `AgentResponseType`, `GateStatus`
- Dataclasses: `ToolCall`, `ChatTurn`, `WAMessage`, `Session`, `AgentResponse`, `GateResult`, `OrchestratorContext`

### `models/appointment.py`
Pydantic models only. Input/output contracts for the appointment tool.

Exports: `IncomingPayload`, `BookingConfirmation`, `CancellationResult`, `ErrorResult`, `BookingResponse`

### `prompts/system.py`
String constants only. Only imported by `orchestrator/llm.py`.

Exports: `PATIENT_SYSTEM_PROMPT`, `DOCTOR_SYSTEM_PROMPT`

### `orchestrator/schemas.py`
Tool JSON schema dicts and role-to-tools mappings. No logic.

Exports: `_appointment_schema`, `_kg_retriever_schema`, `_query_data_schema`, `PATIENT_TOOLS`, `DOCTOR_TOOLS`, `ROLE_PERMISSIONS`

### `orchestrator/session.py`
`InMemoryRepository` only. Depends on `models/session.py`. Swappable for a Redis or DB-backed repo without touching anything else.

Exports: `InMemoryRepository`

### `orchestrator/llm.py`
`GeminiLLMAdapter` and `PrintWANotifier`. Knows how to call Gemini, build message history, and replay `thought_signature`. Depends on `models/session.py`, `prompts/system.py`, `orchestrator/schemas.py`.

Exports: `GeminiLLMAdapter`, `PrintWANotifier`

### `orchestrator/core.py`
`WhatsAppOrchestrator` only. The ReAct loop, gate, tool dispatch, confirm/interrupt flow. Imports from all other `orchestrator/` modules and calls into `tools/`.

Exports: `WhatsAppOrchestrator`

### `orchestrator/__init__.py`
Re-exports everything `run_manual_test.py` imports so that file needs zero changes:
```python
from orchestrator.core    import WhatsAppOrchestrator
from orchestrator.session import InMemoryRepository
from orchestrator.llm     import GeminiLLMAdapter, PrintWANotifier
from models.session       import WAMessage, SessionState
```

### `tools/appointment/__init__.py`
Single public function. Orchestrator only ever calls this; internals are hidden.

Exports: `handle_request(payload_dict: dict) -> dict`

### `tools/appointment/booking.py`
`book()`, `cancel()`, `calculate_eta()` — unchanged logic, just moved.

### `tools/appointment/database.py`
All Postgres operations — unchanged logic. Drops the `config.py` import; reads DB credentials via `os.getenv()` directly (root `.env` is loaded at startup by `load_dotenv()` in the orchestrator).

---

## Dependency Graph

```
run_manual_test.py
    └── orchestrator/  (via __init__.py)
            ├── core.py
            │     ├── models/session.py
            │     ├── orchestrator/schemas.py
            │     ├── orchestrator/session.py
            │     ├── orchestrator/llm.py
            │     └── tools/
            │           ├── appointment/   handle_request()
            │           ├── kg_retriever   retrieve_context()
            │           └── query_data     run_query()
            ├── llm.py
            │     ├── models/session.py
            │     ├── prompts/system.py
            │     └── orchestrator/schemas.py
            └── session.py
                  └── models/session.py

tools/appointment/
    ├── __init__.py  →  booking.py + database.py
    ├── booking.py   →  database.py + models/appointment.py
    └── database.py  →  os.getenv() (.env)

tools/kg_retriever.py  →  os.getenv() (.env, NEO4J_*, GEMINI_*)
tools/query_data.py    →  os.getenv() (.env, DB_*, GEMINI_*) + repository
```

No circular imports. `models/` and `prompts/` are leaves — they import nothing from this project.

---

## Migration Notes

- `run_manual_test.py` import line stays identical: `from orchestrator import WhatsAppOrchestrator, WAMessage, ...`
- All tool call sites in `orchestrator/core.py` remain the same — only the `from` path changes
- `tools/appointment/database.py`: remove `from config import DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD` and replace with `os.getenv()` calls; add `load_dotenv()` at top
- No changes to `tools/kg_retriever.py` or `tools/query_data.py` internals

---

## Files Changed Summary

| Action | Path |
|---|---|
| Create | `models/__init__.py` |
| Create | `models/session.py` |
| Create | `models/appointment.py` |
| Create | `prompts/__init__.py` |
| Create | `prompts/system.py` |
| Create | `orchestrator/__init__.py` |
| Create | `orchestrator/core.py` |
| Create | `orchestrator/session.py` |
| Create | `orchestrator/llm.py` |
| Create | `orchestrator/schemas.py` |
| Create | `tools/appointment/__init__.py` |
| Move+edit | `appoint_tool/booking.py` → `tools/appointment/booking.py` |
| Move+edit | `appoint_tool/database.py` → `tools/appointment/database.py` |
| Delete | `orchestrator.py` |
| Delete | `appoint_tool/` (entire directory) |
