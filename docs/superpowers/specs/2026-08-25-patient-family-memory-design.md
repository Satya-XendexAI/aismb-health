# Patient Family Memory — Design Spec

**Goal:** Let patients book appointments for family members (mother, father, son, etc.) without re-entering their details each time. Family profiles are registered inline during the first booking and reused automatically in all future sessions across hospitals.

**Architecture:** Phone-scoped identity layer above the existing hospital-scoped `patients` table. Family members stored in `patient_relationships` (linked to `patient_identities` by phone). On session start, the orchestrator loads all registered family members into `session.family_members` (one DB hit per session) and injects them as a single context line into the system prompt. The LLM uses this context to auto-fill booking details when the patient mentions a family member by relation. New members are saved to DB after the first successful booking and cached in the session immediately.

**Tech Stack:** PostgreSQL (psycopg2), existing `InMemoryRepository`, existing `WhatsAppOrchestrator`, Python dataclasses.

---

## Data Model

Two new tables — phone-scoped, no `hospital_id`.

```sql
CREATE TABLE patient_identities (
    identity_id UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    phone       VARCHAR(20) UNIQUE NOT NULL,
    created_at  TIMESTAMP   DEFAULT NOW()
);

CREATE TABLE patient_relationships (
    relationship_id UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    identity_id     UUID        NOT NULL REFERENCES patient_identities(identity_id) ON DELETE CASCADE,
    relation        VARCHAR(30) NOT NULL,
    name            VARCHAR(100) NOT NULL,
    phone           VARCHAR(20),
    age             INTEGER,
    created_at      TIMESTAMP DEFAULT NOW(),
    UNIQUE (identity_id, relation)
);

CREATE INDEX idx_patient_rel_identity ON patient_relationships(identity_id);
```

`relation` values: `mother`, `father`, `son`, `daughter`, `wife`, `husband`, `brother`, `sister`, `grandparent`, `grandchild`, `spouse`.

---

## Session State

Two new fields added to `Session` in `models/session.py`:

```python
family_members: dict = field(default_factory=dict)
# Format: {relation: {name: str, phone: str|None, age: int|None}}
# Example: {"mother": {"name": "Meena", "phone": "9876543210", "age": 58}}

family_loaded: bool = False
# True once DB has been queried for this session — prevents repeated hits
```

---

## New Module: tools/family_memory/

### `database.py`

```python
def get_or_create_identity(conn, phone: str) -> str:
    # INSERT INTO patient_identities(phone) ON CONFLICT DO NOTHING
    # SELECT identity_id WHERE phone = %s
    # Returns identity_id (UUID string)

def load_members(conn, phone: str) -> dict:
    # JOIN patient_relationships → patient_identities WHERE phone = %s
    # Returns {relation: {name, phone, age}}

def upsert_member(conn, phone: str, relation: str, name: str,
                  member_phone: str | None, age: int | None) -> None:
    # get_or_create_identity, then:
    # INSERT INTO patient_relationships ON CONFLICT (identity_id, relation)
    # DO UPDATE SET name=EXCLUDED.name, phone=EXCLUDED.phone, age=EXCLUDED.age
```

### `__init__.py`

```python
def load_family(phone: str) -> dict:
    # Opens own DB connection via database.get_connection()
    # Calls load_members, returns dict

def save_family_member(phone: str, relation: str, name: str,
                       member_phone: str | None = None,
                       age: int | None = None) -> None:
    # Opens own DB connection
    # Calls upsert_member
```

---

## Orchestrator Changes (orchestrator/core.py)

### 1. Pre-load family on first patient message

```python
# After _hydrate, before tool selection
if session.role == Role.PATIENT and not session.family_loaded:
    from tools.family_memory import load_family
    session.family_members = load_family(session.from_number)
    session.family_loaded  = True
    self.repository.save_session(session)
```

### 2. Inject family context into system prompt

```python
def _build_system_prompt(self, session: Session, base_prompt: str) -> str:
    if not session.family_members:
        return base_prompt
    lines = ", ".join(
        f"{rel}={m['name']}({m.get('age', '?')}, {m.get('phone', 'no phone')})"
        for rel, m in session.family_members.items()
    )
    return base_prompt + f"\n\nRegistered family: {lines}"
```

Called in `handle_message` when building `system_prompt` for PATIENT role.

### 3. Save new family member after successful booking

```python
# In _execute_tool, after appointment tool returns result
if tool_call.tool_name == "appointment":
    relation = tool_call.args.get("relation")
    if relation and relation not in context.session.family_members:
        from tools.family_memory import save_family_member
        save_family_member(
            phone        = context.wa_message.from_number,
            relation     = relation,
            name         = tool_call.args.get("patient_name"),
            member_phone = tool_call.args.get("patient_phone_member"),
            age          = tool_call.args.get("patient_age"),
        )
        context.session.family_members[relation] = {
            "name":  tool_call.args.get("patient_name"),
            "phone": tool_call.args.get("patient_phone_member"),
            "age":   tool_call.args.get("patient_age"),
        }
```

---

## Appointment Schema Changes (orchestrator/schemas.py)

Two new optional fields added to `_appointment_schema`:

```python
"relation": {
    "type": "string",
    "description": "Relationship of the person being booked to the patient making this request — e.g. 'mother', 'father', 'son'. Only set when booking for a family member, not for self."
},
"patient_phone_member": {
    "type": "string",
    "description": "Phone number of the family member being booked (optional — only if patient provides it)"
},
```

---

## System Prompt Changes (prompts/system.py)

Add to `PATIENT_SYSTEM_PROMPT` under the booking flow:

```
IF patient books for a family member (e.g. 'book for my mother'):
→ Check Registered family context — if the member is listed, use their details directly (name, age, phone) without asking
→ If not registered, collect: relation, name, age (phone optional)
→ Set 'relation' field in the appointment tool call
→ After booking, the system will remember this member for future sessions
```

---

## Conversation Flow Examples

**First time — new family member:**
```
Patient: "Book appointment for my mother with Dr. Susan"
Bot:     "I'd love to help. May I know your mother's name and age?"
Patient: "Meena, 58"
Bot:     "Please confirm: BOOK appointment with Dr. Susan George for Meena"
Patient: "yes"
Bot:     "✅ Confirmed. Token #3 for Meena. I've saved your mother's details for next time."
```

**Second time — registered member:**
```
Patient: "Book for my mother with Dr. Gobu"
Bot:     "Please confirm: BOOK appointment with Dr. Gobu P for Meena (your mother)"
Patient: "yes"
Bot:     "✅ Token #5 for Meena."
```

**No family registered — patient asks:**
```
Patient: "Can you show me my registered family?"
Bot:     "You don't have any family members registered yet. I'll save their details automatically the next time you book for them."
```

---

## Edge Cases

- **Same relation re-registered** — `UPSERT` updates name/age/phone — no duplicate rows
- **Patient books for self** — `relation` field absent — no family save triggered
- **Family member has no phone** — `phone` is nullable — booking still works (hospital phone used)
- **Session restart** — `family_loaded = False` on new session — DB re-queried once, cache rebuilt
