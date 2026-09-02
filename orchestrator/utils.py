_BOOKING_KEYWORDS = {"book", "appointment", "cancel", "token", "schedule", "register", "slot"}
_AFFIRMATIVE      = {"yes", "y", "ok", "okay", "sure", "book", "confirm", "go ahead", "proceed", "yeah", "yep", "do it"}
_NEGATIVE         = {"no", "n", "cancel", "stop", "nope", "never mind", "nevermind", "nah", "don't"}


def detect_booking_intent(text: str) -> bool:
    lowered = text.lower()
    return any(kw in lowered for kw in _BOOKING_KEYWORDS)


def is_affirmative(text: str) -> bool:
    """Exact match only — a longer reply that merely contains a word like
    'book' or 'confirm' (e.g. 'book it for tomorrow instead') is NOT a plain
    yes, it's new input that should go back through the LLM to be understood."""
    return text.strip().lower() in _AFFIRMATIVE


def is_negative(text: str) -> bool:
    return text.strip().lower() in _NEGATIVE
