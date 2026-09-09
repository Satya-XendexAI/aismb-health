# How a Message Gets Identified and Classified — End to End

**Question this answers:** when someone messages the bot, where does the phone number actually get read, and where/how does the system decide "is this a patient, doctor, or admin"? Traced against the real code, with a concrete example using `919640229888`.

---

## Step 1 — the phone number enters the system

Two possible entry points, both landing on the same shape immediately.

**Real WhatsApp** (`whatsapp.py`): Meta's webhook POSTs a JSON payload to `/webhook`. `extract_text_message()` (line 72) pulls the sender's number straight out of Meta's own payload structure:
```python
"from_number": message["from"]   # e.g. "919640229888"
```
This is **not user-editable** — it's WhatsApp's own account number for whoever sent the message, verified by WhatsApp itself. This is the one piece of identity in the whole system that's actually authenticated, not self-declared.

**Local test interface** (`interface/app.py`): `POST /api/send` takes `from_number` directly from the request body — whatever phone number you're logged in as on the mockup UI. No real authentication here (it's a dev tool), which is why testing "as" a specific number is as simple as typing it into the login screen.

Either way, the number becomes one field on a `WAMessage`:
```python
WAMessage(from_number="919640229888", message_id=..., text=..., hospital_id=...)
```

## Step 2 — session lookup: is this number already known?

`orchestrator/core.py`'s `_hydrate()` is the very first thing that runs for every message:
```python
session = self.repository.get_session(wa_message.hospital_id, wa_message.from_number)
```
`InMemoryRepository.get_session()` looks up an in-memory dict keyed on `f"{hospital_id}:{from_number}"` — e.g. `"glngs-chn:919640229888"`. If this exact combination has messaged before (in this process's lifetime — it's in-memory, not persisted), the existing `Session` object comes back with its conversation history, role, and state intact. If not, a brand-new `Session` is created.

**This is also the multi-tenant boundary** — the same phone number messaging two different hospitals gets two completely separate sessions, since the key includes `hospital_id`. Nothing about role or history crosses between them.

## Step 3 — role classification: THIS is where "patient vs doctor vs admin" gets decided

Only for a brand-new session — an existing one already has its role cached. `_hydrate()` calls:
```python
role = self.repository.get_role(wa_message.from_number)
```
And `InMemoryRepository.get_role()` (`orchestrator/session.py`) is the entire classification logic, three lines:
```python
def get_role(self, from_number: str) -> Role:
    if any(a["phone"] == from_number for a in self._admins):
        return Role.ADMIN
    if any(d["phone"] == from_number for d in self._doctors):
        return Role.DOCTOR
    return Role.PATIENT
```
`self._admins` and `self._doctors` are loaded once at startup from `config/doctors.json`. **There is no other mechanism anywhere in the codebase that assigns a role.** It's a flat linear scan against two small lists, admin checked first, then doctor, defaulting to patient if neither matches.

### Tracing `919640229888` concretely

Checked against the actual live `config/doctors.json`:
```
admins:  919916219776 (Satya)
doctors: 919840689449 (Dr. Ajit Yadav), 919843689649 (Dr. Susan George)
```
`919640229888` matches neither list → **classified as `Role.PATIENT`**. This is exactly what happens for *any* number not explicitly listed as a doctor or admin — every unregistered number is a patient by default, no explicit "sign up" step required.

## Step 4 — role decides everything downstream, immediately

Once `session.role` is set, `_build_prompt_and_tools()` picks the tool schema list and system prompt for the rest of the conversation:
```python
PATIENT_TOOLS / DOCTOR_TOOLS / ADMIN_TOOLS   (orchestrator/schemas.py)
PATIENT_SYSTEM_PROMPT / DOCTOR_SYSTEM_PROMPT / ADMIN_SYSTEM_PROMPT   (prompts/system.py)
```
And `_gate()` — the permission check run before every tool call — checks the *same* `session.role` against `ROLE_PERMISSIONS`. A patient's session never even has `query_data` (the doctor/admin data tool) in its available tool list, and even if it somehow did, the gate would return `FORBIDDEN`.

**Nothing about role is re-evaluated per message.** It's decided once, at first contact, cached on the session for its lifetime. If `919640229888` were later added to `config/doctors.json`'s doctor list, it would *not* retroactively change an existing session — only a fresh session (new process, or after that specific session is somehow cleared) would pick up the new role, since `get_role()` only runs inside the "session is None" branch of `_hydrate()`.

## Step 5 — from role onward, the phone number's job changes

Once role is settled, `from_number` keeps being used, but for different purposes per role:

- **Patient**: becomes `requester_phone` on every booking (`orchestrator/core.py`'s `_execute_tool`) — the stable identity anchor for `find_family_member`/`insert_family_member`, and the scope filter for `list_appointments`/`memory_tool`. A patient can never see or affect another phone number's data — every query is filtered by this exact string.
- **Doctor**: resolved back to a `doctor_id` via `get_doctor_config(from_number)`, then hard-bound as the filter parameter in `query_data`'s generated SQL — so a doctor's questions can only ever return their own patients, regardless of phrasing (this is the mechanism that closed the earlier "doctor vs patient intent ambiguity" question).
- **Admin**: resolved to an admin config via `get_admin_config(from_number)`, used mainly for greeting by name; admin's tools (`get_session_impact`, `execute_plan`, etc.) operate hospital-wide rather than being scoped to the admin's own number.

---

## Summary diagram

```
Incoming message
      │
      ▼
extract from_number (whatsapp.py: from Meta's payload / interface/app.py: from request body)
      │
      ▼
WAMessage(from_number, ...)
      │
      ▼
_hydrate(): session = get_session(hospital_id, from_number)
      │
   ┌──┴──┐
  found   not found
   │        │
   │        ▼
   │   get_role(from_number)
   │   → check admins list  → Role.ADMIN
   │   → check doctors list → Role.DOCTOR
   │   → neither             → Role.PATIENT   (default — this is 919640229888's case)
   │        │
   └────┬───┘
        ▼
session.role is now fixed for this session's lifetime
        │
        ▼
role picks: tool schema list + system prompt + permission gate behavior
        │
        ▼
from_number continues on as: requester_phone (patient) / doctor_id lookup key (doctor) / admin lookup key (admin)
```
