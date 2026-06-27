from app.services.signal_policy import get_entry_confidence_threshold, should_execute_entry_signal


def test_fallback_entry_threshold_is_more_active_when_ai_is_unavailable() -> None:
    assert get_entry_confidence_threshold(protect_mode=False, ai_available=False) == 0.30
    assert should_execute_entry_signal(0.30, protect_mode=False, ai_available=False) is True


def test_protect_mode_requires_stronger_confidence() -> None:
    assert get_entry_confidence_threshold(protect_mode=True, ai_available=False) == 0.45
    assert should_execute_entry_signal(0.44, protect_mode=True, ai_available=False) is False
