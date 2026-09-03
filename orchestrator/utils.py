import re

_BOOKING_KEYWORDS = {"book", "appointment", "cancel", "token", "schedule", "register", "slot"}
_AFFIRMATIVE      = {"yes", "y", "ok", "okay", "sure", "book", "confirm", "go ahead", "proceed", "yeah", "yep", "do it"}
_NEGATIVE         = {"no", "n", "cancel", "stop", "nope", "never mind", "nevermind", "nah", "don't"}

# Short non-English yes/no words, safe to match as a *substring* of a longer
# transcript — unlike the English words above (checked only via exact match
# in _AFFIRMATIVE/_NEGATIVE, since e.g. "book" or "confirm" as a substring
# would false-positive on a longer reply like "book it for tomorrow instead",
# which is new input, not a plain yes).
_AFFIRMATIVE_SUBSTR = {
    "avunu", "అవును", "సరే",                       # Telugu: yes / ok
    "haan", "han", "हाँ", "हां", "ठीक है", "ठीक",     # Hindi: yes / ok
    "aam", "ஆம்", "சரி",                            # Tamil: yes / ok
    "haudu", "ಹೌದు", "ಸರಿ",                          # Kannada: yes / ok
}
_NEGATIVE_SUBSTR = {
    "vaddu", "వద్దు", "కాదు",                        # Telugu: no / don't want
    "nahi", "nahin", "नहीं",                          # Hindi: no
    "illai", "இல்லை",                                # Tamil: no
    "illa", "ಇಲ್ಲ",                                   # Kannada: no
}


def detect_booking_intent(text: str) -> bool:
    lowered = text.lower()
    return any(kw in lowered for kw in _BOOKING_KEYWORDS)


# Speech-to-text transcripts (unlike typed replies) routinely add terminal
# punctuation to a one-word answer — "yes" comes back as "Yes." or "Yes!".
# Strip that before the exact-match check, or a spoken "yes" silently misses
# _AFFIRMATIVE and falls through to the ambiguous-reply path.
_TRAILING_PUNCT_RE = re.compile(r"[.,!?;:।]+$")


def _normalize_reply(text: str) -> str:
    return _TRAILING_PUNCT_RE.sub("", text.strip()).strip().lower()


def is_affirmative(text: str) -> bool:
    """Exact match for English (a longer reply that merely contains 'book' or
    'confirm', e.g. 'book it for tomorrow instead', is NOT a plain yes — it's
    new input that should go back through the LLM). Non-English yes/no words
    are still matched as a substring since they won't appear inside unrelated
    English sentences."""
    lowered = _normalize_reply(text)
    return lowered in _AFFIRMATIVE or any(kw in lowered for kw in _AFFIRMATIVE_SUBSTR)


def is_negative(text: str) -> bool:
    lowered = _normalize_reply(text)
    return lowered in _NEGATIVE or any(kw in lowered for kw in _NEGATIVE_SUBSTR)


def looks_like_english(text: str) -> bool:
    """Heuristic: true if text has no non-ASCII characters, i.e. it's plain
    Latin-script English rather than one of the supported Indian-language
    scripts (Telugu/Hindi/Tamil/Kannada all use non-ASCII code points).

    Used to catch an LLM reply that slipped into English despite the
    session being in another language — the system prompt asks the model to
    always match the patient's language, but that's a soft instruction, not
    a guarantee."""
    return text.isascii()
