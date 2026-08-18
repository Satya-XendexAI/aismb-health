# Always-On Support Chatbot — Design Spec

**Date:** 2026-08-18
**Scope:** Backend LLD only — no UI/channel integration
**Status:** Approved

---

## 1. Overview

The Support Chatbot is an always-on WhatsApp assistant that answers patient queries about doctors, timings, departments, procedures, insurance, reports, and billing. It uses a linear pipeline:

1. **Intent classification** (rule-based keyword matching) — routes to KG query or FAQ search
2. **Retrieval** — KG path: LLM generates a graph query → `KGQueryAdapter` executes it; FAQ path: `FAQStore` keyword-scores entries
3. **Response synthesis** — LLM formats retrieved context into a WhatsApp-friendly reply
4. **Logging + delivery** — every message/response pair is logged; response sent via `PatientNotifier`

The service is stateless per message — no conversation history is maintained across turns. There is no human-agent fallback in the current scope. All retrieval and LLM failures degrade gracefully to a configurable fallback text — the patient always receives a response.

All external dependencies (KG, FAQ store, LLM, WhatsApp notifier) are behind swappable adapter interfaces. In-memory stubs are used for standalone development and testing.

---

## 2. Architecture

```
┌──────────────────────────────────────────────────────────────┐
│                   SupportChatbotService                       │
│                                                              │
│  + handle_message(patient_id, hospital_id, text)             │
│                              → ChatResponse                  │
│  + get_conversation_log(patient_id, hospital_id)             │
│                              → List[ConversationEntry]       │
│                                                              │
│  [ adapters + classifier config injected at construction ]   │
└─────────────────┬────────────────────────────────────────────┘
                  │ uses
   ┌──────────────┼───────────────┬──────────────────┬─────────────────┐
   ▼              ▼               ▼                  ▼                 ▼
IntentClassifier  KGQueryAdapter  FAQStore       LLMAdapter      PatientNotifier
(rule-based;      (external —     (external —    (external —     (external —
 no adapter ABC)   KG / Neo4j)     FAQ DB)        Claude API)     WhatsApp)

                                                          ▼
                                                 ConversationStore
                                                 (owned by service)
```

**Components:**

- `IntentClassifier` — keyword-based routing; no adapter ABC (pure logic, no external I/O). Keyword sets configured at construction. Returns `IntentType`: `KG_QUERY | FAQ_SEARCH | UNKNOWN`. KG keywords are checked before FAQ keywords.
- `KGQueryAdapter` — executes a graph query string (Cypher or equivalent) against the KG and returns a list of fact dicts. `InMemoryKGQueryAdapter` returns hardcoded results for tests; `should_fail` flag for resilience tests.
- `FAQStore` — stores hospital-scoped FAQ entries; `search(text, hospital_id, top_n)` returns top-N matches by keyword overlap score. `InMemoryFAQStore` pre-loaded with sample entries for tests.
- `LLMAdapter` — two methods: `generate_kg_query(text, hospital_schema) → str`; `synthesize_response(question, context) → str`. `InMemoryLLMAdapter` returns configurable fixed strings for tests.
- `PatientNotifier` — `send_response(patient_id, response_text)`. Same ABC as other services. `InMemoryPatientNotifier` with `should_fail` flag.
- `ConversationStore` — logs every `ChatMessage` + `ChatResponse` pair. `InMemoryConversationStore` for tests.

`SupportChatbotService` holds no mutable state itself.

---

## 3. Data Models

### 3.1 Construction-time config

```
IntentKeywords                          -- injected at construction
  kg_query_keywords    List[str]        e.g. ["doctor", "timing", "department", "specialty",
                                              "available", "procedure", "who treats", "opd"]
  faq_keywords         List[str]        e.g. ["insurance", "billing", "payment", "report",
                                              "cost", "policy", "price", "refund"]

HospitalSchema                          -- injected at construction; one per hospital_id
  hospital_id          UUID
  description          str              plain-text KG schema passed to LLM as context
                                        (node types, relationship types, property names)
```

### 3.2 External entities (owned by FAQStore)

```
FAQEntry
  faq_id               UUID             PK
  hospital_id          UUID             scoped per hospital; searches never cross hospitals
  question             str              canonical question text
  answer               str              answer text
  tags                 List[str]        e.g. ["billing", "insurance", "reports"]
  created_at           datetime
```

### 3.3 Internal entities (owned by ConversationStore)

```
ChatMessage
  message_id           UUID             PK
  patient_id           UUID
  hospital_id          UUID
  text                 str              raw patient message
  intent               Enum             KG_QUERY | FAQ_SEARCH | UNKNOWN
  received_at          datetime

ChatResponse
  response_id          UUID             PK
  message_id           UUID             FK → ChatMessage
  patient_id           UUID
  hospital_id          UUID
  response_text        str
  source               Enum             KG | FAQ | UNKNOWN_FALLBACK
  responded_at         datetime
ConversationEntry                      -- return type of get_conversation_log; not stored
  message            ChatMessage
  response           ChatResponse
```

**Key rules:**
- Every inbound message produces exactly one `ChatMessage` + one `ChatResponse`, including UNKNOWN intents (`source = UNKNOWN_FALLBACK`).
- `FAQEntry` is scoped by `hospital_id` — searches never cross hospitals.
- `HospitalSchema` is injected at construction; schema updates require service restart (acceptable for demo).
- `handle_message` is not idempotent — duplicate delivery is the webhook handler's responsibility, using the platform message ID.

