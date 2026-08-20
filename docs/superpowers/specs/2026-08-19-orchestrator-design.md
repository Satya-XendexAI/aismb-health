# WhatsApp Orchestrator — Design Spec

**Date:** 2026-08-19
**Scope:** Backend LLD only — no UI/channel integration
**Status:** Approved

---

## 1. Overview

The WhatsApp Orchestrator is the single entry point for all inbound patient messages. It runs a ReAct (Reason + Act) loop: an LLM reasons over the conversation and picks a tool or produces a final response. Tool calls pass through a gate that checks auth and confirmation requirements before executing. Interrupts suspend the loop and send a WhatsApp prompt to the patient (identity request or confirmation request); the loop resumes on the patient's next reply.

All three tools are dummy stubs for now — they log their arguments and return a mock response. The orchestrator flow (hydrate → agent → gate → execute/interrupt → responder) is the full implementation scope of this spec.

---

## 2. Architecture

```
                     inbound WA message
                            │
                     ┌──────▼──────┐
                     │   hydrate   │  resolve patient, load session
                     └──────┬──────┘
                            │
                     ┌──────▼──────┐ ◄──────────────────────────┐
                     │    agent    │  LLM ReAct: text or tool_call│
                     │   (ReAct)   │                              │
                     └──────┬──────┘                             │
                            │                                     │
                    tool_call?                                    │
              ┌─────────────┴──────────┐                         │
              no (text)               yes                         │
              │                        │                          │
              │                 ┌──────▼──────┐                  │
              │                 │    gate     │                   │
              │                 └──────┬──────┘                  │
              │              ┌─────────┼─────────┐               │
              │         AUTH_REQ  CONFIRM_REQ    OK               │
              │              │         │          │               │
              │         ┌────▼─────────▼─┐  ┌────▼──────────┐   │
              │         │   interrupt()  │  │  execute_tool │───┘
              │         │ (send WA + save│  │  (dummy log)  │
              │         │   session)     │  └───────────────┘
              │         └───────────────┘
              │
       ┌──────▼──────┐
       │  responder  │  chunk + format + send
       └─────────────┘

[ LLMAdapter + WANotifier + Repository injected at construction ]
```

**Components:**

- `LLMAdapter` — `run_agent(messages, tool_schemas) → AgentResponse`. `InMemoryLLMAdapter` returns configurable fixed responses for tests; supports `should_return_tool_call` flag.
- `WANotifier` — `send(to_number, text)`. `InMemoryWANotifier` with `should_fail` flag for tests.
- `Repository` — reads `patients` (phone → patient_id lookup), reads/writes `sessions` to SQL DB.
- **Tool handlers** — three dummy functions; no external calls.

`WhatsAppOrchestrator` holds no mutable state itself.

---

## 3. Data Models

### 3.1 Runtime Objects (not persisted)

```
WAMessage                              -- inbound WhatsApp payload
  from_number    str                   patient's WhatsApp number
  message_id     str                   platform-assigned dedup ID
  text           str                   raw message text
  timestamp      datetime
  hospital_id    UUID

OrchestratorContext                    -- assembled by hydrate; passed through all nodes
  wa_message     WAMessage
  session        Session
  patient_id     UUID?                 resolved from from_number; None if unknown

AgentResponse                          -- returned by LLMAdapter.run_agent()
  type           Enum   TOOL_CALL | TEXT
  tool_call      ToolCall?
  text           str?

ToolCall
  tool_name      Enum   APPOINTMENT | KG_RETRIEVER | QUERY_DATA
  args           dict

GateResult
  status         Enum   OK | AUTH_REQUIRED | CONFIRM_REQUIRED

ChatTurn                               -- one entry in session.history
  role           Enum   USER | ASSISTANT | TOOL_RESULT
  content        str
  tool_call      ToolCall?             present only when role = ASSISTANT + tool called
```

### 3.2 DB Table

```
sessions                               -- one per (hospital_id, from_number)
  session_id     UUID        PK
  hospital_id    UUID
  from_number    str
  patient_id     UUID?                 None if patient identity not resolved
  state          Enum        IDLE | AWAITING_CONFIRM | AWAITING_AUTH
  history        JSON                  List[ChatTurn]; capped at max_history_turns (default 10)
  pending_tool   JSON?                 saved ToolCall when state = AWAITING_CONFIRM
  created_at     datetime
  updated_at     datetime

  UNIQUE (hospital_id, from_number)
```

