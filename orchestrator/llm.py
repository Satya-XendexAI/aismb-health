import json
import logging
import os
import re
from typing import List
from openai import OpenAI
from dotenv import load_dotenv

from models.session import (
    ChatTurn, ChatRole, AgentResponse, AgentResponseType, ToolCall,
)
from prompts.system import PATIENT_SYSTEM_PROMPT

load_dotenv()
logger = logging.getLogger(__name__)

# Occasionally the model returns its raw reasoning instead of a real reply
# (e.g. a response that literally starts with the word "Thought" on its own
# line). Never forward that to a patient — fall back to a safe message instead.
_LEAKED_REASONING_RE = re.compile(r"^\s*thought\s*\n", re.IGNORECASE)


def _looks_like_leaked_reasoning(text: str) -> bool:
    return bool(_LEAKED_REASONING_RE.match(text))


# Fixed WhatsApp templates (booking confirm / confirmation card) are plain
# English by default. LANGUAGE_NAMES maps the language codes Sarvam's
# transcription returns (e.g. "te-IN" -> "te") to a human language name for
# the translation prompt below.
LANGUAGE_NAMES = {
    "te": "Telugu",
    "hi": "Hindi",
    "ta": "Tamil",
    "kn": "Kannada",
    "en": "English",
}


# Numbers/tokens/amounts/dates that must survive a real translation untouched.
# If the model produced a diff, an explanation, or leaked its own reasoning
# instead of a clean translation, these almost never all show up verbatim —
# far more robust than trying to blacklist every broken-output shape.
_INVARIANT_RE = re.compile(r"#\d+|₹\d+|\d{4}-\d{2}-\d{2}|\d{1,2}:\d{2}")


def _looks_like_broken_translation(original: str, translated: str) -> bool:
    if not translated or "`" in translated or "->" in translated:
        return True
    if translated.count("(") != translated.count(")"):
        return True  # e.g. the model stopped generating mid-parenthetical
    return any(literal not in translated for literal in _INVARIANT_RE.findall(original))


def _request_translation(llm: "GeminiLLMAdapter", text: str, language_name: str) -> str:
    completion = llm.client.chat.completions.create(
        model=llm.model,
        messages=[
            {
                "role": "system",
                "content": (
                    f"You translate short WhatsApp messages into {language_name}. "
                    "Reply with ONLY the translated message — no explanation, no "
                    "commentary, no line numbers, no diff or before/after format. "
                    "Keep numbers, dates, the '#' symbol, emoji, and *bold* markers "
                    "exactly as they appear; translate only the English words."
                ),
            },
            {"role": "user", "content": text},
        ],
        temperature=0.1,
        # Generous headroom: this model spends some of its token budget on
        # internal "thinking" even for short prompts, and a tight max_tokens
        # was observed truncating the visible reply mid-sentence.
        max_tokens=4096,
    )
    return (completion.choices[0].message.content or "").strip()


def translate_text(llm: "GeminiLLMAdapter", text: str, language_code: str | None) -> str:
    """Translate a fixed template's labels into language_code, leaving numbers/emoji/dates as-is.

    Falls back to the original English text if the language is English/unknown,
    the translation call fails, or the model's output looks malformed (tried
    twice before giving up, since this is somewhat stochastic).
    """
    lang_prefix = (language_code or "en").split("-")[0].lower()
    language_name = LANGUAGE_NAMES.get(lang_prefix)
    if not language_name or language_name == "English":
        return text

    for attempt in (1, 2):
        try:
            translated = _request_translation(llm, text, language_name)
        except Exception as exc:
            logger.warning("Template translation to %s failed, using English: %s", language_name, exc)
            return text

        if not _looks_like_broken_translation(text, translated):
            return translated
        logger.warning(
            "Translation to %s looked malformed (attempt %d), %r",
            language_name, attempt, translated[:200],
        )

    return text


# Fixed, unchanging messages (no dynamic content) only ever need translating
# ONCE per language, then reused forever — turns an occasional flake into a
# one-time cost instead of a per-message gamble.
_STATIC_TRANSLATION_CACHE: dict[tuple[str, str], str] = {}


