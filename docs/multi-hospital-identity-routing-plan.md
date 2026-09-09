# Multi-Hospital Message Routing & Role Scoping — Implementation Plan

**Problem:** `hospital_id` is currently a hardcoded constant (`HOSPITAL_ID = "glngs-chn"`) in both `whatsapp.py` and `interface/app.py` — never derived from the incoming message. Separately, role classification (`get_role`/`get_doctor_config`/`get_admin_config` in `orchestrator/session.py`) checks a phone number against the doctor/admin lists globally, with no `hospital_id` filter at all. Together, these mean one deployment can only ever serve one hospital, and if it somehow served two, a doctor registered at Hospital A would be misclassified as `Role.DOCTOR` when messaging about Hospital B.

**Fix, in one sentence:** derive `hospital_id` per message from Meta's own `phone_number_id` via a config mapping, and make role lookups take `hospital_id` as a second filter alongside phone number.

**Scope:** `whatsapp.py` (production webhook) and role config only. `interface/app.py` (local test tool) is deliberately left untouched — it has no Meta payload to read a `phone_number_id` from, and stays single-hospital for testing unless multi-hospital local testing is specifically needed later.

**Files touched:** 3 existing files, 1 config file restructured. No new files, no schema/DB changes — `WAMessage.hospital_id` already exists as a field; this is about correctly populating it, not adding it.

---

## 1. `config/doctors.json` — restructure to be hospital-aware

```json
{
  "hospitals": {
    "123456789012345": { "hospital_id": "glngs-chn", "name": "Gleneagles Chennai" }
  },
  "doctors": [
    {
      "doctor_id": "dr-ajit-yadav--chn",
      "hospital_id": "glngs-chn",
      "phone": "919840689449",
      "name": "Dr. Ajit Yadav",
      "tables": ["patients", "tokens", "doctor_sessions"]
    }
  ],
  "admins": [
    { "hospital_id": "glngs-chn", "phone": "919916219776", "name": "Satya" }
  ]
}
```

Two changes to the existing shape: a new top-level `"hospitals"` block mapping Meta's `phone_number_id` → your internal `hospital_id`, and a `"hospital_id"` field added to every doctor and admin entry. Replace `123456789012345` with your real Meta `phone_number_id` (visible in the Meta developer console / the `PHONE_NUMBER_ID` env var already in `whatsapp.py`).

---

## 2. `whatsapp.py` — extract `phone_number_id`, resolve `hospital_id` dynamically

### 2a. Load the hospital mapping alongside the existing doctors/admins load

```python
with open("config/doctors.json") as f:
    _cfg      = json.load(f)
    DOCTORS   = _cfg["doctors"]
    ADMINS    = _cfg.get("admins", [])
    HOSPITALS = _cfg.get("hospitals", {})   # phone_number_id -> {hospital_id, name}
```

### 2b. `extract_text_message()` — also pull `phone_number_id` from `value.metadata`

```python
def extract_text_message(payload: dict) -> dict | None:
    try:
        for entry in payload.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value", {})
                phone_number_id = value.get("metadata", {}).get("phone_number_id", "")
                for message in value.get("messages", []):
                    if message.get("type") == "text":
                        return {
                            "from_number":     message["from"],
                            "message_id":      message["id"],
                            "text":            message["text"]["body"],
                            "phone_number_id": phone_number_id,
                        }
    except (KeyError, TypeError) as exc:
        logger.error("Failed to parse webhook payload: %s", exc)
    return None
```

### 2c. `receive_webhook()` — resolve `hospital_id` from the mapping instead of the hardcoded constant

```python
@app.post("/webhook")
async def receive_webhook(request: Request, background_tasks: BackgroundTasks):
    body = await request.body()
    if not verify_signature(body, request.headers.get("X-Hub-Signature-256", "")):
        return JSONResponse({"error": "invalid signature"}, status_code=403)

    incoming = extract_text_message(json.loads(body))
    if incoming:
        hospital = HOSPITALS.get(incoming["phone_number_id"])
        if not hospital:
            logger.error("Unknown phone_number_id: %s", incoming["phone_number_id"])
            return JSONResponse({"status": "ok"})
        wa_message = WAMessage(
            from_number=incoming["from_number"],
            message_id=incoming["message_id"],
            text=incoming["text"],
            hospital_id=hospital["hospital_id"],
        )
        background_tasks.add_task(orchestrator.handle_message, wa_message)

    return JSONResponse({"status": "ok"})
```

