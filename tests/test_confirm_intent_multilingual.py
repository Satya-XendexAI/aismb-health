from unittest.mock import MagicMock

from orchestrator.llm import classify_confirm_reply


def _llm_returning(reply_text: str) -> MagicMock:
    llm = MagicMock()
    completion = MagicMock()
    completion.choices[0].message.content = reply_text
    llm.client.chat.completions.create.return_value = completion
    return llm


def test_classify_confirm_reply_returns_yes_for_a_yes_classification():
    llm = _llm_returning("YES")
    assert classify_confirm_reply(llm, "avunu") == "yes"


def test_classify_confirm_reply_returns_no_for_a_no_classification():
    llm = _llm_returning("NO")
    assert classify_confirm_reply(llm, "వద్దు") == "no"


def test_classify_confirm_reply_returns_unclear_for_anything_else():
    llm = _llm_returning("UNCLEAR")
    assert classify_confirm_reply(llm, "what is the doctor's fee") == "unclear"


def test_classify_confirm_reply_is_case_and_whitespace_tolerant():
    llm = _llm_returning("  yes\n")
    assert classify_confirm_reply(llm, "sure") == "yes"


def test_classify_confirm_reply_falls_back_to_unclear_on_malformed_output():
    """A pending action must never be treated as confirmed just because the
    model's output didn't parse cleanly."""
    llm = _llm_returning("I think they mean yes but I'm not fully sure")
    assert classify_confirm_reply(llm, "hmm") == "unclear"


def test_classify_confirm_reply_falls_back_to_unclear_on_llm_failure():
    llm = MagicMock()
    llm.client.chat.completions.create.side_effect = RuntimeError("network down")
    assert classify_confirm_reply(llm, "yes") == "unclear"


def test_classify_confirm_reply_includes_pending_action_context_in_prompt():
    """Passing the pending action lets the model resolve replies like
    'cancel' meaning YES when the pending action is itself a cancellation."""
    llm = _llm_returning("YES")
    classify_confirm_reply(llm, "cancel", pending_action="CANCEL appointment with Dr. Susan George")

    sent_messages = llm.client.chat.completions.create.call_args.kwargs["messages"]
    system_content = sent_messages[0]["content"]
    assert "CANCEL appointment with Dr. Susan George" in system_content
