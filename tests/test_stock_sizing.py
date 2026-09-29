"""Tests sizing paper vs caps legacy live."""

from bot.risk.stock_sizing import (
    LEGACY_TINY_MAX_NOTIONAL,
    resolve_max_notional_per_order,
    resolve_position_size_pct,
    resolve_risk_percent_per_trade,
)


def test_paper_legacy_max_notional_upgraded(monkeypatch):
    monkeypatch.setenv("MAX_NOTIONAL_PER_ORDER", "40")
    assert (
        resolve_max_notional_per_order(paper=True, bot_profile="stocks_top50") == 12_000.0
    )
    assert resolve_max_notional_per_order(paper=True, bot_profile="stocks") == 12_000.0


def test_live_tiny_max_notional_kept(monkeypatch):
    monkeypatch.setenv("MAX_NOTIONAL_PER_ORDER", "45")
    assert resolve_max_notional_per_order(paper=False, bot_profile="stocks") == 45.0


def test_paper_explicit_large_max_honored(monkeypatch):
    monkeypatch.setenv("MAX_NOTIONAL_PER_ORDER", "8000")
    assert resolve_max_notional_per_order(paper=True, bot_profile="stocks_top50") == 8000.0


def test_paper_default_risk_by_profile(monkeypatch):
    monkeypatch.delenv("RISK_PERCENT_PER_TRADE", raising=False)
    assert resolve_risk_percent_per_trade(paper=True, bot_profile="stocks_top50") == 0.005
    assert resolve_risk_percent_per_trade(paper=True, bot_profile="stocks") == 0.01


def test_paper_high_position_pct_from_live_template(monkeypatch):
    monkeypatch.setenv("POSITION_SIZE_PCT", "0.14")
    assert resolve_position_size_pct(paper=True, bot_profile="stocks") == 0.04


def test_legacy_threshold():
    assert LEGACY_TINY_MAX_NOTIONAL == 100.0
