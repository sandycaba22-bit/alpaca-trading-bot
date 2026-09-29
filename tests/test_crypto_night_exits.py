from datetime import datetime, timezone

from strategies.crypto_night.exits_live import evaluate_exit
from strategies.crypto_night.position_book import NightTradeRecord


def test_partial_at_1r_long():
    trade = NightTradeRecord(
        symbol="BTC/USD",
        side="long",
        variant="V1",
        qty=1.0,
        qty_open=1.0,
        entry_order_id="x",
        entry_price=100.0,
        stop_price=95.0,
        runner_stop=95.0,
        entry_time_utc=datetime.now(timezone.utc).isoformat(),
        status="open",
    )
    decision = evaluate_exit(trade, 105.0, datetime.now(timezone.utc))
    assert decision.action == "partial_1r"
    assert decision.close_qty == 0.5


def test_stop_long():
    trade = NightTradeRecord(
        symbol="BTC/USD",
        side="long",
        variant="V1",
        qty=1.0,
        qty_open=1.0,
        entry_order_id="x",
        entry_price=100.0,
        stop_price=95.0,
        runner_stop=95.0,
        entry_time_utc=datetime.now(timezone.utc).isoformat(),
        status="open",
    )
    decision = evaluate_exit(trade, 94.0, datetime.now(timezone.utc))
    assert decision.action == "close_all"
    assert decision.reason == "stop"
