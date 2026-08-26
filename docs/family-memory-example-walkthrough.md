# Family Memory — End-to-End Example Walkthrough

**Scenario:** Ravi Kumar (phone: `9876543210`) books for himself, his wife Priya (same phone), his father Ramesh (own phone: `9998887770`), then cancels only his wife's appointment.

---

## Step 1: Ravi Books for Himself

### WhatsApp Conversation
```
Ravi:  "I want to book an appointment with Dr. Ajit Yadav"
Bot:   "Is this appointment for you, or for a family member?"
Ravi:  "For me. My name is Ravi Kumar"
Bot:   "Please confirm: BOOK appointment with Dr. Ajit Yadav (General Medicine) for Ravi Kumar on today"
Ravi:  "Yes"
```

### Code Path

**1. LLM generates tool call:**
```json
{
  "action": "BOOK",
  "doctor_id": "dr-ajit-yadav--chn",
  "department": "General Medicine",
  "patient_name": "Ravi Kumar",
  "relation_to_booker": "self"
}
```

**2. `orchestrator/core.py` → `_execute_tool()` (line 155–163) builds payload:**
```python
payload = {
    **tool_call.args,                                    # LLM's fields
    "hospital_id":   "glngs-chn",                        # from wa_message
    "booker_phone":  "9876543210",                       # 🆕 from wa_message.from_number
    "patient_phone": "9876543210",                       # LLM didn't set it → defaults to from_number
}
```

**3. `tools/appointment/__init__.py` → `handle_request()` validates via `IncomingPayload`:**
```python
IncomingPayload(
    action="BOOK",
    hospital_id="glngs-chn",
    doctor_id="dr-ajit-yadav--chn",
    department="General Medicine",
    patient_name="Ravi Kumar",
    patient_phone="9876543210",
    booker_phone="9876543210",           # 🆕
    relation_to_booker="self",           # 🆕
    date=None,
)
```

**4. `tools/appointment/booking.py` → `book()` executes:**

```
Step 4a: db.get_hospital(conn, "glngs-chn")
         → SELECT * FROM hospitals WHERE hospital_id = 'glngs-chn'
         → Returns: {hospital_id: "glngs-chn", name: "Gleneagles Chennai", booking_mode: "TOKEN", ...}

Step 4b: db.get_doctor(conn, "dr-ajit-yadav--chn", "glngs-chn")
         → SELECT * FROM doctors WHERE doctor_id = 'dr-ajit-yadav--chn' AND hospital_id = 'glngs-chn' AND is_active = true
         → Returns: {doctor_id: "dr-ajit-yadav--chn", name: "Ajit Yadav", avg_checkin_time: "10:00", avg_consultation_minutes: 10, fee: 800}

Step 4c: db.find_family_member(conn, "9876543210", "glngs-chn", "Ravi Kumar", "self")  🆕
         → SELECT * FROM patients
           WHERE booked_by_phone = '9876543210'
             AND hospital_id = 'glngs-chn'
             AND LOWER(name) = LOWER('Ravi Kumar')
           ORDER BY updated_at DESC LIMIT 1
         → Returns: None (first time)

Step 4d: db.insert_family_member(...)  🆕
         → INSERT INTO patients (hospital_id, name, phone, age, location, diagnosis, booked_by_phone, relation_to_booker)
           VALUES ('glngs-chn', 'Ravi Kumar', '9876543210', NULL, NULL, NULL, '9876543210', 'self')
           ON CONFLICT (hospital_id, booked_by_phone, LOWER(name)) DO NOTHING
           RETURNING *
         → Returns: {patient_id: "uuid-001", name: "Ravi Kumar", booked_by_phone: "9876543210", relation_to_booker: "self"}

Step 4e: db.get_or_create_today_session(conn, "dr-ajit-yadav--chn", "glngs-chn", None)
         → INSERT INTO doctor_sessions (doctor_id, hospital_id, date, status) VALUES (..., CURRENT_DATE, 'OPEN')
           ON CONFLICT DO NOTHING
         → Returns: {session_id: "sess-001", date: "2026-08-25", status: "OPEN", started_at: null}

Step 4f: db.find_active_token(conn, "uuid-001", "dr-ajit-yadav--chn", None)
         → SELECT * FROM tokens t JOIN doctor_sessions ds ON ...
           WHERE t.patient_id = 'uuid-001' AND t.doctor_id = 'dr-ajit-yadav--chn'
             AND t.status = 'WAITING' AND ds.date = CURRENT_DATE
         → Returns: None (no existing token)

Step 4g: db.insert_token(conn, session_id="sess-001", patient_id="uuid-001", ...)
         → SELECT session_id FROM doctor_sessions WHERE session_id = 'sess-001' FOR UPDATE  (lock)
         → SELECT COALESCE(MAX(token_number), 0) + 1 FROM tokens WHERE session_id = 'sess-001'  → 1
         → INSERT INTO tokens (session_id, patient_id, doctor_id, hospital_id, department, token_number, status)
           VALUES ('sess-001', 'uuid-001', 'dr-ajit-yadav--chn', 'glngs-chn', 'General Medicine', 1, 'WAITING')
         → Returns: {token_id: "tok-001", token_number: 1, status: "WAITING"}

Step 4h: db.touch_family_member(conn, "uuid-001")  🆕
         → UPDATE patients SET updated_at = NOW() WHERE patient_id = 'uuid-001'

Step 4i: db.count_patients_ahead(conn, "sess-001", 1)
         → SELECT COUNT(*) FROM tokens WHERE session_id = 'sess-001' AND token_number < 1 AND status = 'WAITING'
         → Returns: 0

Step 4j: calculate_eta(session, doctor, 0)
         → started_at is null → fallback to avg_checkin_time = 10:00
         → 0 patients ahead × 10 min = 0 min wait
         → estimated_time = 2026-08-25 10:00:00
```

