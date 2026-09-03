from unittest.mock import MagicMock

import pytest

import orchestrator.llm as llm_module
from orchestrator.llm import translate_static, translate_labels, CARD_LABELS


@pytest.fixture(autouse=True)
def clear_translation_caches():
    llm_module._STATIC_TRANSLATION_CACHE.clear()
    llm_module._LABEL_CACHE.clear()
    yield
    llm_module._STATIC_TRANSLATION_CACHE.clear()
    llm_module._LABEL_CACHE.clear()


def _adapter_with_completion(text):
    message    = MagicMock(content=text)
    completion = MagicMock(choices=[MagicMock(message=message)])
    adapter = MagicMock()
    adapter.model = "gemini-3.6-flash"
    adapter.client.chat.completions.create.return_value = completion
    return adapter


# ── translate_static ──────────────────────────────────────────────────────

def test_translate_static_calls_llm_only_once_for_repeated_calls():
    adapter = _adapter_with_completion("అర్థమైంది. మీ అభ్యర్థన రద్దు చేయబడింది.")

    first  = translate_static(adapter, "Understood. Your request has been cancelled.", "te-IN")
    second = translate_static(adapter, "Understood. Your request has been cancelled.", "te-IN")

    assert first == second == "అర్థమైంది. మీ అభ్యర్థన రద్దు చేయబడింది."
    adapter.client.chat.completions.create.assert_called_once()  # second call hit the cache


def test_translate_static_does_not_cache_a_failed_translation():
    adapter = MagicMock()
    adapter.model = "gemini-3.6-flash"
    adapter.client.chat.completions.create.side_effect = RuntimeError("down")

    first  = translate_static(adapter, "Understood. Your request has been cancelled.", "te-IN")
    second = translate_static(adapter, "Understood. Your request has been cancelled.", "te-IN")

    assert first == second == "Understood. Your request has been cancelled."
    assert adapter.client.chat.completions.create.call_count == 2  # retried both times, nothing cached


def test_translate_static_caches_separately_per_language():
    adapter = _adapter_with_completion("translated")

    translate_static(adapter, "Understood. Your request has been cancelled.", "te-IN")
    translate_static(adapter, "Understood. Your request has been cancelled.", "hi-IN")

    assert adapter.client.chat.completions.create.call_count == 2


# ── translate_labels ─────────────────────────────────────────────────────

def _numbered_translation(values):
    return "\n".join(f"{i+1}. {v}" for i, v in enumerate(values))


def test_translate_labels_returns_all_translated_keys():
    translated_values = [
        "అపాయింట్‌మెంట్ నిర్ధారించబడింది", "టోకెన్", "డాక్టర్", "విభాగం",
        "ఆసుపత్రి", "చిరునామా", "తేదీ", "రిపోర్టింగ్ సమయం", "ఫీజు",
    ]
    adapter = _adapter_with_completion(_numbered_translation(translated_values))

    labels = translate_labels(adapter, "te-IN")

    assert labels["token"] == "టోకెన్"
    assert labels["doctor"] == "డాక్టర్"
    assert labels["fee"] == "ఫీజు"
    assert set(labels.keys()) == set(CARD_LABELS.keys())


def test_translate_labels_caches_after_first_success():
    adapter = _adapter_with_completion(_numbered_translation(["x"] * len(CARD_LABELS)))

    translate_labels(adapter, "te-IN")
    translate_labels(adapter, "te-IN")

    adapter.client.chat.completions.create.assert_called_once()


def test_translate_labels_falls_back_to_english_on_malformed_output():
    # Wrong number of lines back — can't be safely mapped to the label keys.
    adapter = _adapter_with_completion("1. only one line")

    labels = translate_labels(adapter, "te-IN")

    assert labels == CARD_LABELS
    assert adapter.client.chat.completions.create.call_count == 2  # retried once


def test_translate_labels_skips_llm_call_for_english():
    adapter = _adapter_with_completion("should never be used")

    labels = translate_labels(adapter, "en-IN")

    assert labels == CARD_LABELS
    adapter.client.chat.completions.create.assert_not_called()
