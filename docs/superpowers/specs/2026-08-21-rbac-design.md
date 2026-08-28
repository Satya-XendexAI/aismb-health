# RBAC Design — Hospital WhatsApp Assistant

**Date:** 2026-08-21
**Status:** Approved

---

## Overview

Two user roles access the assistant via WhatsApp: **PATIENT** (default) and **DOCTOR** (elevated). Role is resolved at session hydration from a pre-configured doctors registry. Patients and doctors receive different tool schemas so the LLM is never offered tools it cannot use. The gate enforces role permissions as a hard safety net.

---

## Permission Matrix

| Tool          | PATIENT | DOCTOR |
|---------------|---------|--------|
| `kg_retriever` | ✅      | ✅     |
| `appointment`  | ✅ (confirm + auth gates) | ❌ |
| `query_data`   | ❌      | ✅     |

---

## Role Resolution

Role is derived entirely from the incoming WhatsApp phone number at hydration time. If the number is in the doctors registry → `DOCTOR`, otherwise → `PATIENT`. Role is stored on the `Session` and never changes mid-session.

No patient registry exists — any number not in the doctors registry is treated as a patient.

---

## Data Model Changes

**New enum:**
```python
class Role(Enum):
    PATIENT = "patient"
    DOCTOR  = "doctor"
```

**`Session` dataclass** — add one field:
```python
role: Role   # resolved at hydration, immutable for session lifetime
```

**`InMemoryRepository`** — add doctors registry:
```python
def __init__(self, doctors: set = None, known_patients: dict = None):
    self._doctors: set = doctors or set()
    ...

def get_role(self, from_number: str) -> Role:
    return Role.DOCTOR if from_number in self._doctors else Role.PATIENT
```

---

## Doctors Registry — Configuration File

Doctor phone numbers are stored in `config/doctors.json`, not in code:

```json
{
  "doctors": [
    "+91-9876543210",
    "+91-9123456789"
  ]
}
```

Loaded at startup in `run_manual_test.py` (and the production webhook entrypoint):

```python
with open("config/doctors.json") as f:
    doctors = set(json.load(f)["doctors"])

repository = InMemoryRepository(doctors=doctors)
```

Adding or removing a doctor requires only a file edit — no code change.

---

## Role-Scoped Tool Schemas

Replace the single `TOOL_SCHEMAS` list with two role-specific lists:

```python
PATIENT_TOOLS = [appointment_schema, kg_retriever_schema]
DOCTOR_TOOLS  = [kg_retriever_schema, query_data_schema]
```

In `handle_message`, select schemas before the ReAct loop:

```python
tool_schemas = DOCTOR_TOOLS if session.role == Role.DOCTOR else PATIENT_TOOLS
```

Pass `tool_schemas` into every `llm.run_agent()` call. The LLM never sees `query_data` as a patient or `appointment` as a doctor.

---

## Gate — Safety Net

Add `FORBIDDEN` to `GateStatus`:

```python
class GateStatus(Enum):
    OK               = "OK"
    AUTH_REQUIRED    = "AUTH_REQUIRED"
    CONFIRM_REQUIRED = "CONFIRM_REQUIRED"
    FORBIDDEN        = "FORBIDDEN"
```

Define role permissions:

```python
ROLE_PERMISSIONS = {
    "appointment":  {Role.PATIENT},
    "query_data":   {Role.DOCTOR},
    "kg_retriever": {Role.PATIENT, Role.DOCTOR},
}
```

Role check runs first in `_gate`, before any tool-specific logic:

```python
def _gate(self, tool_call, context):
    allowed = ROLE_PERMISSIONS.get(tool_call.tool_name, {Role.PATIENT, Role.DOCTOR})
    if context.session.role not in allowed:
        return GateResult(GateStatus.FORBIDDEN)
    # existing appointment auth/confirm logic...
```

**On FORBIDDEN:** end the turn immediately and send:
> "Sorry, you don't have permission to perform this action."

---

## Hydration Change

`_hydrate` resolves role from the repository and stores it on the session:

```python
session.role = self.repository.get_role(wa_message.from_number)
```

Role is set when the session is first created and not re-evaluated on subsequent turns.

---

## What Is Not Changing

- `query_data` remains a mock tool — no real DB wiring in this scope
- `appointment` tool execution remains a mock
- Session state machine (IDLE / AWAITING_CONFIRM / AWAITING_AUTH) is unchanged
- No separate WhatsApp number for doctors — same entry point, role resolved by phone
- No UI or admin panel for managing the registry — file-based config only
