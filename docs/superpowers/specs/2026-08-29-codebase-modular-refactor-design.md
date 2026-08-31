# Codebase Modular Refactor — Design Spec

**Date:** 2026-08-29
**Branch:** skill
**Goal:** Remove dead code, split oversized files into focused modules, wire up clean package APIs — without changing any runtime behaviour.

---

## Scope

Two primary targets:

1. `orchestrator/core.py` (597 lines) — mixes loop control, gate logic, formatting, and session management in one class
2. `tools/kg_retriever.py` (501 lines) — mixes Neo4j client setup, graph queries, embeddings, query parsing, and retrieval logic

Everything else (schemas, models, prompts, session, llm, tools/appointment) stays untouched.

---

## Section 1 — Orchestrator Split

### Current structure

```
orchestrator/core.py        597 lines
orchestrator/schemas.py     236 lines
orchestrator/llm.py         120 lines
orchestrator/session.py      30 lines
orchestrator/__init__.py     13 lines
```

### Target structure

```
orchestrator/core.py        ~200 lines   WhatsAppOrchestrator (thin coordinator)
orchestrator/gates.py       ~170 lines   Confirmation-gate state machine
orchestrator/formatters.py  ~120 lines   Pure response-formatting functions
orchestrator/utils.py        ~15 lines   Intent/affirmative/negative helpers
orchestrator/schemas.py     unchanged
orchestrator/llm.py         unchanged
orchestrator/session.py     unchanged
orchestrator/__init__.py    unchanged (public surface unchanged)
```

### `orchestrator/utils.py`

Three pure functions extracted from the top of `core.py`:

- `detect_booking_intent(text: str) -> bool`
- `is_affirmative(text: str) -> bool`
- `is_negative(text: str) -> bool`

Constants (`_BOOKING_KEYWORDS`, `_AFFIRMATIVE`, `_NEGATIVE`) live here too.

### `orchestrator/formatters.py`

All static/pure formatting logic. No state, no I/O:

- `format_booking_result(result, tool_args) -> str | None`
- `format_plan_summary(plan, summary_line) -> str`
- `format_delay_preview(preview) -> str`
- `describe_tool(tool_call) -> str`
- `doctor_display_name(args) -> str`
- `chunk_text(text, max_chars) -> list[str]`

All currently live as `@staticmethod` on `WhatsAppOrchestrator`. Extracted as module-level functions; the orchestrator calls them directly via import.

### `orchestrator/gates.py`

All AWAITING_CONFIRM state-machine logic and its helpers:

- `handle_awaiting_confirm(wa_message, session, context, repository, notifier, executor) -> bool`
  Returns `True` if the message was consumed by the gate (caller should return early).
- `interrupt_tool(tool_call, context, repository, notifier)`
- `interrupt_plan(plan_args, context, repository, notifier)`
- `interrupt_delay(args, context, doc_config, repository, notifier)`
- `build_pending_plan(plan_args) -> list[PlanAction]`
- `execute_approved_plan(plan, context, notifier)`

`executor` passed into `handle_awaiting_confirm` is a callable `(tool_call, context) -> dict` — the orchestrator's `_execute_tool` — so `gates.py` has no import of `core.py` (no circular dependency).

### `orchestrator/core.py` (after)

`WhatsAppOrchestrator` retains only:

- `__init__`
- `handle_message` (public entry, try/except wrapper)
- `_handle_message_inner` (system-prompt construction + ReAct loop)
- `_hydrate`
- `_preload_memory`
- `_execute_tool` (tool dispatch, ~55 lines — kept here as it needs `self.repository`/`self.notifier`)
- `_llm_call_with_retry`
- `_responder`
- `_log_tool`
- `_gate`

Delegates to `gates`, `formatters`, `utils` via imports at the top of the file.

---

## Section 2 — KG Retriever Split

### Current structure

```
tools/kg_retriever.py    501 lines   Everything in one file
```

### Target structure

