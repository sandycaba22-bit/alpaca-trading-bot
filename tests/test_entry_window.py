from datetime import datetime
from zoneinfo import ZoneInfo

from bot.market.entry_window import entry_window_allows, parse_stock_entry_window_et

ET = ZoneInfo("America/New_York")


def test_parse_window():
    w = parse_stock_entry_window_et("09:30-13:30")
    assert w is not None
    assert w.label() == "09:30-13:30 ET"


def test_top50_shift():
    w = parse_stock_entry_window_et("09:30-13:30")
    assert entry_window_allows(w, now=datetime(2026, 9, 29, 10, 0, tzinfo=ET))
    assert not entry_window_allows(w, now=datetime(2026, 9, 29, 13, 30, tzinfo=ET))
    assert not entry_window_allows(w, now=datetime(2026, 9, 29, 14, 0, tzinfo=ET))


def test_elite_shift():
    w = parse_stock_entry_window_et("13:30-16:00")
    assert not entry_window_allows(w, now=datetime(2026, 9, 29, 12, 0, tzinfo=ET))
    assert entry_window_allows(w, now=datetime(2026, 9, 29, 13, 30, tzinfo=ET))
    assert entry_window_allows(w, now=datetime(2026, 9, 29, 15, 59, tzinfo=ET))
    assert not entry_window_allows(w, now=datetime(2026, 9, 29, 16, 0, tzinfo=ET))
