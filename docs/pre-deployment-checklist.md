# Pre-Deployment Checklist — Multi-Hospital (Token + Slot) Rollout

Everything below the line is **already implemented and tested** this session (slot booking/reschedule/cancel, the `list_appointments`/`memory_tool` union, the kg_retriever tenant-scoping fix). This file is the gap list — what's still hardcoded, stubbed, or missing between "works when I test it locally" and "safe to push to GitHub and deploy."

---

## 🔴 Must fix before Mithra can receive real WhatsApp traffic

### 1. `whatsapp.py` inbound routing — ✅ RESOLVED, via a simpler mechanism than originally planned
Originally this item called for `phone_number_id`-based routing (a real, separate WhatsApp Business number for Mithra). Since this doc was first written, a **simpler alternative shipped instead**: `hospitals.is_live_number` (see `migrations/0007_add_live_hospital_flag.sql`) — one boolean flag, exactly one hospital `true` at a time, looked up fresh on every message in `whatsapp.py`'s `receive_webhook()` ([whatsapp.py:132-136](../whatsapp.py#L132)). No more hardcoded `HOSPITAL_ID` constant. Switching which hospital your one real number represents is now two `UPDATE` statements, no code change, no restart:
```sql
UPDATE hospitals SET is_live_number = false WHERE hospital_id = 'glngs-chn';
UPDATE hospitals SET is_live_number = true  WHERE hospital_id = 'mit-lbn';
```
Tested both directions plus the fail-closed "nobody live" case against the real FastAPI app.

**Important limitation this doesn't remove:** only **one** hospital can ever be live at a time on this one number — this is a testing/staging mechanism, not true simultaneous multi-hospital service. If you eventually want **both** hospitals receiving real traffic *at the same time*, you're back to needing a second real WhatsApp number and the original `phone_number_id`-based plan in `docs/multi-hospital-identity-routing-plan.md` — including its `get_role`/`get_doctor_config`/`get_admin_config` hospital-scoping fix, which is genuinely unneeded right now (only one hospital is ever active globally, so there's no cross-hospital doctor/admin ambiguity to resolve) but would become necessary again at that point.

### 2. Outbound replies are also single-hospital — ✅ N/A under the current approach
This was originally a real gap (replying to a Mithra patient from Chaitanya's number). With the `is_live_number` approach, it's moot: it's the same one physical number and token sending regardless of which hospital is live, so there's nothing to fix here **unless/until** you get a second real number for true simultaneous multi-hospital service (see the limitation above) — at which point this becomes relevant again.

### 3. No recurring slot-generation job — still open
`migrations/0006_regenerate_slots_rolling_window.sql` was run manually (10-day window, all SLOT-mode hospitals, doctor-hours-driven — an improvement over the original `0005`, which only covered Mithra with hardcoded hours). But it's still a **one-off manual run**, not a scheduled job. Nothing re-runs it automatically as the window ages. Needs a daily/weekly cron (or whatever your deploy target supports) pointed at that same script's `INSERT ... ON CONFLICT DO NOTHING` logic.

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
| 1 | `whatsapp.py` inbound routing | ✅ Resolved (single-number, one-at-a-time) | Only re-opens if you need *simultaneous* multi-hospital service |
| 2 | Outbound per-hospital sending | ✅ N/A under current approach | Same re-open condition as #1 |
| 3 | Recurring slot-generation job | Yes (within days) | Small — one scheduled script |
| 4 | Session persistence | Depends on deploy target | Medium, only if needed |
| 5 | `config/kg_tenants.py` maintenance | No | None now — process note only |