---

## 4. Core Flows

### 4.1 handle_message(patient_id, hospital_id, text) → ChatResponse

```
1. intent = IntentClassifier.classify(text)

2. Save ChatMessage(patient_id, hospital_id, text, intent, received_at=now)

3a. If KG_QUERY:
      try:
        query = LLMAdapter.generate_kg_query(text, hospital_schema)
        facts = KGQueryAdapter.execute(query)
        if facts:
          response_text = LLMAdapter.synthesize_response(text, facts)
          source = KG
        else:
          response_text = unknown_fallback_text
          source = UNKNOWN_FALLBACK
      except Exception:
        response_text = unknown_fallback_text
        source = UNKNOWN_FALLBACK

3b. If FAQ_SEARCH:
      try:
        matches = FAQStore.search(text, hospital_id, top_n=3)
        if matches:
          response_text = LLMAdapter.synthesize_response(text, matches)
          source = FAQ
        else:
          response_text = unknown_fallback_text
          source = UNKNOWN_FALLBACK
      except Exception:
        response_text = unknown_fallback_text
        source = UNKNOWN_FALLBACK

3c. If UNKNOWN:
      response_text = unknown_fallback_text
      source = UNKNOWN_FALLBACK

4. Save ChatResponse(message_id, patient_id, hospital_id,
                     response_text, source, responded_at=now)

5. try:
     PatientNotifier.send_response(patient_id, response_text)
   except Exception:
     pass   ← best-effort; response already logged

6. Return ChatResponse
```

### 4.2 IntentClassifier.classify(text) → IntentType [internal, rule-based]

```
text_lower = text.lower()

for keyword in kg_query_keywords:   ← checked first; KG takes priority on overlap
  if keyword in text_lower: return KG_QUERY

for keyword in faq_keywords:
  if keyword in text_lower: return FAQ_SEARCH

return UNKNOWN
```

KG keywords are evaluated before FAQ keywords. A message containing both (e.g., "billing for a surgery procedure") resolves to `KG_QUERY` if a KG keyword appears first in the list.

### 4.3 FAQStore.search(text, hospital_id, top_n) → List[FAQEntry]

```
1. candidates = all FAQEntry where faq.hospital_id == hospital_id

2. For each candidate:
     score = count of words from text.lower() that appear in
             (candidate.question + " " + " ".join(candidate.tags)).lower()

3. Return top_n candidates sorted by score desc where score > 0
```

The `InMemoryFAQStore` implements this directly. The real adapter replaces step 2 with embedding similarity search.

### 4.4 get_conversation_log(patient_id, hospital_id) → List[ConversationEntry]

```
1. messages = ConversationStore.get_messages(patient_id, hospital_id)
             ordered by received_at asc

2. For each message, fetch its paired ChatResponse by message_id

3. Return List[ConversationEntry(message, response)]
```

`ConversationEntry` is a paired view — not a stored entity.

---

## 5. Error Cases

| Scenario | Error Code | Behaviour |
|---|---|---|
| `LLMAdapter.generate_kg_query` raises | _(no raise)_ | Catch → `UNKNOWN_FALLBACK` response |
| `KGQueryAdapter.execute` raises | _(no raise)_ | Catch → treat as empty result → `UNKNOWN_FALLBACK` |
| `LLMAdapter.synthesize_response` raises | _(no raise)_ | Catch → `UNKNOWN_FALLBACK` response |
| `FAQStore.search` raises | _(no raise)_ | Catch → treat as zero matches → `UNKNOWN_FALLBACK` |
| `PatientNotifier.send_response` raises | _(no raise)_ | Best-effort; `ChatResponse` already logged |
| Unknown patient or hospital | _(no raise)_ | Service does not validate existence — logs and responds regardless |

All retrieval and LLM failures produce the same `unknown_fallback_text` — the patient always receives a response.

---

## 6. Idempotency

`handle_message` is intentionally not idempotent — two calls with identical text create two log entries. The WhatsApp webhook handler is responsible for deduplicating platform messages using the platform-assigned message ID, consistent with the webhook idempotency pattern used across this platform.

---

## 7. Design Patterns

| Pattern | Application |
|---|---|
| Adapter | `KGQueryAdapter`, `FAQStore`, `LLMAdapter`, `PatientNotifier` — swappable behind ABCs; in-memory stubs for tests |
| Rule-based classifier | `IntentClassifier` — deterministic, free, independently testable; no LLM cost for routing |
| Graceful degradation | All retrieval/LLM failures caught and converted to `UNKNOWN_FALLBACK`; patient always gets a response |
| Separation of concerns | LLM used only for query generation and synthesis — not routing; routing is rule-based |
| Request-response (no tick) | `handle_message` is synchronous; no `evaluate(now)` loop needed |

---

## 8. Open Questions

- Should the intermediate KG query string be logged for debugging and KG quality review? Current design: only the final response is logged.
- Should `IntentClassifier` handle mixed-intent messages (e.g., "Dr. Sharma's billing policy")? Current design: first keyword match wins — KG takes priority.
- Should FAQ entries be manageable via this service's API, or always loaded externally? Current design: `FAQStore` is populated externally — no CRUD on the chatbot service.
- Should the `unknown_fallback_text` include a hospital contact number? Current design: fully configurable string set at construction — hospital can embed contact info.
- When the KG returns results but the LLM synthesis fails, should the raw facts be returned as-is? Current design: synthesis failure → `UNKNOWN_FALLBACK` (raw graph output is not patient-friendly).
