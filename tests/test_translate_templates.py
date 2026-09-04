from unittest.mock import MagicMock

from orchestrator.llm import translate_text


def _adapter_with_completion(translated_text):
    message    = MagicMock(content=translated_text)
    choice     = MagicMock(message=message)
    completion = MagicMock(choices=[choice])

    adapter = MagicMock()
    adapter.model = "gemini-3.6-flash"
    adapter.client.chat.completions.create.return_value = completion
    return adapter


def test_translate_text_calls_llm_for_non_english_language():
    adapter = _adapter_with_completion("✅ *అపాయింట్‌మెంట్ నిర్ధారించబడింది*")

    result = translate_text(adapter, "✅ *Appointment Confirmed*", "te-IN")

    assert result == "✅ *అపాయింట్‌మెంట్ నిర్ధారించబడింది*"
    adapter.client.chat.completions.create.assert_called_once()
    messages = adapter.client.chat.completions.create.call_args.kwargs["messages"]
    assert "Telugu" in messages[0]["content"]      # system prompt names the target language
    assert messages[1]["content"] == "✅ *Appointment Confirmed*"  # original text, unmodified


def test_translate_text_skips_llm_call_for_english():
    adapter = _adapter_with_completion("should never be used")

    result = translate_text(adapter, "✅ *Appointment Confirmed*", "en-IN")

    assert result == "✅ *Appointment Confirmed*"
    adapter.client.chat.completions.create.assert_not_called()


def test_translate_text_skips_llm_call_for_none_language():
    adapter = _adapter_with_completion("should never be used")

    result = translate_text(adapter, "✅ *Appointment Confirmed*", None)

    assert result == "✅ *Appointment Confirmed*"
    adapter.client.chat.completions.create.assert_not_called()


def test_translate_text_falls_back_to_english_on_failure():
    adapter = MagicMock()
    adapter.model = "gemini-3.6-flash"
    adapter.client.chat.completions.create.side_effect = RuntimeError("API down")

    result = translate_text(adapter, "✅ *Appointment Confirmed*", "hi-IN")

    assert result == "✅ *Appointment Confirmed*"


def test_translate_text_ignores_unknown_language_code():
    adapter = _adapter_with_completion("should never be used")

    result = translate_text(adapter, "✅ *Appointment Confirmed*", "fr-FR")

    assert result == "✅ *Appointment Confirmed*"
    adapter.client.chat.completions.create.assert_not_called()


def test_translate_text_falls_back_when_model_returns_a_diff_instead_of_a_translation():
    # Reproduces a real failure seen against the live API: the model
    # sometimes replies with a line-by-line "before -> after" explanation
    # instead of the translated message itself.
    adapter = _adapter_with_completion("Line 9: `💰 *Fee:* ₹2000` -> `💰 *ఫీజు:* ₹2000`")

    result = translate_text(adapter, "✅ *Appointment Confirmed*", "te-IN")

    assert result == "✅ *Appointment Confirmed*"
    assert adapter.client.chat.completions.create.call_count == 2  # retried once before giving up


def test_translate_text_falls_back_when_model_invents_an_extra_instruction():
    # Reproduces a real failure seen in production: translating a plain
    # confirm prompt ("Please confirm: BOOK appointment with Dr. X" — no
    # formatting instructions anywhere in it, no date/time/# to trip the
    # existing invariant check) came back with a fabricated extra sentence
    # telling the patient to reply in a specific format, using "**"
    # markers the source never had. Patients should never be told to
    # follow a reply format the system doesn't actually require —
    # classify_confirm_reply already understands natural replies.
    adapter = _adapter_with_completion(
        "దయచేసి కన్ఫర్మ్ చేయండి: డాక్టర్ X తో అపాయింట్మెంట్ బుక్ చేయండి "
        "ఫార్మాటింగ్ కోసం**: మార్చుబడిన సెక్స్‌న్తో మాత్రమే రిప్లై ఇవ్వండి"
    )

    result = translate_text(adapter, "Please confirm: BOOK appointment with Dr. X", "te-IN")

    assert result == "Please confirm: BOOK appointment with Dr. X"
    assert adapter.client.chat.completions.create.call_count == 2  # retried once before giving up


def test_translate_text_falls_back_when_model_drops_the_fee_and_token_numbers():
    # Reproduces another real failure: the model leaks its own word-choice
    # reasoning ("let's use X or Y...") instead of translating — no backtick
    # or "->", but the real token/fee numbers are missing from the output.
    adapter = _adapter_with_completion('"ఫీజు" is fine, let\'s use "ఫీజు" or "రుసుము". Let\'s go with "ఫీజు:".')

    result = translate_text(
        adapter,
        "✅ *Appointment Confirmed*\n🎫 *Token:* #6\n💰 *Fee:* ₹2000",
        "te-IN",
    )

    assert result == "✅ *Appointment Confirmed*\n🎫 *Token:* #6\n💰 *Fee:* ₹2000"


def test_translate_text_falls_back_when_model_truncates_mid_parenthetical():
    # Reproduces a real failure: the model stopped generating right after an
    # opening "(" for the department name, dropping the rest of the message.
    adapter = _adapter_with_completion("దయచేసి ధృవీకరించండి: ... సుసాన్ జార్జ్ (కార్డియాలజీ")

    result = translate_text(
        adapter,
        "Please confirm: BOOK appointment with Susan George (Cardiology) on 2026-09-02",
        "te-IN",
    )

    assert result == "Please confirm: BOOK appointment with Susan George (Cardiology) on 2026-09-02"


def test_translate_text_retries_and_succeeds_on_second_attempt():
    adapter = _adapter_with_completion("placeholder")  # overridden by side_effect below
    adapter.client.chat.completions.create.side_effect = [
        MagicMock(choices=[MagicMock(message=MagicMock(content="Line 1: `#6` -> broken"))]),
        MagicMock(choices=[MagicMock(message=MagicMock(content="✅ *అపాయింట్‌మెంట్ నిర్ధారించబడింది* #6"))]),
    ]

    result = translate_text(adapter, "✅ *Appointment Confirmed* #6", "te-IN")

    assert result == "✅ *అపాయింట్‌మెంట్ నిర్ధారించబడింది* #6"
    assert adapter.client.chat.completions.create.call_count == 2
