from __future__ import annotations


def get_entry_confidence_threshold(*, protect_mode: bool, ai_available: bool) -> float:
    """Return the minimum confidence required for an entry signal.

    The trading engine previously rejected many valid signals because it used a
    hard 0.60 threshold in protect mode and 0.40 otherwise, even when AI was
    unavailable and the screener had already identified a strong opportunity.
    We keep protect mode stricter, but allow the engine to act on meaningful
    signals when the fallback path is being used.
    """
    if protect_mode:
        return 0.45
    if ai_available:
        return 0.40
    return 0.30


def should_execute_entry_signal(confidence: float, *, protect_mode: bool, ai_available: bool) -> bool:
    return confidence >= get_entry_confidence_threshold(protect_mode=protect_mode, ai_available=ai_available)


def should_prefer_screener(
    screener_confidence: float,
    ai_confidence: float,
    *,
    screener_advantage_delta: float,
) -> bool:
    """Return True when the screener should override the AI signal.

    The hybrid policy prefers the AI recommendation by default, but allows the
    screener to win when it is meaningfully stronger than the AI output.
    """
    return screener_confidence - ai_confidence >= screener_advantage_delta
