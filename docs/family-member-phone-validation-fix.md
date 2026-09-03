# Family-Member Phone Number Validation — Implementation Plan

**Problem:** When booking on behalf of a family member with their own contact number, the system accepts anything the LLM extracts as a phone number — including a 5- or 8-digit string, as seen live ("88977295" was accepted with no complaint). Nothing in the pipeline checks length or format.

**Fix:** Validate at the same place every other booking business-rule is enforced (`tools/appointment/booking.py`), reusing the file's existing `ErrorResult` pattern. Accept a plain 10-digit number *or* a 10-digit number prefixed with the `91` India country code (with or without a `+`); reject anything else and ask the patient to resend the correct number.

**Files touched:** 1 existing file (`tools/appointment/booking.py`), 1 optional line in `prompts/system.py`. No new files, no schema/model changes.

---

## 1. `tools/appointment/booking.py`

### 1a. New helper — add after `_check_supported_mode` (currently ends at line 19)

```python
def _normalize_phone(raw: str) -> str:
    """Strip spaces, dashes, +, parentheses; drop a leading '91' country
    code on a 12-digit number. Returns digits only — caller checks length."""
    digits = "".join(ch for ch in raw if ch.isdigit())
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    return digits
```

Examples this handles:
| Input | Normalized | Valid? |
|---|---|---|
| `8897729577` | `8897729577` | ✅ |
| `+91 8897729577` | `8897729577` (91 stripped) | ✅ |
| `91-8897-729-577` | `8897729577` (91 stripped) | ✅ |
| `88977295` | `88977295` | ❌ (8 digits) |
| `9188977295` | `9188977295` | ❌ (10 digits but not stripped — doesn't start a 12-digit number, so this is treated as a real 10-digit number as typed, which is correct: stripping only ever applies to a 12-digit input) |

### 1b. Validation check inside `book()` — insert after the doctor check (currently ends at line 49), *before* patient resolution (currently starts at line 51, since `patient_phone` is consumed at line 62)

```python
    # Validate alternate contact number, if the family member has their own
    patient_phone = payload.patient_phone
    if patient_phone:
        patient_phone = _normalize_phone(patient_phone)
        if len(patient_phone) != 10:
            return ErrorResult(
                status="ERROR", error_code="INVALID_PHONE",
                message=(
                    f"That doesn't look like a valid number for {payload.patient_name} "
                    f"— could you please give me the correct, full 10-digit mobile number?"
                ),
            )
```

### 1c. Use the normalized value when storing — change line 62

```python
# before
phone=payload.patient_phone or payload.requester_phone,
# after
phone=patient_phone or payload.requester_phone,
```

So `+91 8897729577` gets stored in the `patients.phone` column as a clean `8897729577`, not with the country code or punctuation baked in.

---

## 2. `prompts/system.py` — optional but recommended (matches the project's existing two-layer pattern: deterministic backend check + prompt reinforcement, same approach used for the kg_retriever retry-storm fix)

In the `"IF patient books appointment:"` block, after the existing phone-number line (`"If they give a different number, set it as patient_phone.\n"`), add:

```python
"If the appointment tool returns error_code INVALID_PHONE, tell the patient their "
"number doesn't look right and ask them to resend the correct 10-digit number — "
"do not proceed to book until a valid one is given.\n"
```

This isn't required for correctness (the backend check in `booking.py` already guarantees no invalid number gets stored, regardless of what the LLM does), but it makes sure the LLM's re-ask is phrased clearly and immediately rather than left to chance, consistent with how the `kg_retriever` empty-result case was handled.

---

## 3. Why this shape

- **No hardcoding beyond the one stated rule** (10 digits, optional `91` prefix) — no hospital-specific or doctor-specific logic, works identically for any family member on any booking.
- **Reuses the existing error-handling path exactly** — `INVALID_PHONE` is a new `error_code` but follows the identical `ErrorResult(status="ERROR", error_code=..., message=...)` shape as `HOSPITAL_NOT_FOUND` / `DOCTOR_NOT_FOUND` / `DUPLICATE_BOOKING` already in this file, so it flows through `tools/appointment/__init__.py`'s `handle_request()` and the orchestrator's ReAct loop with zero additional plumbing — the LLM already knows how to turn any `ErrorResult` into a spoken response to the patient.
- **Only touches the family-member path.** `requester_phone` (the real WhatsApp sender) is never validated or touched — it's inherently a real number from the webhook, not free text.
- **Skipped entirely when not needed** — if the patient says "use my number," `payload.patient_phone` stays `None`, the whole check is bypassed, exactly like today.

## 4. Verification checklist

- [ ] Book for a family member giving a plain 10-digit number — succeeds, stored as-is.
- [ ] Book for a family member giving `+91 8897729577` — succeeds, stored as `8897729577` (country code stripped).
- [ ] Book for a family member giving an 8-digit number (e.g. `88977295`, the reported bug) — bot asks for the correct 10-digit number, no booking created.
- [ ] Book for self (no alternate number given) — unaffected, works exactly as before.
- [ ] Confirm the `patients.phone` column never contains spaces, dashes, `+`, or a leading `91` after this change.
