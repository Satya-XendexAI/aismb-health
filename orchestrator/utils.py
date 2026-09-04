_BOOKING_KEYWORDS = {"book", "appointment", "cancel", "token", "schedule", "register", "slot"}


def detect_booking_intent(text: str) -> bool:
    lowered = text.lower()
    return any(kw in lowered for kw in _BOOKING_KEYWORDS)


def looks_like_english(text: str) -> bool:
    """Heuristic: true if text has no non-ASCII characters, i.e. it's plain
    Latin-script English rather than one of the supported Indian-language
    scripts (Telugu/Hindi/Tamil/Kannada all use non-ASCII code points).

    Used to catch an LLM reply that slipped into English despite the
    session being in another language — the system prompt asks the model to
    always match the patient's language, but that's a soft instruction, not
    a guarantee."""
    return text.isascii()
