from orchestrator.formatters import format_booking_result


def _confirmed_result(**overrides):
    booking = {
        "status": "CONFIRMED", "token_number": 1, "doctor_name": "R.Ramachandran",
        "department": "Dermatology", "hospital_name": "Test Hospital",
        "hospital_address": "", "fee": 800, "estimated_time": "2026-09-08T17:00:00",
    }
    booking.update(overrides)
    return {"action": "BOOK", "result": booking}


def test_booking_card_includes_patient_name():
    result = _confirmed_result(patient_name="Geetha")
    card = format_booking_result(result, tool_args={"date": "2026-09-08"})

    assert "Geetha" in card
    assert "Patient Name" in card


def test_booking_card_shows_relation_for_a_family_member():
    result = _confirmed_result(patient_name="Savitri", relation_to_requester="mother")
    card = format_booking_result(result, tool_args={"date": "2026-09-08"})

    assert "Savitri (mother)" in card


def test_booking_card_omits_relation_suffix_for_self():
    result = _confirmed_result(patient_name="Geetha", relation_to_requester="self")
    card = format_booking_result(result, tool_args={"date": "2026-09-08"})

    assert "Geetha" in card
    assert "(self)" not in card


def test_booking_card_omits_patient_line_when_name_missing():
    result = _confirmed_result()
    card = format_booking_result(result, tool_args={"date": "2026-09-08"})

    assert "Patient Name" not in card


def test_booking_card_uses_translated_patient_label():
    result = _confirmed_result(patient_name="Geetha")
    card = format_booking_result(
        result, tool_args={"date": "2026-09-08"}, labels={"patient": "రోగి పేరు"},
    )

    assert "రోగి పేరు" in card
    assert "Geetha" in card
