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


def is_affirmative(text: str) -> bool:
    """Exact match for English (a longer reply that merely contains 'book' or
    'confirm', e.g. 'book it for tomorrow instead', is NOT a plain yes — it's
    new input that should go back through the LLM). Non-English yes/no words
    are still matched as a substring since they won't appear inside unrelated
    English sentences."""
    lowered = text.strip().lower()
    return lowered in _AFFIRMATIVE or any(kw in lowered for kw in _AFFIRMATIVE_SUBSTR)


def is_negative(text: str) -> bool:
    lowered = text.strip().lower()
    return lowered in _NEGATIVE or any(kw in lowered for kw in _NEGATIVE_SUBSTR)
