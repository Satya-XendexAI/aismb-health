import pytest

from orchestrator.utils import is_affirmative, is_negative


@pytest.mark.parametrize("text", [
    "yes", "Yes", "ok", "sure", "yeah",
    "అవును", "సరే",                # Telugu
    "haan", "हाँ", "ठीक है",        # Hindi
    "aam", "ஆம்", "சரி",           # Tamil
    "haudu", "ಹೌದు", "ಸರಿ",         # Kannada
])
def test_is_affirmative_recognizes_yes_in_every_supported_language(text):
    assert is_affirmative(text) is True


@pytest.mark.parametrize("text", [
    "no", "No", "cancel", "stop", "nope",
    "వద్దు", "కాదు",                # Telugu
    "nahi", "नहीं",                 # Hindi
    "illai", "இல்லை",              # Tamil
    "illa", "ಇಲ್ಲ",                 # Kannada
])
def test_is_negative_recognizes_no_in_every_supported_language(text):
    assert is_negative(text) is True


def test_is_affirmative_false_for_unrelated_text():
    assert is_affirmative("what is the doctor's fee") is False


def test_is_negative_false_for_unrelated_text():
    assert is_negative("what time does the clinic open") is False