def translate_static(llm: "GeminiLLMAdapter", text: str, language_code: str | None) -> str:
    """Like translate_text, but caches successful results for exact, static strings."""
    lang_prefix = (language_code or "en").split("-")[0].lower()
    cache_key = (lang_prefix, text)
    if cache_key in _STATIC_TRANSLATION_CACHE:
        return _STATIC_TRANSLATION_CACHE[cache_key]

    translated = translate_text(llm, text, language_code)
    if translated != text:
        _STATIC_TRANSLATION_CACHE[cache_key] = translated
    return translated


# The booking-confirmation card's field labels — translated as a small batch
# (much easier for the model to get right than a whole rendered card) and
# cached per language, so every booking after the first reuses known-good
# labels instead of re-rolling the dice each time.
CARD_LABELS = {
    "appointment_confirmed": "Appointment Confirmed",
    "token":                 "Token",
    "doctor":                "Doctor",
    "department":            "Department",
    "hospital":               "Hospital",
    "address":                "Address",
    "date":                   "Date",
    "reporting_time":         "Reporting Time",
    "fee":                    "Fee",
}

_LABEL_CACHE: dict[str, dict[str, str]] = {}
_NUMBERED_LINE_RE = re.compile(r"^\s*\d+[.):]\s*(.+)$")


def translate_labels(llm: "GeminiLLMAdapter", language_code: str | None) -> dict[str, str]:
    """Translate CARD_LABELS into language_code once, then return the cached result.

    Falls back to the English labels for any language that hasn't (yet)
    translated successfully — never raises, never blocks a booking.
    """
    lang_prefix = (language_code or "en").split("-")[0].lower()
    language_name = LANGUAGE_NAMES.get(lang_prefix)
    if not language_name or language_name == "English":
        return CARD_LABELS
    if lang_prefix in _LABEL_CACHE:
        return _LABEL_CACHE[lang_prefix]

    keys = list(CARD_LABELS.keys())
    numbered_prompt = "\n".join(f"{i+1}. {CARD_LABELS[k]}" for i, k in enumerate(keys))

    for attempt in (1, 2):
        try:
            completion = llm.client.chat.completions.create(
                model=llm.model,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            f"Translate each numbered WhatsApp UI label into {language_name}. "
                            "Reply with the same numbers, one short translation per line, "
                            "nothing else — no explanation, no extra text."
                        ),
                    },
                    {"role": "user", "content": numbered_prompt},
                ],
                temperature=0.1,
                # Same headroom concern as _request_translation — a tight
                # budget was observed truncating the label list mid-way.
                max_tokens=4096,
            )
            reply = (completion.choices[0].message.content or "").strip()
        except Exception as exc:
            logger.warning("Label translation to %s failed, using English: %s", language_name, exc)
            return CARD_LABELS

        translated_lines = {}
        for line in reply.splitlines():
            m = _NUMBERED_LINE_RE.match(line)
            if m:
                translated_lines[len(translated_lines) + 1] = m.group(1).strip()

        if len(translated_lines) == len(keys) and all(
            v and "`" not in v and "->" not in v for v in translated_lines.values()
        ):
            labels = {keys[i]: translated_lines[i + 1] for i in range(len(keys))}
            _LABEL_CACHE[lang_prefix] = labels
            return labels

        logger.warning("Label translation to %s looked malformed (attempt %d): %r", language_name, attempt, reply[:300])

    return CARD_LABELS


def normalize_to_english(llm: "GeminiLLMAdapter", text: str) -> str:
    """Convert patient-provided data (name, place, symptoms) to English for
    storage, regardless of what language it was spoken in.

    Names/places are transliterated phonetically (never translated by
    meaning — a name isn't a word to translate); phrases/sentences are
    translated by meaning. Already-English text is returned untouched, and
    the original text is kept if the call fails or looks malformed — a
    patient's data is never dropped over a translation hiccup.
    """
    if not text or text.isascii():
        return text

    try:
        completion = llm.client.chat.completions.create(
            model=llm.model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Convert the given text to English. If it is a person's name or "
                        "a place name, transliterate it phonetically into the Latin "
                        "alphabet (do not translate the meaning of a name). If it is a "
                        "phrase or sentence, translate it by meaning. "
                        "Reply with ONLY the converted text — no explanation, no quotes."
                    ),
                },
                {"role": "user", "content": text},
            ],
            temperature=0.1,
            max_tokens=256,
        )
        result = (completion.choices[0].message.content or "").strip()
    except Exception as exc:
        logger.warning("normalize_to_english failed, keeping original: %s", exc)
        return text

    if not result or "`" in result or "->" in result:
        logger.warning("normalize_to_english looked malformed, keeping original: %r", result[:200])
        return text
    return result


