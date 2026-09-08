from unittest.mock import MagicMock, patch

import httpx
import openai

from models.session import ChatTurn, ChatRole, ToolCall
from orchestrator.core import WhatsAppOrchestrator, _trim_history
from orchestrator.llm import GeminiLLMAdapter


def _api_error(exc_class, status_code):
    request  = httpx.Request("POST", "http://test")
    response = httpx.Response(status_code, request=request)
    return exc_class("boom", response=response, body=None)


def _orchestrator():
    return WhatsAppOrchestrator(llm=MagicMock(), notifier=MagicMock(), repository=MagicMock())


# ── _llm_call_with_retry: retry by real exception type, not message text ──

def test_retries_on_a_genuinely_transient_rate_limit_error():
    orchestrator = _orchestrator()
    orchestrator.llm.run_agent.side_effect = [
        _api_error(openai.RateLimitError, 429),
        "final response",
    ]

    with patch("orchestrator.core.time.sleep"):
        result = orchestrator._llm_call_with_retry([], [], "system", max_retries=3)

    assert result == "final response"
    assert orchestrator.llm.run_agent.call_count == 2


def test_does_not_retry_a_bad_request_error_even_if_its_message_contains_the_word_rate():
    # Regression: the old text-matching check ("rate" in str(exc).lower())
    # was tricked by the word "GenerateContentRequest" in a real Gemini
    # 400 error, wasting two retries on a request that could never
    # succeed no matter how many times it was resent.
    orchestrator = _orchestrator()
    orchestrator.llm.run_agent.side_effect = _api_error(
        openai.BadRequestError, 400,
    )
    # Overwrite the message directly to guarantee it contains "rate" via
    # "GenerateContentRequest", matching the real error seen in production.
    orchestrator.llm.run_agent.side_effect.args = (
        "GenerateContentRequest.contents[0].parts[0].function_response.name: Name cannot be empty.",
    )

    result = orchestrator._llm_call_with_retry([], [], "system", max_retries=3)

    assert result is None
    orchestrator.llm.run_agent.assert_called_once()  # no retries — failed fast


def test_retries_on_internal_server_error():
    orchestrator = _orchestrator()
    orchestrator.llm.run_agent.side_effect = [
        _api_error(openai.InternalServerError, 503),
        "final response",
    ]

    with patch("orchestrator.core.time.sleep"):
        result = orchestrator._llm_call_with_retry([], [], "system", max_retries=3)

    assert result == "final response"
    assert orchestrator.llm.run_agent.call_count == 2


def test_gives_up_after_max_retries_on_persistent_transient_errors():
    orchestrator = _orchestrator()
    orchestrator.llm.run_agent.side_effect = _api_error(openai.RateLimitError, 429)

    with patch("orchestrator.core.time.sleep"):
        result = orchestrator._llm_call_with_retry([], [], "system", max_retries=3)

    assert result is None
    assert orchestrator.llm.run_agent.call_count == 3


# ── _trim_history: never leave a dangling tool-result at the front ────────

def test_trim_history_drops_a_leading_tool_result_left_by_the_cut():
    tool_call = ToolCall(tool_name="kg_retriever", args={}, tool_use_id="call_1")
    history = [
        ChatTurn(role=ChatRole.USER, content="old message"),                                    # dropped by slice
        ChatTurn(role=ChatRole.ASSISTANT, content="", tool_call=tool_call),                      # dropped by slice
        ChatTurn(role=ChatRole.TOOL_RESULT, content="{}", tool_call=tool_call),                  # orphaned by slice
        ChatTurn(role=ChatRole.USER, content="next message"),
        ChatTurn(role=ChatRole.ASSISTANT, content="reply"),
    ]

    trimmed = _trim_history(history, max_turns=3)

    assert trimmed == history[-2:]
    assert all(t.role != ChatRole.TOOL_RESULT for t in trimmed)


def test_trim_history_keeps_a_complete_tool_call_result_pair():
    tool_call = ToolCall(tool_name="kg_retriever", args={}, tool_use_id="call_1")
    history = [
        ChatTurn(role=ChatRole.USER, content="find a doctor"),
        ChatTurn(role=ChatRole.ASSISTANT, content="", tool_call=tool_call),
        ChatTurn(role=ChatRole.TOOL_RESULT, content="{}", tool_call=tool_call),
    ]

    trimmed = _trim_history(history, max_turns=3)

    assert trimmed == history


def test_trim_history_is_a_no_op_for_short_history():
    history = [ChatTurn(role=ChatRole.USER, content="hi")]
    assert _trim_history(history, max_turns=10) == history


# ── _build_messages: never send a malformed tool call/result ──────────────

def test_build_messages_skips_an_assistant_tool_call_with_no_name():
    adapter = GeminiLLMAdapter.__new__(GeminiLLMAdapter)  # no API client needed for this
    broken_call = ToolCall(tool_name="", args={}, tool_use_id="")
    history = [
        ChatTurn(role=ChatRole.USER, content="hi"),
        ChatTurn(role=ChatRole.ASSISTANT, content="", tool_call=broken_call),
    ]

    messages = adapter._build_messages(history)

    assert len(messages) == 1
    assert messages[0]["role"] == "user"


def test_build_messages_skips_a_tool_result_with_no_matching_tool_call():
    adapter = GeminiLLMAdapter.__new__(GeminiLLMAdapter)
    broken_call = ToolCall(tool_name="", args={}, tool_use_id="")
    history = [
        ChatTurn(role=ChatRole.TOOL_RESULT, content="{}", tool_call=broken_call),
        ChatTurn(role=ChatRole.USER, content="hi"),
    ]

    messages = adapter._build_messages(history)

    assert len(messages) == 1
    assert messages[0]["role"] == "user"
