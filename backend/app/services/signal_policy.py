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
