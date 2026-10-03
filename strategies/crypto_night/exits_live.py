"""Salidas live: TP fijo, time stop 3h; BE/trail solo tras ganancia mínima (%%)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from strategies.crypto_night.position_book import NightTradeRecord
from strategies.crypto_night.protective_pct import unrealized_pct
from strategies.crypto_night.types import TradeSide


MAX_HOLD_SECONDS = 3 * 3600
TIME_STOP_MIN_R = 0.5


@dataclass(frozen=True)
class ExitDecision:
    action: str  # hold | partial_1r | close_all | update_stop
    close_qty: float = 0.0
    new_runner_stop: float | None = None
    reason: str = ""


def _risk_dist(trade: NightTradeRecord) -> float:
    if trade.entry_price is None:
        return 0.0
    return abs(float(trade.entry_price) - float(trade.stop_price))


def current_r(trade: NightTradeRecord, last_price: float) -> float:
    risk = _risk_dist(trade)
    if risk <= 0 or trade.entry_price is None:
        return 0.0
    entry = float(trade.entry_price)
    if trade.side == TradeSide.LONG.value:
        return (last_price - entry) / risk
    return (entry - last_price) / risk


def stop_hit(trade: NightTradeRecord, last_price: float) -> bool:
    stop = trade.runner_stop if trade.partial_taken else trade.stop_price
    if trade.side == TradeSide.LONG.value:
        return last_price <= stop
    return last_price >= stop


def take_profit_hit(trade: NightTradeRecord, last_price: float) -> bool:
    tp = float(trade.take_profit_price or 0.0)
    if tp <= 0:
        return False
    if trade.side == TradeSide.LONG.value:
        return last_price >= tp
    return last_price <= tp


def _compression_trade(trade: NightTradeRecord) -> bool:
    return bool(trade.meta.get("compression_mode"))


def evaluate_exit(
    trade: NightTradeRecord,
    last_price: float,
    now: datetime,
    *,
    scale_at_1r: bool = False,
    breakeven_activate_pct: float = 0.00375,
    breakeven_buffer_pct: float = 0.0,
    trail_offset_pct: float = 0.0021,
    compression_trail_atr_mult: float = 2.0,
    compression_max_runner_pct: float = 0.025,
) -> ExitDecision:
    if trade.entry_price is None or trade.qty_open <= 0:
        return ExitDecision("hold")

    risk = _risk_dist(trade)
    if risk <= 0:
        return ExitDecision("close_all", close_qty=trade.qty_open, reason="invalid_risk")

    r = current_r(trade, last_price)
    best = max(trade.best_r, r)

    compression = _compression_trade(trade)
    partial_tp_pct = float(trade.meta.get("compression_partial_tp_pct") or 0.0)

    if stop_hit(trade, last_price):
        return ExitDecision(
            "close_all",
            close_qty=trade.qty_open,
            reason="stop",
        )

    entry = float(trade.entry_price)
    upnl = unrealized_pct(entry_price=entry, last_price=last_price, side=trade.side)

    if compression and partial_tp_pct > 0 and not trade.partial_taken and upnl >= partial_tp_pct:
        half = trade.qty_open / 2.0
        close_qty = half if half > 0 else trade.qty_open
        return ExitDecision(
            "partial_1r",
            close_qty=close_qty,
            new_runner_stop=trade.runner_stop,
            reason="compression_partial_tp",
        )

    if compression and trade.partial_taken and compression_max_runner_pct > 0:
        if upnl >= float(compression_max_runner_pct):
            return ExitDecision(
                "close_all",
                close_qty=trade.qty_open,
                reason="compression_expansion_max",
            )

    if not compression and take_profit_hit(trade, last_price):
        return ExitDecision(
            "close_all",
            close_qty=trade.qty_open,
            reason="take_profit",
        )

    if trade.entry_time_utc:
        try:
            opened = datetime.fromisoformat(trade.entry_time_utc.replace("Z", "+00:00"))
            if opened.tzinfo is None:
                opened = opened.replace(tzinfo=timezone.utc)
            age = (now - opened.astimezone(timezone.utc)).total_seconds()
        except ValueError:
            age = 0.0
        if age >= MAX_HOLD_SECONDS:
            if best < TIME_STOP_MIN_R:
                return ExitDecision(
                    "close_all",
                    close_qty=trade.qty_open,
                    reason="time_stop",
                )
            return ExitDecision(
                "close_all",
                close_qty=trade.qty_open,
                reason="time_exit",
            )

    be_threshold = max(0.0, float(breakeven_activate_pct))
    atr_abs = float(trade.meta.get("atr_abs") or 0.0)

    if scale_at_1r and not trade.partial_taken and r >= 1.0:
        if be_threshold > 0 and upnl < be_threshold:
            pass
        else:
            if trade.side == TradeSide.LONG.value:
                new_stop = entry + 0.3 * risk
            else:
                new_stop = entry - 0.3 * risk
            half = trade.qty_open / 2.0
            close_qty = half if half > 0 else trade.qty_open
            return ExitDecision(
                "partial_1r",
                close_qty=close_qty,
                new_runner_stop=new_stop,
                reason="scale_1r",
            )

    if scale_at_1r and trade.partial_taken and r >= 2.0:
        if be_threshold <= 0 or upnl >= be_threshold:
            if trade.side == TradeSide.LONG.value:
                trail = entry + 1.5 * risk
                if trail > trade.runner_stop:
                    return ExitDecision(
                        "update_stop",
                        new_runner_stop=trail,
                        reason="trail_2r",
                    )
            else:
                trail = entry - 1.5 * risk
                if trail < trade.runner_stop:
                    return ExitDecision(
                        "update_stop",
                        new_runner_stop=trail,
                        reason="trail_2r",
                    )

    if be_threshold > 0 and upnl >= be_threshold:
        buf = entry * max(0.0, float(breakeven_buffer_pct))
        if trade.side == TradeSide.LONG.value:
            be_stop = entry + buf
            if trade.runner_stop < be_stop - 1e-12:
                return ExitDecision(
                    "update_stop",
                    new_runner_stop=be_stop,
                    reason="breakeven_lock",
                )
        else:
            be_stop = entry - buf
            if trade.runner_stop > be_stop + 1e-12:
                return ExitDecision(
                    "update_stop",
                    new_runner_stop=be_stop,
                    reason="breakeven_lock",
                )

    if compression and atr_abs > 0 and upnl >= be_threshold:
        mult = max(0.5, float(compression_trail_atr_mult))
        if trade.side == TradeSide.LONG.value:
            trail = last_price - mult * atr_abs
            if trail > trade.runner_stop + 1e-12:
                return ExitDecision(
                    "update_stop",
                    new_runner_stop=trail,
                    reason="compression_trail_atr",
                )
        else:
            trail = last_price + mult * atr_abs
            if trail < trade.runner_stop - 1e-12:
                return ExitDecision(
                    "update_stop",
                    new_runner_stop=trail,
                    reason="compression_trail_atr",
                )

    return ExitDecision("hold")
