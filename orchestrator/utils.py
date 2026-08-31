_BOOKING_KEYWORDS = {"book", "appointment", "cancel", "token", "schedule", "register", "slot"}
_AFFIRMATIVE      = {"yes", "y", "ok", "okay", "sure", "book", "confirm", "go ahead", "proceed", "yeah", "yep", "do it"}
_NEGATIVE         = {"no", "n", "cancel", "stop", "nope", "never mind", "nevermind", "nah", "don't"}


def detect_booking_intent(text: str) -> bool:
    lowered = text.lower()
    return any(kw in lowered for kw in _BOOKING_KEYWORDS)


def is_affirmative(text: str) -> bool:
    lowered = text.strip().lower()
    return lowered in _AFFIRMATIVE or any(
        kw in lowered for kw in {"yes", "book", "confirm", "go ahead", "proceed", "sure", "ok"}
    )


def is_negative(text: str) -> bool:
    lowered = text.strip().lower()
    return lowered in _NEGATIVE or any(kw in lowered for kw in {"no", "cancel", "stop", "don't"})