```
tools/kg/
  __init__.py      Re-exports retrieve_context
  client.py        Neo4j driver + Gemini client singletons
  queries.py       Graph query functions + embed + semantic_search
  resolver.py      _parse_query, _resolve_specializations, _specialization_names
  context.py       _build_context, language helpers, _fuse_results, retrieve_context

tools/kg_retriever.py   Thin shim: `from tools.kg import retrieve_context`
```

#### `tools/kg/client.py`

Owns all external-client setup:
- `_make_driver()` and module-level `_driver`
- `_gemini_client`, `_embed_client`
- Environment variable reads (`NEO4J_*`, `GEMINI_*`, `TENANT_ID`, `HOSPITAL_NAME`)

#### `tools/kg/queries.py`

Pure graph I/O — imports `_driver` from `client.py`:
- `find_doctors_by_specialization(spec, limit, tenant_id)`
- `find_doctors_by_language(lang, limit, tenant_id)`
- `find_by_fulltext(keyword, limit, tenant_id)`
- `_sanitize_lucene(keyword)`
- `embed(text) -> np.ndarray`
- `semantic_search(query, n_results, tenant_id)`
- `next_available_slots(doctor_ids)`

#### `tools/kg/resolver.py`

Query parsing and specialization resolution — imports `_gemini_client` from `client.py` and `_driver` from `client.py`:
- `_specialization_names() -> list[str]`
- `_resolve_specializations(specs) -> list[str]`
- `_parse_query(query) -> dict`

#### `tools/kg/context.py`

Retrieval orchestration — imports from `queries.py` and `resolver.py`:
- `_fuse_results(vector_results, graph_results, n)`
- `_build_context(fused) -> str | dict`
- `_no_language_response(language, specs) -> dict`
- `_no_language_for_specialty_response(language, specs, specialty_results) -> dict`
- `retrieve_context(query) -> dict`  ← public API

#### `tools/kg/__init__.py`

```python
from tools.kg.context import retrieve_context
__all__ = ["retrieve_context"]
```

#### `tools/kg_retriever.py` (shim)

```python
from tools.kg import retrieve_context
__all__ = ["retrieve_context"]
```

Preserves all existing `from tools.kg_retriever import retrieve_context` call sites with zero changes.

---

## Section 3 — __init__ Files

`orchestrator/__init__.py` already exports the public API correctly. No changes needed.

`tools/appointment/__init__.py` already exports `handle_request` and `list_appointments`. No changes needed.

`models/__init__.py`, `prompts/__init__.py` — leave as-is (internal use only, no public consumers broken).

---

## What Does NOT Change

- All public import paths (`from orchestrator import WhatsAppOrchestrator`, `from tools.kg_retriever import retrieve_context`, etc.)
- All runtime behaviour, tool schemas, system prompts, DB queries
- `models/`, `prompts/`, `tools/appointment/`, `tools/session_impact.py`, `tools/query_data.py`, `tools/memory_tool.py`, `tools/delay_report.py`, `tools/bulk_ops.py`
- Test files (`run_manual_test.py`, `test_admin_flow.py`)

---

## Dependency Graph (after)

```
orchestrator/core.py
  ├── orchestrator/gates.py
  │     └── orchestrator/formatters.py
  ├── orchestrator/formatters.py
  ├── orchestrator/utils.py
  ├── orchestrator/schemas.py
  └── orchestrator/llm.py

tools/kg_retriever.py (shim)
  └── tools/kg/__init__.py
        └── tools/kg/context.py
              ├── tools/kg/queries.py
              │     └── tools/kg/client.py
              └── tools/kg/resolver.py
                    └── tools/kg/client.py
```

No circular dependencies. Each module's imports flow in one direction only.

---

## Verification

After each file is created/modified:
1. `python3 -c "from orchestrator import WhatsAppOrchestrator"` — no import error
2. `python3 -c "from tools.kg_retriever import retrieve_context"` — no import error
3. Run `run_manual_test.py` for a basic patient greeting turn — same response as before
