"""Pruebas offline de ejecucion, precision y protecciones."""

import os
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pandas as pd
from alpaca.common.exceptions import APIError
from alpaca.trading.enums import OrderSide, TimeInForce
from requests import Response
from requests.exceptions import HTTPError

from bot.alpaca.execution import OrderExecutor, OrderSubmitResult, classify_order
from bot.config import STOCK_UNIVERSE, load_settings
from bot.engine import PendingExecution, TradingEngine
from bot.market.assets import all_symbols
from bot.market.mode import TradingMode, resolve_trading_mode
from bot.risk.manager import RiskManager
from bot.risk.stops import StopTakeProfitPolicy
from bot.security.exceptions import ValidationError
from bot.security.sanitize import sanitize_qty, sanitize_symbols
from bot.storage.pending_orders import PendingOrderBook, RestingOrder
from bot.storage.positions import OpenPositionBook
from bot.strategy.base import Signal
from bot.strategy.breakout import detect_breakout
from bot.strategy.signal_filters import SignalFilterLayer


def settings(**env):
    with patch.dict(os.environ, {
        "APCA_API_KEY_ID": "PKtest",
        "APCA_API_SECRET_KEY": "test-secret",
        "APCA_API_BASE_URL": "https://paper-api.alpaca.markets",
        **env,
    }, clear=True), patch("bot.config.load_dotenv"):
        return load_settings()


def broker_error(status):
    response = Response()
    response.status_code = status
    return APIError('{"code":40410000,"message":"test"}', HTTPError(response=response))


class ExecutionRegressionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.settings = settings()
        self.client = Mock(settings=self.settings)
        self.book = OpenPositionBook(self.root / "positions.json")
        self.pending = PendingOrderBook(self.root / "pending.json")
        self.executor = OrderExecutor(
            self.client, dry_run=False, journal=Mock(),
            position_book=self.book, pending_fills=self.pending,
        )
        self.engine = TradingEngine.__new__(TradingEngine)
        self.engine.settings = self.settings
        self.engine.executor = self.executor
        self.engine._closing_lock = threading.Lock()
        self.engine._closing_in_flight = set()
        self.engine._pending_lock = threading.Lock()
        self.engine._pending = {}
        self.engine._live_quote = Mock(return_value=(None, None, None))
        self.engine._note_position_closed = Mock()
        self.engine._on_buy_filled = Mock()
        self.engine.reporter = Mock()
        self.engine.notifier = Mock()
        self.engine.risk = Mock()
        self.engine.dynamic_tp = Mock()
        self.book.open(
            "AAPL", 2.5, 100, stop_price=99, take_profit_price=101.5,
            stop_pct=0.01, take_profit_pct=0.015, dry_run=False,
        )

    def row(self, **kwargs):
        return RestingOrder(
            order_id="close-1", symbol="AAPL", side="sell", qty=2.5,
            reason="stop_loss", is_close=True, event_id="event",
            submitted_at="test", entry_price=100, signal_price=99, **kwargs,
        )

    def test_fixed_stock_universe_and_legacy_filters(self):
        cfg = settings(
            SYMBOLS="TSLA", ADX_THRESHOLD="26.0", BREAKOUT_VOLUME_MULT="1.85",
            ADX_THRESHOLD_OVERRIDES="AAPL:26",
            BREAKOUT_VOLUME_MULT_OVERRIDES="AAPL:1.85",
        )
        self.assertEqual(cfg.stock_symbols, list(STOCK_UNIVERSE))
        self.assertEqual(len(sanitize_symbols(cfg.stock_symbols)), 13)
        self.assertEqual(cfg.adx_threshold, 24)
        self.assertEqual(cfg.breakout_volume_mult, 1.45)
        self.assertEqual(cfg.adx_threshold_overrides["AAPL"], 24)
        self.assertEqual(cfg.breakout_volume_mult_overrides["AAPL"], 1.45)
        self.assertEqual(all_symbols(cfg), list(STOCK_UNIVERSE))
        self.assertEqual(resolve_trading_mode(SimpleNamespace(is_open=False), cfg), (TradingMode.STOCKS, []))
        self.assertEqual(resolve_trading_mode(SimpleNamespace(is_open=True), cfg)[1], list(STOCK_UNIVERSE))

    def test_pending_cancel_is_not_terminal(self):
        for filled in ("0", "1"):
            order = SimpleNamespace(status="pending_cancel", qty="2.5", filled_qty=filled)
            self.assertIn(classify_order(order), {"resting", "partial"})
        row = self.row(filled_qty=1)
        self.pending.upsert(row)
        self.engine._finalize_resting_failed = Mock()
        order.filled_avg_price = "99"
        self.engine._apply_resting_order(row, order)
        self.assertTrue(self.pending.has_close("AAPL"))
        self.engine._finalize_resting_failed.assert_not_called()

    def test_buy_fills_anchor_levels_to_actual_entry(self):
        self.book.close("AAPL")
        self.executor._record_fill(
            "AAPL", 1, OrderSide.BUY, 102, entry_price=100,
            reason="signal", dry_run=False, order_id="buy-1",
            stop_price=99, take_profit_price=101.5,
            stop_pct=0.01, take_profit_pct=0.015,
        )
        pos = self.book.get("AAPL")
        self.assertEqual(pos.stop_price, 101)
        self.assertEqual(pos.take_profit_price, 103.5)
        self.book.add_fill("AAPL", 1, 104)
        self.assertEqual(pos.avg_entry_price, 103)
        self.assertEqual(pos.stop_price, 102)
        self.assertEqual(pos.take_profit_price, 104.5)
        self.assertEqual(pos.opened_qty, 2)

    def test_breakout_accepts_volume_above_145(self):
        bars = pd.DataFrame({
            "high": [101.0] * 20 + [103.0],
            "low": [99.0] * 21, "close": [100.0] * 20 + [102.0],
            "volume": [100.0] * 20 + [150.0],
        })
        signal, _ = detect_breakout(
            bars, lookback=20, volume_mult=self.settings.breakout_volume_mult,
            min_range_atr_mult=0.5, atr_period=14, has_long=False,
        )
        self.assertEqual(signal, Signal.BUY)

    def test_adx_25_passes_and_23_blocks(self):
        layer = SignalFilterLayer(self.settings, state=Mock())
        with patch("bot.strategy.signal_filters.last_adx", return_value=25):
            self.assertTrue(layer.check_adx("AAPL", pd.DataFrame()).allowed)
        with patch("bot.strategy.signal_filters.last_adx", return_value=23):
            self.assertFalse(layer.check_adx("AAPL", pd.DataFrame()).allowed)

    def test_repeated_close_is_silent_and_not_sent(self):
        self.pending.upsert(self.row())
        with patch("bot.engine.logger") as logger:
            for _ in range(20):
                self.assertFalse(self.engine._close_and_report("AAPL", 2.5, 100, 99, "stop_loss"))
                self.engine._ratchet_and_maybe_exit("AAPL", 2.5, 100, 99, 100, Mock())
            logger.info.assert_not_called()
        self.client.trading.submit_order.assert_not_called()
        self.engine.risk.trailing_stop.assert_not_called()

    def test_concurrent_close_submits_once(self):
        entered, release = threading.Event(), threading.Event()
        def submit(*args):
            entered.set()
            self.assertTrue(release.wait(3))
            self.pending.upsert(self.row())
            return False
        self.engine._submit_close_and_report = Mock(side_effect=submit)
        worker = threading.Thread(
            target=self.engine._close_and_report, args=("AAPL", 2.5, 100, 99, "stop_loss")
        )
        worker.start()
        try:
            self.assertTrue(entered.wait(3))
            self.assertFalse(self.engine._close_and_report("AAPL", 2.5, 100, 99, "stop_loss"))
        finally:
            release.set()
            worker.join(3)
        self.engine._submit_close_and_report.assert_called_once()

    def test_rejected_close_queues_without_repeated_submit(self):
        self.executor.close_position = Mock(return_value=OrderSubmitResult(False, broker_detail="rejected"))
        self.engine._close_and_report("AAPL", 2.5, 100, 99, "stop_loss")
        self.engine._close_and_report("AAPL", 2.5, 100, 99, "stop_loss")
        self.executor.close_position.assert_called_once()
        self.assertTrue(self.engine._pending["AAPL"].is_close)

    def test_pending_retry_does_not_cancel_inflight_close(self):
        self.pending.upsert(self.row())
        self.executor.cancel_open_orders = Mock()
        self.engine._retry_pending(PendingExecution("AAPL", "sell", 2.5, "stop_loss", 99, is_close=True))
        self.executor.cancel_open_orders.assert_not_called()

    def test_partial_fill_keeps_lock_until_terminal(self):
        row = self.row()
        self.pending.upsert(row)
        self.engine._reconcile_book_qty_from_broker = Mock(return_value=1.5)
        self.executor._record_fill = Mock(return_value=0)
        order = SimpleNamespace(status="partially_filled", qty="2.5", filled_qty="1", filled_avg_price="99")
        self.engine._apply_resting_order(row, order)
        self.engine._apply_resting_order(row, order)
        self.assertTrue(self.engine._is_closing("AAPL"))
        self.executor._record_fill.assert_called_once()
        self.engine._finalize_resting_failed = Mock()
        order.status = "canceled"
        self.engine._apply_resting_order(row, order)
        self.assertFalse(self.engine._is_closing("AAPL"))

    def test_filled_close_waits_for_broker_and_survives_restart(self):
        row = self.row(filled_qty=2.5)
        self.pending.upsert(row)
        order = SimpleNamespace(status="filled", qty="2.5", filled_qty="2.5", filled_avg_price="99")
        self.executor.get_position = Mock(return_value=SimpleNamespace(qty="2.5"))
        self.engine._finalize_resting_fill = Mock()
        self.engine._apply_resting_order(row, order)
        self.assertTrue(PendingOrderBook(self.root / "pending.json").has_close("AAPL"))
        self.engine._finalize_resting_fill.assert_not_called()
        self.executor.get_position.return_value = None
        self.engine._apply_resting_order(row, order)
        self.assertFalse(self.engine._is_closing("AAPL"))
        self.engine._finalize_resting_fill.assert_called_once()

    def test_broker_errors_are_not_closed_positions(self):
        for error in (TimeoutError("timeout"), broker_error(500)):
            self.client.trading.get_open_position.side_effect = error
            with self.assertRaises(type(error)):
                self.executor.get_position("AAPL")
        self.client.trading.get_open_position.side_effect = broker_error(404)
        self.assertIsNone(self.executor.get_position("AAPL"))

    def test_fractional_stock_close_uses_day_and_exact_qty(self):
        self.client.trading.get_open_position.return_value = SimpleNamespace(qty="0.123456789", avg_entry_price="100")
        self.executor._broker_submit = Mock(return_value=SimpleNamespace(
            id="close-1", status="accepted", qty="0.123456789", filled_qty="0",
        ))
        self.executor.close_position("AAPL", price=99, spread_pct=0.01)
        args = self.executor._broker_submit.call_args.args
        self.assertEqual(args[1], 0.123456789)
        self.assertEqual(args[3], TimeInForce.DAY)
        self.assertFalse(args[4])

    def test_quantity_truncates_and_rejects_zero(self):
        self.assertEqual(sanitize_qty(0.1234569, fractional=True), 0.123456)
        for qty in (0, float("nan"), float("inf"), 0.0000001):
            with self.assertRaises(ValidationError):
                sanitize_qty(qty, fractional=True)
        self.assertEqual(RiskManager._normalize_qty("AAPL", 2.99), 2)
        self.assertEqual(RiskManager._normalize_qty("BTC/USD", 0.1234569), 0.123456)
        self.assertEqual(RiskManager._normalize_qty("AAPL", float("nan")), 0)

    def test_levels_do_not_move_with_last_price_and_ignore_tiny_atr(self):
        policy = StopTakeProfitPolicy(stop_loss_pct=0.01, take_profit_pct=0.015)
        a = policy.levels(100, 2, last_price=99, atr_value=2)
        b = policy.levels(100, 2, last_price=101, atr_value=2)
        self.assertEqual(a, b)
        self.assertAlmostEqual(a.stop_price, 97)
        tiny = policy.levels(100, 2, atr_value=0.00001)
        self.assertAlmostEqual(tiny.stop_price, 99)
        self.assertAlmostEqual(tiny.take_profit_price, 101.5)
        self.assertFalse(policy.levels(100, 2, atr_value=float("inf")).used_atr)
        self.assertIsNone(policy.trailing_candidate(100, 2, 100.01, 99, 0.00001)[0])

    def test_sizing_uses_same_stop_distance_and_never_exceeds_cap(self):
        risk = RiskManager(self.settings)
        for atr in (None, 0.00001, 2, 1000, float("nan")):
            levels = risk.protective_levels(100, 1, atr_value=atr, symbol="AAPL")
            self.assertAlmostEqual(risk._stop_distance(100, atr, None, "AAPL"), 100 - levels.stop_price)
            qty = risk._qty_from_risk("BTC/USD", 100, 10000, atr, None, 12.34569)
            self.assertLessEqual(qty * 100, 12.34569)


if __name__ == "__main__":
    unittest.main()