**Session state machine:**

```
IDLE ──────────────────────────► AWAITING_CONFIRM  (gate: confirm needed)
     ──────────────────────────► AWAITING_AUTH      (gate: patient unknown)

AWAITING_CONFIRM ──── user "YES" ──► IDLE  (execute pending tool, clear pending_tool)
                 ──── user "NO"  ──► IDLE  (cancel, clear pending_tool)
                 ──── other text ──► re-enter agent loop (user changed intent)

AWAITING_AUTH    ──── patient_id resolved ──► IDLE (continue agent loop)
                 ──── unresolvable         ──► stays AWAITING_AUTH (re-prompt)
```

### 3.3 Tool Schemas (construction-time constant)

```
TOOL_SCHEMAS = [
  {
    name: "appointment",
    description: "Book or cancel a doctor appointment",
    parameters: { action: "book|cancel", doctor_id: str, date: str }
  },
  {
    name: "kg_retriever",
    description: "Answer questions about doctors, departments, timings, procedures",
    parameters: { query: str }
  },
  {
    name: "query_data",
    description: "Fetch patient records, test results, or prescriptions",
    parameters: { query_type: str, filters: dict }
  }
]
```

---

## 4. Core Flows

### 4.1 handle_message(wa_message) [main entry point]

```
Input: WAMessage

1. context = _hydrate(wa_message)
   session  = context.session

2. Append ChatTurn(role=USER, content=wa_message.text) to session.history

3. Handle suspended states:

   If session.state == AWAITING_CONFIRM:
     reply = wa_message.text.strip().upper()
     If reply == "YES":
       tool_call = session.pending_tool
       session.pending_tool = None
       session.state = IDLE
       Repository.save_session(session)
       result = _execute_tool(tool_call)
       Append ChatTurn(role=TOOL_RESULT, content=str(result)) to session.history
       → continue to ReAct loop (step 4)
     Elif reply == "NO":
       session.pending_tool = None
       session.state = IDLE
       _responder("Understood. Your request has been cancelled.", context)
       return
     Else:
       session.state = IDLE     ← user changed intent; re-enter loop normally
       session.pending_tool = None

   If session.state == AWAITING_AUTH:
     patient_id = Repository.get_patient_id_by_phone(
                    wa_message.from_number, wa_message.hospital_id)
     If patient_id:
       session.patient_id = patient_id
       session.state = IDLE
       Repository.save_session(session)
     → continue to ReAct loop (step 4); gate will re-check auth

4. ReAct loop (max_iterations = 5):

   For i in range(max_iterations):

     agent_response = LLMAdapter.run_agent(session.history, TOOL_SCHEMAS)

     If agent_response.type == TEXT:
       final_text = agent_response.text
       break  ← exit loop with response

     # type == TOOL_CALL
     Append ChatTurn(role=ASSISTANT, tool_call=agent_response.tool_call) to session.history

     gate_result = _gate(agent_response.tool_call, context)

     If gate_result.status == AUTH_REQUIRED:
       _interrupt(gate_result, agent_response.tool_call, context)
       return  ← suspend; resume on next message

     If gate_result.status == CONFIRM_REQUIRED:
       _interrupt(gate_result, agent_response.tool_call, context)
       return  ← suspend; resume on next message

     # gate_result.status == OK
     result = _execute_tool(agent_response.tool_call)
     Append ChatTurn(role=TOOL_RESULT, content=str(result)) to session.history

   Else (loop exhausted without TEXT response):
     final_text = fallback_text   ← configurable at construction

5. _responder(final_text, context)
```

### 4.2 _hydrate(wa_message) → OrchestratorContext [internal]

```
1. session = Repository.get_session(wa_message.hospital_id, wa_message.from_number)
   If None:
     session = Session(
       hospital_id = wa_message.hospital_id,
       from_number = wa_message.from_number,
       patient_id  = None,
       state       = IDLE,
       history     = [],
       pending_tool = None
     )

2. If session.patient_id is None:
     patient_id = Repository.get_patient_id_by_phone(
                    wa_message.from_number, wa_message.hospital_id)
     If patient_id:
       session.patient_id = patient_id

3. Repository.save_session(session)

4. Return OrchestratorContext(wa_message, session, patient_id=session.patient_id)
```