class GeminiLLMAdapter:
    def __init__(
        self,
        model:    str = os.getenv("GEMINI_MODEL", "gemini-3.6-flash"),
        base_url: str = "https://generativelanguage.googleapis.com/v1beta/openai/",
        api_key:  str = os.getenv("GEMINI_API_KEY", ""),
    ):
        self.model  = model
        self.client = OpenAI(base_url=base_url, api_key=api_key, timeout=120.0)

    def run_agent(self, history: List[ChatTurn], tool_schemas: list, system_prompt: str = PATIENT_SYSTEM_PROMPT) -> AgentResponse:
        messages = [{"role": "system", "content": system_prompt}] + self._build_messages(history)

        completion = self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            tools=tool_schemas,
            tool_choice="auto",
            temperature=float(os.getenv("GEMINI_TEMPERATURE", "0.3")),
            max_tokens=8192,
            stream=False,
        )

        choice        = completion.choices[0]
        finish_reason = choice.finish_reason
        message       = choice.message


        if finish_reason == "tool_calls" and message.tool_calls:
            tc  = message.tool_calls[0]
            sig = (tc.extra_content or {}).get("google", {}).get("thought_signature")
            return AgentResponse(
                type=AgentResponseType.TOOL_CALL,
                tool_call=ToolCall(
                    tool_name=tc.function.name,
                    args=json.loads(tc.function.arguments),
                    tool_use_id=tc.id,
                    thought_signature=sig,
                ),
            )

        text = message.content or ""
        if _looks_like_leaked_reasoning(text):
            logger.warning("Suppressed a leaked-reasoning LLM response: %r", text[:200])
            text = "Sorry, I'm having trouble with that — could you rephrase or try again?"

        return AgentResponse(type=AgentResponseType.TEXT, text=text)

    def _build_messages(self, history: List[ChatTurn]) -> list:
        messages = []
        for i, turn in enumerate(history):
            if turn.role == ChatRole.USER:
                if messages and messages[-1]["role"] == "user":
                    messages[-1]["content"] += "\n" + turn.content
                else:
                    messages.append({"role": "user", "content": turn.content})

            elif turn.role == ChatRole.ASSISTANT:
                if turn.tool_call:
                    tc_entry = {
                        "id":       turn.tool_call.tool_use_id,
                        "type":     "function",
                        "function": {
                            "name":      turn.tool_call.tool_name,
                            "arguments": json.dumps(turn.tool_call.args),
                        },
                    }
                    if turn.tool_call.thought_signature:
                        tc_entry["extra_content"] = {
                            "google": {"thought_signature": turn.tool_call.thought_signature}
                        }
                    messages.append({
                        "role":       "assistant",
                        "content":    None,
                        "tool_calls": [tc_entry],
                    })
                else:
                    messages.append({"role": "assistant", "content": turn.content or " "})

            elif turn.role == ChatRole.TOOL_RESULT:
                # Prefer the tool_call stored directly on this turn (reliable)
                if turn.tool_call:
                    tool_use_id = turn.tool_call.tool_use_id
                    tool_name   = turn.tool_call.tool_name
                else:
                    # Fallback: search backwards for the matching ASSISTANT tool call
                    tool_use_id = None
                    tool_name   = None
                    for prev_turn in reversed(history[:i]):
                        if prev_turn.role == ChatRole.ASSISTANT and prev_turn.tool_call:
                            tool_use_id = prev_turn.tool_call.tool_use_id
                            tool_name   = prev_turn.tool_call.tool_name
                            break
                # Gemini rejects empty tool name — skip the turn to avoid a 400 error
                if not tool_name or not tool_use_id:
                    continue
                messages.append({
                    "role":         "tool",
                    "tool_call_id": tool_use_id,
                    "name":         tool_name,
                    "content":      turn.content,
                })

        return messages

class PrintWANotifier:
    def send(self, to_number: str, text: str):
        print(f"\n  [→ {to_number}] {text}\n")