### Tables After Step 1

**`patients` table:**
| patient_id | hospital_id | name | phone | booked_by_phone 🆕 | relation_to_booker 🆕 |
|---|---|---|---|---|---|
| uuid-001 | glngs-chn | Ravi Kumar | 9876543210 | 9876543210 | self |

**`doctor_sessions` table:**
| session_id | doctor_id | hospital_id | date | status | started_at |
|---|---|---|---|---|---|
| sess-001 | dr-ajit-yadav--chn | glngs-chn | 2026-08-25 | OPEN | null |

**`tokens` table:**
| token_id | session_id | patient_id | doctor_id | department | token_number | status |
|---|---|---|---|---|---|---|
| tok-001 | sess-001 | uuid-001 | dr-ajit-yadav--chn | General Medicine | 1 | WAITING |

### Response to WhatsApp
```json
{
  "action": "BOOK",
  "result": {
    "status": "CONFIRMED",
    "token_number": 1,
    "patient_name": "Ravi Kumar",
    "relation_to_booker": "self",
    "doctor_name": "Ajit Yadav",
    "department": "General Medicine",
    "hospital_name": "Gleneagles Chennai",
    "fee": 800.0,
    "estimated_time": "2026-08-25T10:00:00"
  }
}
```

---

## Step 2: Ravi Books for Wife Priya (Same Phone)

### WhatsApp Conversation
```
Ravi:  "Also book for my wife Priya with the same doctor"
Bot:   "Does Priya have her own phone number, or should I use yours?"
Ravi:  "Use mine"
Bot:   "Please confirm: BOOK appointment with Dr. Ajit Yadav (General Medicine) for Priya Kumar (wife) on today"
Ravi:  "Yes"
```

### Code Path (differences from Step 1 only)

