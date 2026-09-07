# Pre-Deployment Checklist — Multi-Hospital (Token + Slot) Rollout

Everything below the line is **already implemented and tested** this session (slot booking/reschedule/cancel, the `list_appointments`/`memory_tool` union, the kg_retriever tenant-scoping fix). This file is the gap list — what's still hardcoded, stubbed, or missing between "works when I test it locally" and "safe to push to GitHub and deploy."

---

## 🔴 Must fix before Mithra can receive real WhatsApp traffic

### 1. `whatsapp.py` only routes to one hospital
`HOSPITAL_ID = "glngs-chn"` is still hardcoded ([whatsapp.py:32](../whatsapp.py#L32)), and `extract_text_message()` never reads Meta's `metadata.phone_number_id` ([whatsapp.py:72-86](../whatsapp.py#L72)) — every inbound webhook, regardless of which hospital's WhatsApp number it hit, is tagged `glngs-chn`. Mithra cannot receive a single real message until this changes.

**Needs, once you have Mithra's real WhatsApp Business number / `phone_number_id`:**
- `config/doctors.json` gains a `"hospitals"` block: `{"<phone_number_id>": {"hospital_id": "...", "name": "..."}}`, one entry per hospital.
- Every doctor/admin entry in `config/doctors.json` gains a `hospital_id` field — `get_role`/`get_doctor_config`/`get_admin_config` ([orchestrator/session.py](../orchestrator/session.py)) currently classify by phone number alone, with no hospital filter. Without this, a Chaitanya doctor messaging via Mithra's number would be misclassified as `Role.DOCTOR` there too.
- `whatsapp.py`'s `extract_text_message()` extracts `phone_number_id`; `receive_webhook()` looks up `hospital_id` from the new map instead of the constant. Full design already written up in `docs/multi-hospital-identity-routing-plan.md` — not yet applied to actual code.
- `orchestrator/core.py`'s 4 call sites that pass `from_number` alone to `get_role`/`get_doctor_config`/`get_admin_config` need `hospital_id` added (see that plan doc's §4 for exact line numbers, though they'll have shifted since it was written).

### 2. Outbound replies are also single-hospital
`PHONE_NUMBER_ID`/`GRAPH_API_URL`/`ACCESS_TOKEN` are built once at module load from one set of env vars ([whatsapp.py:33-37](../whatsapp.py#L33)). Even after inbound routing is fixed, `WhatsAppNotifier.send()` would still reply to a Mithra patient *from Chaitanya's number*. Needs the send path to pick the right `phone_number_id`/`access_token` per hospital (per-hospital values in `config/doctors.json`'s `"hospitals"` block, or hospital-specific env vars — pick one).

### 3. No recurring slot-generation job
Mithra's slots were seeded by a one-off migration (`migrations/0005_mithra_generate_slots.sql`) covering a fixed 7-day window from whenever it was run. There is **no scheduled process** that keeps generating the next day's slots as the window rolls forward. Left as-is, Mithra runs out of bookable slots within days of going live, and every booking attempt fails with `SLOT_UNAVAILABLE` (or `list_available_slots` just returns empty, indistinguishable from "no availability" to the patient). Needs a daily/weekly job (cron, scheduled task, whatever your deploy target supports) that runs the equivalent of `migrations/0005`'s `INSERT ... ON CONFLICT DO NOTHING` logic per active SLOT-mode hospital.

---

## 🟡 Decide deliberately before deploying (not a code bug, an infra choice)

### 4. Sessions are in-memory (`InMemoryRepository`)
A process restart or redeploy loses every active conversation, including anyone mid-booking in the confirm step (`AWAITING_CONFIRM`). It also can't be shared across multiple instances if you deploy behind a load balancer or with autoscaling — two instances would each have their own, inconsistent session state for the same patient. If your deploy target restarts processes routinely or runs >1 instance, this needs a real backing store (Redis, a DB table) before deploy; if it's a single always-on process, it's a known, acceptable limitation for now — just don't be surprised by it.

---

## 🟢 Acceptable as-is — not a blocker, just an onboarding note

### 5. `config/kg_tenants.py`
This is a hardcoded `hospital_id → Neo4j tenant_id` dict, by deliberate choice (avoids touching the production DB — see the conversation that led to this file). Fine for the 2 hospitals that exist today. The obligation it creates going forward: **whenever a 3rd hospital is onboarded, add its line here**, or doctor search for that hospital fails closed (returns "no doctors found" rather than leaking another hospital's doctors — safe failure, but still a functional gap if forgotten). Worth one line in whatever onboarding runbook you keep for adding a new hospital.

---

## Recommended manual test pass before pushing

Run the conversational script below through `interface/app.py` (hospital picker → Mithra) at least once end-to-end — it exercises the real orchestrator/LLM path, not just direct function calls:

1. Book: ask for a cardiologist → pick a doctor → pick a date → get slot times → pick one → confirm with `yes` → verify *Appointment Confirmed* with a time, not a token.
2. Reschedule: ask to reschedule → get new slot times → pick one → confirm → verify *Appointment Rescheduled* wording, not "Confirmed".
3. Cancel: ask to cancel → confirm → verify cancellation message, and that `list_appointments` afterward shows none active.
4. Regression: repeat step 1 on Chaitanya (TOKEN mode) — should get a token number + reporting time, `list_available_slots` should never be called (it's filtered out of the tool list for TOKEN-mode hospitals).

If any of these needs a different test date, check `SELECT DISTINCT date FROM slots WHERE hospital_id='mit-lbn'` first — the generated window moves as item 3 above goes unaddressed.

---

## Summary table

| # | Item | Blocks Mithra going live on real WhatsApp? | Effort |
|---|---|---|---|
| 1 | `whatsapp.py` inbound routing | Yes | Medium — needs Mithra's real `phone_number_id` first |
| 2 | Outbound per-hospital sending | Yes | Small, same PR as #1 |
| 3 | Recurring slot-generation job | Yes (within days) | Small — one scheduled script |
| 4 | Session persistence | Depends on deploy target | Medium, only if needed |
| 5 | `config/kg_tenants.py` maintenance | No | None now — process note only |
