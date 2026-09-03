_BOOKING_KEYWORDS = {"book", "appointment", "cancel", "token", "schedule", "register", "slot"}
_AFFIRMATIVE      = {"yes", "y", "ok", "okay", "sure", "book", "confirm", "go ahead", "proceed", "yeah", "yep", "do it"}
_NEGATIVE         = {"no", "n", "cancel", "stop", "nope", "never mind", "nevermind", "nah", "don't"}

# Words safe to match as a *substring* of a longer transcript (unlike "y"/"n",
# which are only checked for an exact whole-message match above — matching
# those as substrings would false-positive on words like "any" or "not").
_AFFIRMATIVE_SUBSTR = {
    "yes", "book", "confirm", "go ahead", "proceed", "sure", "ok",
    "avunu", "అవును", "సరే",                       # Telugu: yes / ok
    "haan", "han", "हाँ", "हां", "ठीक है", "ठीक",     # Hindi: yes / ok
    "aam", "ஆம்", "சரி",                            # Tamil: yes / ok
    "haudu", "ಹೌದు", "ಸರಿ",                          # Kannada: yes / ok
}
_NEGATIVE_SUBSTR = {
    "no", "cancel", "stop", "don't",
    "vaddu", "వద్దు", "కాదు",                        # Telugu: no / don't want
    "nahi", "nahin", "नहीं",                          # Hindi: no
    "illai", "இல்லை",                                # Tamil: no
    "illa", "ಇಲ್ಲ",                                   # Kannada: no
}


def detect_booking_intent(text: str) -> bool:
    lowered = text.lower()
    return any(kw in lowered for kw in _BOOKING_KEYWORDS)


def is_affirmative(text: str) -> bool:
    lowered = text.strip().lower()
    return lowered in _AFFIRMATIVE or any(kw in lowered for kw in _AFFIRMATIVE_SUBSTR)


def is_negative(text: str) -> bool:
    lowered = text.strip().lower()
    return lowered in _NEGATIVE or any(kw in lowered for kw in _NEGATIVE_SUBSTR)
