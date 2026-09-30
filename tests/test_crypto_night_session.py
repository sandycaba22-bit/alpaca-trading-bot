from datetime import datetime, timezone

from strategies.crypto_night.session import (
    entries_allowed,
    in_night_trading_window,
    should_flatten_crypto_positions,
)


def test_always_mode_weekend_saturday():
    sat = datetime(2026, 10, 3, 15, 0, tzinfo=timezone.utc)
    assert in_night_trading_window(sat, session_mode="always")
    assert entries_allowed(sat, session_mode="always")
    assert not should_flatten_crypto_positions(sat, session_mode="always")


def test_night_mode_blocks_us_regular_session():
    mon_noon = datetime(2026, 9, 28, 16, 0, tzinfo=timezone.utc)
    assert not in_night_trading_window(mon_noon, session_mode="night")