### 4.3 _gate(tool_call, context) → GateResult [internal]

```
If tool_call.tool_name == APPOINTMENT:

  If context.patient_id is None:
    return GateResult(AUTH_REQUIRED)

  If context.session.state != AWAITING_CONFIRM:
    return GateResult(CONFIRM_REQUIRED)

return GateResult(OK)
```

**Gate rules:**

| Tool | Auth required | Confirm required |
|---|---|---|
| `APPOINTMENT` | Yes — patient_id must be known | Yes — destructive action |
| `KG_RETRIEVER` | No | No |
| `QUERY_DATA` | No | No |

### 4.4 _execute_tool(tool_call) → dict [internal — dummy]

```
APPOINTMENT:
  log(f"[TOOL] appointment called with: {tool_call.args}")
  return {"status": "ok", "result": "Appointment tool executed (dummy)"}

KG_RETRIEVER:
  log(f"[TOOL] kg_retriever called with: {tool_call.args}")
  return {"facts": ["(dummy KG fact)"]}

QUERY_DATA:
  log(f"[TOOL] query_data called with: {tool_call.args}")
  return {"data": "(dummy query result)"}
```

### 4.5 _interrupt(gate_result, tool_call, context) [internal]

```
If gate_result.status == AUTH_REQUIRED:
  message = "To proceed, I need to verify your identity. Please share your Patient ID."
  context.session.state = AWAITING_AUTH

If gate_result.status == CONFIRM_REQUIRED:
  message = f"Please confirm: {_describe_tool(tool_call)}. Reply YES to proceed or NO to cancel."
  context.session.pending_tool = tool_call
  context.session.state = AWAITING_CONFIRM

Repository.save_session(context.session)
try:
  WANotifier.send(context.wa_message.from_number, message)
except Exception:
  pass   ← best-effort; session state already saved

_describe_tool(tool_call):
  APPOINTMENT → f"appointment action: {tool_call.args}"
  KG_RETRIEVER → f"knowledge graph query: {tool_call.args.get('query')}"
  QUERY_DATA   → f"data query: {tool_call.args.get('query_type')}"
```

### 4.6 _responder(text, context) [internal]

```
1. chunks = _chunk(text, max_chars=1000)
   _chunk: split on sentence boundaries; never mid-word

2. For each chunk:
     try:
       WANotifier.send(context.wa_message.from_number, chunk)
     except Exception:
       pass   ← best-effort

3. Append ChatTurn(role=ASSISTANT, content=text) to context.session.history

4. Trim session.history to last max_history_turns (default 10)

5. Repository.save_session(context.session)
```

---

## 5. Error Cases

| Scenario | Behaviour |
|---|---|
| `LLMAdapter.run_agent` raises | Treat as TEXT response with `fallback_text`; break loop |
| ReAct loop hits max_iterations (5) | Use `fallback_text` as final response |
| `WANotifier.send` raises (responder) | Best-effort; session still saved |
| `WANotifier.send` raises (interrupt) | Best-effort; session state already saved; patient misses prompt |
| Patient replies "YES" with no pending_tool (stale state) | Treat as IDLE, re-enter agent loop normally |
| `from_number` not in patients table | `patient_id = None`; auth gate catches it only when APPOINTMENT tool is called |

---

## 6. Idempotency

- `message_id` deduplication is the webhook handler's responsibility — `handle_message` is not idempotent.
- Session `UNIQUE (hospital_id, from_number)` prevents duplicate session rows.
- `pending_tool` is cleared on YES/NO/re-entry — answering twice is safe.

---

## 7. Design Patterns

| Pattern | Application |
|---|---|
| ReAct loop | LLM reasons + acts in a loop; each tool result is fed back into context for next reasoning step |
| Gate | Auth and confirm checks separated from tool execution — tools stay pure |
| Interrupt + resume | Suspend loop on AUTH/CONFIRM; resume transparently on next patient message |
| Session state machine | `IDLE → AWAITING_CONFIRM / AWAITING_AUTH → IDLE`; pending tool call persisted across messages |
| Adapter | `LLMAdapter`, `WANotifier` behind ABCs; in-memory stubs for tests |
| Loop guard | Max 5 iterations prevents runaway LLM loops; falls back to generic response |
| Chunked responder | Long responses split at sentence boundaries for WA readability |