**LLM tool call:**
```json
{
  "action": "BOOK",
  "doctor_id": "dr-ajit-yadav--chn",
  "department": "General Medicine",
  "patient_name": "Priya Kumar",
  "relation_to_booker": "wife"
}
```
Note: LLM did NOT set `patient_phone` → `core.py` defaults it to `9876543210` (sender's number).

**`booking.py:book()` key steps:**

```
Step: db.find_family_member(conn, "9876543210", "glngs-chn", "Priya Kumar", "wife")
     → SELECT * FROM patients
       WHERE booked_by_phone = '9876543210'
         AND hospital_id = 'glngs-chn'
         AND LOWER(name) = LOWER('Priya Kumar')
         AND LOWER(relation_to_booker) = LOWER('wife')
     → Returns: None (first time for Priya)

Step: db.insert_family_member(name="Priya Kumar", phone="9876543210", relation="wife", ...)
     → INSERT INTO patients (..., booked_by_phone, relation_to_booker)
       VALUES (..., '9876543210', 'wife')
     → Returns: {patient_id: "uuid-002", name: "Priya Kumar", relation_to_booker: "wife"}
            ↑ NEW patient row, even though SAME phone as Ravi ✅

Step: db.find_active_token(conn, "uuid-002", "dr-ajit-yadav--chn")
     → Returns: None (Priya has no tokens)

Step: db.insert_token(session_id="sess-001", patient_id="uuid-002", ...)
     → Next token_number = 2 (Ravi was #1)
     → Returns: {token_id: "tok-002", token_number: 2}
```

### Tables After Step 2

**`patients` table:**
| patient_id | hospital_id | name | phone | booked_by_phone | relation_to_booker |
|---|---|---|---|---|---|
| uuid-001 | glngs-chn | Ravi Kumar | 9876543210 | 9876543210 | self |
| **uuid-002** | glngs-chn | **Priya Kumar** | **9876543210** | **9876543210** | **wife** |

↑ Same phone, same booker, but **different patient row** because name differs.

**`tokens` table:**
| token_id | session_id | patient_id | token_number | status |
|---|---|---|---|---|
| tok-001 | sess-001 | uuid-001 (Ravi) | 1 | WAITING |
| **tok-002** | sess-001 | **uuid-002 (Priya)** | **2** | **WAITING** |

**`doctor_sessions` — unchanged** (same session reused).

---

## Step 3: Ravi Books for Father Ramesh (Different Phone)

### WhatsApp Conversation
```
Ravi:  "Book for my father Ramesh Kumar with Dr. Ajit too"
Bot:   "Does Ramesh have his own phone number?"
Ravi:  "Yes, 9998887770"
Bot:   "Please confirm: BOOK appointment with Dr. Ajit Yadav (General Medicine) for Ramesh Kumar (father) on today"
Ravi:  "Yes"
```

### Code Path (key difference)

**LLM tool call — this time it SETS `patient_phone`:**
```json
{
  "action": "BOOK",
  "doctor_id": "dr-ajit-yadav--chn",
  "department": "General Medicine",
  "patient_name": "Ramesh Kumar",
  "relation_to_booker": "father",
  "patient_phone": "9998887770"
}
```

**`core.py:_execute_tool()` — uses LLM's phone instead of sender's:**
```python
payload = {
    ...
    "booker_phone":  "9876543210",                     # Always the WhatsApp sender
    "patient_phone": "9998887770",                     # 🆕 LLM provided it → NOT overwritten
}
```

**`booking.py:book()` key steps:**
```
Step: db.insert_family_member(name="Ramesh Kumar", phone="9998887770", relation="father", booker="9876543210")
     → INSERT: phone = 9998887770 (father's OWN phone), booked_by = 9876543210 (Ravi's phone)
     → Returns: {patient_id: "uuid-003", phone: "9998887770", relation_to_booker: "father"}

Step: insert_token → Token #3
```

### Tables After Step 3

**`patients` table:**
| patient_id | name | phone | booked_by_phone | relation_to_booker |
|---|---|---|---|---|
| uuid-001 | Ravi Kumar | 9876543210 | 9876543210 | self |
| uuid-002 | Priya Kumar | 9876543210 | 9876543210 | wife |
| **uuid-003** | **Ramesh Kumar** | **9998887770** | **9876543210** | **father** |

↑ All 3 share `booked_by_phone = 9876543210` (Ravi). Ramesh has his OWN phone.

**`tokens` table:**
| token_id | patient_id | token_number | status |
|---|---|---|---|
| tok-001 | uuid-001 (Ravi) | 1 | WAITING |
| tok-002 | uuid-002 (Priya) | 2 | WAITING |
| **tok-003** | **uuid-003 (Ramesh)** | **3** | **WAITING** |

---

## Step 4: Ravi Cancels ONLY Wife's Appointment

### WhatsApp Conversation
```
Ravi:  "Cancel my wife Priya's appointment"
Bot:   "Please confirm: CANCEL appointment with Dr. Ajit Yadav (General Medicine) for Priya Kumar (wife) on today"
Ravi:  "Yes"
```

### Code Path

**LLM tool call:**
```json
{
  "action": "CANCEL",
  "doctor_id": "dr-ajit-yadav--chn",
  "department": "General Medicine",
  "patient_name": "Priya Kumar",
  "relation_to_booker": "wife"
}
```

**`booking.py:cancel()` executes:**

```
Step 1: db.find_family_member(conn, "9876543210", "glngs-chn", "Priya Kumar", "wife")
        → SELECT * FROM patients
          WHERE booked_by_phone = '9876543210'
            AND hospital_id = 'glngs-chn'
            AND LOWER(name) = LOWER('Priya Kumar')
            AND LOWER(relation_to_booker) = LOWER('wife')
        → Returns: {patient_id: "uuid-002", name: "Priya Kumar", relation_to_booker: "wife"}
                    ↑ Finds PRIYA, not Ravi ✅

Step 2: db.find_active_token(conn, "uuid-002", "dr-ajit-yadav--chn")
        → SELECT t.* FROM tokens t JOIN doctor_sessions ds ON ...
          WHERE t.patient_id = 'uuid-002'
            AND t.doctor_id = 'dr-ajit-yadav--chn'
            AND t.status = 'WAITING'
            AND ds.date = CURRENT_DATE
        → Returns: {token_id: "tok-002", token_number: 2, status: "WAITING"}

Step 3: db.cancel_token(conn, "tok-002")
        → UPDATE tokens SET status = 'CANCELLED' WHERE token_id = 'tok-002'
```

### Tables After Step 4

**`patients` table — UNCHANGED** (no rows added or removed, cancellation doesn't delete patients):
| patient_id | name | phone | booked_by_phone | relation_to_booker |
|---|---|---|---|---|
| uuid-001 | Ravi Kumar | 9876543210 | 9876543210 | self |
| uuid-002 | Priya Kumar | 9876543210 | 9876543210 | wife |
| uuid-003 | Ramesh Kumar | 9998887770 | 9876543210 | father |

**`tokens` table — only tok-002 changed:**
| token_id | patient_id | token_number | status |
|---|---|---|---|
| tok-001 | uuid-001 (Ravi) | 1 | WAITING ← **untouched** ✅ |
| tok-002 | uuid-002 (Priya) | 2 | **CANCELLED** ← **changed** |
| tok-003 | uuid-003 (Ramesh) | 3 | WAITING ← **untouched** ✅ |

**`doctor_sessions` — UNCHANGED.**

### Response to WhatsApp
```json
{
  "action": "CANCEL",
  "result": {
    "status": "CANCELLED",
    "message": "Token #2 for Priya Kumar has been cancelled.",
    "cancelled_for": "Priya Kumar"
  }
}
```

Bot sends: *"Priya Kumar's appointment with Dr. Ajit Yadav has been cancelled. Your appointment (Token #1) and Ramesh Kumar's (Token #3) are still active."*

---

## How Relation Fetching Works — The Query Logic

### The Core Query: `find_family_member()`

Every BOOK and CANCEL flows through this single function. Here's how it resolves the right person:

```sql
SELECT patient_id, name, phone, booked_by_phone, relation_to_booker
FROM patients
WHERE booked_by_phone = '9876543210'        -- WHO is on WhatsApp (Ravi)
  AND hospital_id = 'glngs-chn'             -- WHICH hospital (tenant isolation)
  AND LOWER(name) = LOWER('Priya Kumar')    -- WHO is the appointment for
  AND LOWER(relation_to_booker) = LOWER('wife')  -- WHAT is their relation (optional filter)
ORDER BY updated_at DESC
LIMIT 1
```

### Why Each Filter Matters

| Filter | Purpose | What breaks without it |
|---|---|---|
| `booked_by_phone = ?` | Scopes to THIS WhatsApp user's family | Would return someone else's wife named "Priya" |
| `hospital_id = ?` | Multi-tenant isolation | Ravi's family at Hospital A ≠ Hospital B |
| `LOWER(name) = LOWER(?)` | Case-insensitive exact match | "priya kumar" ≠ "Priya Kumar" without LOWER |
| `LOWER(relation) = LOWER(?)` | Disambiguates same-name family | If Ravi has two relatives named "Kumar" |
| `ORDER BY updated_at DESC` | Most recently used first | Stale records don't shadow active ones |
| `LIMIT 1` | Deterministic single result | Prevents random row selection |

### When Relation Filter is Skipped

The relation filter (`AND LOWER(relation_to_booker) = ...`) is only added when `relation != "self"`:

```python
if relation and relation.strip().lower() != "self":
    conditions.append("LOWER(relation_to_booker) = LOWER(%s)")
```

**Why?** For self-bookings, filtering by relation adds no value — the name + booker_phone already uniquely identifies the person. Skipping it avoids false negatives if the LLM inconsistently sends "self" vs not sending it at all.

### Edge Case: Two Sons with Same Name

If Ravi has two sons both named "Rahul Kumar" (unlikely but possible):
- The UNIQUE constraint `(hospital_id, booked_by_phone, LOWER(name))` prevents this
- The second INSERT would hit `ON CONFLICT DO NOTHING` → falls back to `find_family_member()` → returns the existing Rahul
- If they truly need separate records, one would need a distinguishing name (e.g., "Rahul Kumar Jr.")

---

## Summary: Which Tables Change at Each Step

| Step | `patients` | `doctor_sessions` | `tokens` |
|---|---|---|---|
| **1. Book self** | INSERT uuid-001 (Ravi, self) | INSERT sess-001 | INSERT tok-001 (#1, WAITING) |
| **2. Book wife** | INSERT uuid-002 (Priya, wife) | — unchanged — | INSERT tok-002 (#2, WAITING) |
| **3. Book father** | INSERT uuid-003 (Ramesh, father) | — unchanged — | INSERT tok-003 (#3, WAITING) |
| **4. Cancel wife** | — unchanged — | — unchanged — | UPDATE tok-002 → CANCELLED |

**Key insight:** `patients` rows are permanent family records. They're created once and reused for future bookings. Only `tokens.status` changes on cancellation — the patient record stays.
