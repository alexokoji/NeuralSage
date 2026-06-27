from app.services.signal_policy import (
    get_entry_confidence_threshold,
    should_execute_entry_signal,
    should_prefer_screener,
)


def test_fallback_entry_threshold_is_more_active_when_ai_is_unavailable() -> None:
    assert get_entry_confidence_threshold(protect_mode=False, ai_available=False) == 0.30
    assert should_execute_entry_signal(0.30, protect_mode=False, ai_available=False) is True


def test_protect_mode_requires_stronger_confidence() -> None:
    assert get_entry_confidence_threshold(protect_mode=True, ai_available=False) == 0.45
    assert should_execute_entry_signal(0.44, protect_mode=True, ai_available=False) is False


def test_screener_can_outrank_ai_when_confidence_gap_is_large() -> None:
    assert should_prefer_screener(0.72, 0.60, screener_advantage_delta=0.10) is True
    assert should_prefer_screener(0.69, 0.60, screener_advantage_delta=0.10) is False