The old `HOSPITAL_ID = "glngs-chn"` constant is removed — nothing else in this file needs it once this is in place.

---

## 3. `orchestrator/session.py` — make role lookup hospital-aware

```python
def get_role(self, from_number: str, hospital_id: str) -> Role:
    if any(a["phone"] == from_number and a.get("hospital_id") == hospital_id for a in self._admins):
        return Role.ADMIN
    if any(d["phone"] == from_number and d.get("hospital_id") == hospital_id for d in self._doctors):
        return Role.DOCTOR
    return Role.PATIENT

def get_doctor_config(self, from_number: str, hospital_id: str) -> dict | None:
    return next(
        (d for d in self._doctors if d["phone"] == from_number and d.get("hospital_id") == hospital_id),
        None,
    )

def get_admin_config(self, from_number: str, hospital_id: str) -> dict | None:
    return next(
        (a for a in self._admins if a["phone"] == from_number and a.get("hospital_id") == hospital_id),
        None,
    )
```

Same three functions, same names — just one added parameter each, and one added `and ... == hospital_id` clause per check.

---

## 4. `orchestrator/core.py` — update all 4 call sites to pass `hospital_id`

| Line | Before | After |
|---|---|---|
| 154 | `self.repository.get_admin_config(wa_message.from_number)` | `self.repository.get_admin_config(wa_message.from_number, wa_message.hospital_id)` |
| 160 | `self.repository.get_doctor_config(wa_message.from_number)` | `self.repository.get_doctor_config(wa_message.from_number, wa_message.hospital_id)` |
| 204 | `self.repository.get_doctor_config(context.wa_message.from_number)` | `self.repository.get_doctor_config(context.wa_message.from_number, context.wa_message.hospital_id)` |
| 264 | `role = self.repository.get_role(wa_message.from_number)` | `role = self.repository.get_role(wa_message.from_number, wa_message.hospital_id)` |

`wa_message.hospital_id` is already available at every one of these call sites — nothing new needs to be threaded through, it's already on the object.

---

## 5. `interface/app.py` — deliberately untouched

Keeps `HOSPITAL_ID = "glngs-chn"` hardcoded exactly as it is today. There's no Meta payload here to extract a `phone_number_id` from, and the local test UI stays scoped to one hospital at a time unless multi-hospital local testing becomes a real need — at which point the fix would be a hospital picker on the login screen, not anything related to this plan.

If `interface/app.py` is used to test the *actual* multi-hospital logic (rather than just booking flows), it would call `get_role(from_number, HOSPITAL_ID)` with its own hardcoded constant — the function signature changes apply there too, just with a fixed value instead of a dynamic lookup.

---

## Why this shape

- **No pipeline redesign.** `WAMessage.hospital_id` already exists and already flows through `_hydrate()` → `Session` → every downstream tool call. This plan only changes *where its value comes from* (a real lookup vs. a hardcoded string) and makes two existing functions hospital-aware — nothing structural.
- **Admins have no alternative to config.** There's no `admins` table anywhere in the Postgres schema, so a config-based, hospital-scoped admin list isn't a shortcut — it's the only option unless a real table gets built later.
- **Doctors could source `hospital_id` from the real `doctors` table instead of duplicating it in JSON** — noted as a real tradeoff, not pursued here. The real `doctors` table already has `hospital_id` (used for booking), so `config/doctors.json` is a second, independently-maintained list of the same doctors. Deliberately kept as JSON for now per the "smallest safe change" principle — worth revisiting if the two lists ever drift out of sync in practice.
- **`interface/app.py` scope cut is deliberate**, not an oversight — it solves a different problem (local iteration on booking logic) than production multi-hospital routing does.

---

## Verification checklist

- [ ] A message with a known `phone_number_id` resolves to the correct `hospital_id` (not the old hardcoded one).
- [ ] A message with an *unknown* `phone_number_id` is logged and safely ignored, not crashed on.
- [ ] The same phone number, registered as a doctor at Hospital A only, is classified `Role.PATIENT` when messaging via Hospital B's WhatsApp number.
- [ ] The same phone number, registered as a doctor at Hospital A, is still correctly classified `Role.DOCTOR` when messaging via Hospital A's own number (no regression).
- [ ] Admin flow (delay reporting, `get_session_impact`, etc.) still works end-to-end for the existing hospital after the signature change.
- [ ] `interface/app.py` still works exactly as before, unaffected.
