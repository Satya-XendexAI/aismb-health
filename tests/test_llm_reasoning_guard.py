from unittest.mock import MagicMock, patch

from orchestrator.llm import GeminiLLMAdapter, _looks_like_leaked_reasoning

LEAKED_CONTENT = (
    "thought\n"
    "The patient profile is null (`self: null`). So we don't have the patient's name yet!\n"
    "Wait, let's look at the instructions for booking:\n"
    '"Ask for patient_name before calling this tool if not already known."'
)

NORMAL_CONTENT = "I found Dr. Gobu P (Cardiology). Could you share your full name to complete the booking?"


def _fake_completion(content: str):
    message = MagicMock(content=content, tool_calls=None)
    choice  = MagicMock(finish_reason="stop", message=message)
    return MagicMock(choices=[choice])


def test_looks_like_leaked_reasoning_detects_thought_prefix():
    assert _looks_like_leaked_reasoning(LEAKED_CONTENT) is True


def test_looks_like_leaked_reasoning_ignores_normal_replies():
    assert _looks_like_leaked_reasoning(NORMAL_CONTENT) is False


def test_run_agent_replaces_leaked_reasoning_with_safe_fallback():
    adapter = GeminiLLMAdapter(api_key="test-key")
    with patch.object(adapter.client.chat.completions, "create", return_value=_fake_completion(LEAKED_CONTENT)):
        response = adapter.run_agent(history=[], tool_schemas=[])

    assert "thought" not in response.text.lower()
    assert response.text == "Sorry, I'm having trouble with that — could you rephrase or try again?"


def test_run_agent_passes_through_normal_replies_unchanged():
    adapter = GeminiLLMAdapter(api_key="test-key")
    with patch.object(adapter.client.chat.completions, "create", return_value=_fake_completion(NORMAL_CONTENT)):
        response = adapter.run_agent(history=[], tool_schemas=[])

    assert response.text == NORMAL_CONTENT
